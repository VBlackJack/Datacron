# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Incremental primary-filesystem integrity scrubber with alert-only evidence."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import uuid4

from datacron.core.config import Settings
from datacron.core.durability import WritePolicy
from datacron.core.hashing import index_generation_hash, sha256_bytes
from datacron.core.logger import get_logger
from datacron.core.paths import PathConfinementError
from datacron.core.protocols import FTS5Store
from datacron.core.scope import VaultScope
from datacron.core.vault_writer import atomic_durable_write

__all__ = [
    "SCRUB_CHECKPOINT_SCHEMA_VERSION",
    "SCRUB_FAULT_POINTS",
    "CanaryInitializationError",
    "ScrubAnomaly",
    "ScrubCheckpointError",
    "ScrubState",
    "initialize_canaries",
    "read_scrub_state",
    "read_scrubber_health",
    "run_integrity_scrub",
]

_LOGGER = get_logger(__name__)

SCRUB_CHECKPOINT_SCHEMA_VERSION: Final[int] = 1
SCRUB_FAULT_POINTS: Final[tuple[str, ...]] = ("after_checkpoint",)
_MEBIBYTE: Final[int] = 1024 * 1024

AnomalySource = Literal["note", "canary", "checkpoint"]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
FaultInjector = Callable[[str, "ScrubState"], None]
NoteObserver = Callable[[str], None]


class CanaryInitializationError(RuntimeError):
    """Raised when explicit canary initialization would overwrite evidence."""


class ScrubCheckpointError(RuntimeError):
    """Raised when persisted scrub state is malformed or unsupported."""


@dataclass(frozen=True)
class ScrubAnomaly:
    """One deduplicated alert produced from primary filesystem bytes."""

    source: AnomalySource
    rel_path: str
    kind: str
    expected_hash: str | None
    actual_hash: str | None
    size_bytes: int | None
    detected_at: str

    @property
    def key(self) -> tuple[str, str]:
        """Deduplicate evolving classifications for one source path."""
        return self.source, self.rel_path

    def to_dict(self) -> dict[str, object]:
        """Return the stable ASCII-JSON representation."""
        return {
            "source": self.source,
            "path": self.rel_path,
            "type": self.kind,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "size_bytes": self.size_bytes,
            "detected_at": self.detected_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScrubAnomaly:
        """Validate and decode one persisted anomaly."""
        source = payload.get("source")
        if not isinstance(source, str) or source not in {"note", "canary", "checkpoint"}:
            raise ScrubCheckpointError("invalid scrub anomaly source")
        rel_path = payload.get("path")
        kind = payload.get("type")
        detected_at = payload.get("detected_at")
        if not isinstance(rel_path, str) or not isinstance(kind, str):
            raise ScrubCheckpointError("invalid scrub anomaly string field")
        if not isinstance(detected_at, str):
            raise ScrubCheckpointError("invalid scrub anomaly string field")
        expected_hash = _optional_string(payload.get("expected_hash"))
        actual_hash = _optional_string(payload.get("actual_hash"))
        size_bytes = _optional_non_negative_int(payload.get("size_bytes"))
        return cls(
            source=cast("AnomalySource", source),
            rel_path=rel_path,
            kind=kind,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            size_bytes=size_bytes,
            detected_at=detected_at,
        )


@dataclass(frozen=True)
class ScrubState:
    """Durable cursor and evidence for one index generation scrub pass."""

    pass_id: str
    index_generation: int
    index_generation_hash: str
    started_at: str
    updated_at: str
    last_completed_at: str | None
    next_index: int
    checked_notes: int
    checked_bytes: int
    total_notes: int
    completed: bool
    canaries_checked: int
    canaries_total: int
    anomalies: tuple[ScrubAnomaly, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the checkpoint payload."""
        return {
            "schema_version": SCRUB_CHECKPOINT_SCHEMA_VERSION,
            "pass_id": self.pass_id,
            "index_generation": self.index_generation,
            "index_generation_hash": self.index_generation_hash,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_completed_at": self.last_completed_at,
            "next_index": self.next_index,
            "checked_notes": self.checked_notes,
            "checked_bytes": self.checked_bytes,
            "total_notes": self.total_notes,
            "completed": self.completed,
            "canaries_checked": self.canaries_checked,
            "canaries_total": self.canaries_total,
            "anomalies": [anomaly.to_dict() for anomaly in self.anomalies],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScrubState:
        """Validate and decode a persisted checkpoint."""
        if payload.get("schema_version") != SCRUB_CHECKPOINT_SCHEMA_VERSION:
            raise ScrubCheckpointError("unsupported scrub checkpoint schema")
        pass_id = _required_string(payload, "pass_id")
        generation_hash = _required_string(payload, "index_generation_hash")
        started_at = _required_string(payload, "started_at")
        updated_at = _required_string(payload, "updated_at")
        last_completed_at = _optional_string(payload.get("last_completed_at"))
        index_generation = _required_non_negative_int(payload, "index_generation")
        next_index = _required_non_negative_int(payload, "next_index")
        checked_notes = _required_non_negative_int(payload, "checked_notes")
        checked_bytes = _required_non_negative_int(payload, "checked_bytes")
        total_notes = _required_non_negative_int(payload, "total_notes")
        canaries_checked = _required_non_negative_int(payload, "canaries_checked")
        canaries_total = _required_non_negative_int(payload, "canaries_total")
        completed = payload.get("completed")
        if not isinstance(completed, bool):
            raise ScrubCheckpointError("invalid completed field")
        raw_anomalies = payload.get("anomalies")
        if not isinstance(raw_anomalies, list):
            raise ScrubCheckpointError("invalid anomalies field")
        anomalies = tuple(
            ScrubAnomaly.from_dict(item) for item in raw_anomalies if isinstance(item, dict)
        )
        if len(anomalies) != len(raw_anomalies):
            raise ScrubCheckpointError("invalid anomaly record")
        if next_index > total_notes or checked_notes != next_index:
            raise ScrubCheckpointError("scrub checkpoint cursor is inconsistent")
        return cls(
            pass_id=pass_id,
            index_generation=index_generation,
            index_generation_hash=generation_hash,
            started_at=started_at,
            updated_at=updated_at,
            last_completed_at=last_completed_at,
            next_index=next_index,
            checked_notes=checked_notes,
            checked_bytes=checked_bytes,
            total_notes=total_notes,
            completed=completed,
            canaries_checked=canaries_checked,
            canaries_total=canaries_total,
            anomalies=anomalies,
        )


def initialize_canaries(
    vault_root: Path,
    settings: Settings,
    scope: VaultScope,
    write_policy: WritePolicy,
) -> dict[str, int]:
    """Explicitly create missing canaries and refuse every overwrite."""
    write_policy.ensure_writable()
    root = scope.authorize_path(vault_root, "read")
    targets: list[tuple[Path, bytes]] = []
    existing = 0
    for name, content in sorted(settings.scrub_canaries.items()):
        rel_path = (settings.scrub_canary_dir / name).as_posix()
        target = scope.authorize_rel_path(rel_path, "write")
        data = content.encode("utf-8")
        if target.exists():
            current = target.read_bytes()
            if current != data:
                raise CanaryInitializationError(
                    f"refusing to overwrite altered integrity canary: {rel_path}"
                )
            existing += 1
        targets.append((target, data))

    created = 0
    for target, data in targets:
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_durable_write(target, data)
        created += 1
        _LOGGER.info("Initialized integrity canary %s", target.relative_to(root).as_posix())
    return {"created": created, "existing": existing, "total": len(targets)}


async def run_integrity_scrub(
    vault_root: Path,
    settings: Settings,
    scope: VaultScope,
    write_policy: WritePolicy,
    store: FTS5Store,
    *,
    clock: Clock = time.monotonic,
    sleeper: Sleeper = asyncio.sleep,
    fault_injector: FaultInjector | None = None,
    note_observer: NoteObserver | None = None,
) -> ScrubState:
    """Resume one budgeted pass and persist alert-only primary-byte evidence."""
    write_policy.ensure_writable()
    root = scope.authorize_path(vault_root, "read")
    checkpoint = _checkpoint_path(root, settings, scope, access="write")
    stats, indexed = await asyncio.gather(store.stats(), store.list_indexed_notes())
    generation_hash = index_generation_hash(indexed)
    paths = sorted(indexed)
    prior = _read_state_path(checkpoint)
    state = _resume_or_start(prior, stats.generation, generation_hash, len(paths))
    anomalies = {anomaly.key: anomaly for anomaly in state.anomalies}

    run_started = clock()
    canary_anomalies, canary_bytes, canaries_checked = await _check_canaries(
        root,
        settings,
        scope,
    )
    anomalies = {key: value for key, value in anomalies.items() if key[0] != "canary"}
    for canary_anomaly in canary_anomalies:
        anomalies[canary_anomaly.key] = canary_anomaly
        _log_alert(canary_anomaly)
    state = replace(
        state,
        updated_at=_utc_now(),
        canaries_checked=canaries_checked,
        canaries_total=len(settings.scrub_canaries),
        anomalies=_sorted_anomalies(anomalies),
    )
    _write_state(checkpoint, state)

    invocation_notes = canaries_checked
    invocation_bytes = canary_bytes
    await _pace(
        invocation_notes,
        invocation_bytes,
        run_started,
        settings,
        clock,
        sleeper,
    )

    processed_since_checkpoint = 0
    for position in range(state.next_index, len(paths)):
        if invocation_notes > 0 and clock() - run_started >= settings.scrub_max_duration_seconds:
            break
        rel_path = paths[position]
        expected_hash = indexed[rel_path][1]
        note_anomaly, size_bytes = await _check_note(root, rel_path, expected_hash, scope)
        if note_observer is not None:
            note_observer(rel_path)
        anomalies.pop(("note", rel_path), None)
        if note_anomaly is not None:
            anomalies[note_anomaly.key] = note_anomaly
            _log_alert(note_anomaly)
        state = replace(
            state,
            next_index=position + 1,
            checked_notes=position + 1,
            checked_bytes=state.checked_bytes + size_bytes,
            updated_at=_utc_now(),
            anomalies=_sorted_anomalies(anomalies),
        )
        invocation_notes += 1
        invocation_bytes += size_bytes
        processed_since_checkpoint += 1
        await _pace(
            invocation_notes,
            invocation_bytes,
            run_started,
            settings,
            clock,
            sleeper,
        )
        if processed_since_checkpoint >= settings.scrub_checkpoint_interval_notes:
            _write_state(checkpoint, state)
            processed_since_checkpoint = 0
            _inject(fault_injector, "after_checkpoint", state)

    completed = state.next_index == state.total_notes
    completed_at = _utc_now() if completed else state.last_completed_at
    state = replace(
        state,
        completed=completed,
        updated_at=_utc_now(),
        last_completed_at=completed_at,
        anomalies=_sorted_anomalies(anomalies),
    )
    _write_state(checkpoint, state)
    _LOGGER.info(
        "Integrity scrub checkpointed "
        "(vault=%s pass=%s checked=%d total=%d anomalies=%d completed=%s)",
        root,
        state.pass_id,
        state.checked_notes,
        state.total_notes,
        len(state.anomalies),
        state.completed,
    )
    return state


def read_scrub_state(
    vault_root: Path,
    settings: Settings,
    scope: VaultScope,
) -> ScrubState | None:
    """Read the checkpoint without creating or repairing any file."""
    root = scope.authorize_path(vault_root, "read")
    checkpoint = _checkpoint_path(root, settings, scope, access="read")
    return _read_state_path(checkpoint)


def read_scrubber_health(
    vault_root: Path,
    settings: Settings,
    scope: VaultScope,
    *,
    current_generation: int,
    current_total_notes: int,
) -> dict[str, object]:
    """Return scrub evidence for get_health without running a scrub."""
    try:
        state = read_scrub_state(vault_root, settings, scope)
    except (OSError, ScrubCheckpointError, ValueError) as exc:
        rel_path = settings.scrub_checkpoint_path.as_posix()
        _LOGGER.error("SCRUBBER ALERT checkpoint_unreadable path=%s error=%s", rel_path, exc)
        anomaly = ScrubAnomaly(
            source="checkpoint",
            rel_path=rel_path,
            kind="checkpoint_unreadable",
            expected_hash=None,
            actual_hash=None,
            size_bytes=None,
            detected_at=_utc_now(),
        )
        return _health_payload(
            status="critical",
            state=None,
            anomalies=(anomaly,),
            total_notes=current_total_notes,
            canaries_total=len(settings.scrub_canaries),
        )
    if state is None:
        return _health_payload(
            status="not_run",
            state=None,
            anomalies=(),
            total_notes=current_total_notes,
            canaries_total=len(settings.scrub_canaries),
        )
    if state.anomalies:
        status = "critical"
    elif state.index_generation != current_generation or state.total_notes != current_total_notes:
        status = "stale"
    elif state.completed:
        status = "complete"
    else:
        status = "running"
    return _health_payload(
        status=status,
        state=state,
        anomalies=state.anomalies,
        total_notes=state.total_notes,
        canaries_total=state.canaries_total,
    )


async def _check_note(
    root: Path,
    rel_path: str,
    expected_hash: str,
    scope: VaultScope,
) -> tuple[ScrubAnomaly | None, int]:
    detected_at = _utc_now()
    try:
        path = scope.authorize_rel_path(rel_path, "read")
        raw = await asyncio.to_thread(path.read_bytes)
    except PathConfinementError:
        return (
            ScrubAnomaly(
                source="note",
                rel_path=rel_path,
                kind="path_scope_violation",
                expected_hash=expected_hash,
                actual_hash=None,
                size_bytes=None,
                detected_at=detected_at,
            ),
            0,
        )
    except FileNotFoundError:
        return (
            ScrubAnomaly(
                source="note",
                rel_path=rel_path,
                kind="missing",
                expected_hash=expected_hash,
                actual_hash=None,
                size_bytes=None,
                detected_at=detected_at,
            ),
            0,
        )
    except OSError:
        return (
            ScrubAnomaly(
                source="note",
                rel_path=rel_path,
                kind="read_error",
                expected_hash=expected_hash,
                actual_hash=None,
                size_bytes=None,
                detected_at=detected_at,
            ),
            0,
        )
    actual_hash = sha256_bytes(raw)
    if actual_hash == expected_hash:
        return None, len(raw)
    kind = "nul_bytes_hash_mismatch" if b"\x00" in raw else "content_hash_mismatch"
    return (
        ScrubAnomaly(
            source="note",
            rel_path=rel_path,
            kind=kind,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            size_bytes=len(raw),
            detected_at=detected_at,
        ),
        len(raw),
    )


async def _check_canaries(
    root: Path,
    settings: Settings,
    scope: VaultScope,
) -> tuple[list[ScrubAnomaly], int, int]:
    anomalies: list[ScrubAnomaly] = []
    checked_bytes = 0
    checked = 0
    for name, content in sorted(settings.scrub_canaries.items()):
        rel_path = (settings.scrub_canary_dir / name).as_posix()
        expected_hash = sha256_bytes(content.encode("utf-8"))
        detected_at = _utc_now()
        try:
            path = scope.authorize_rel_path(rel_path, "read")
            raw = await asyncio.to_thread(path.read_bytes)
        except PathConfinementError:
            anomalies.append(
                ScrubAnomaly(
                    source="canary",
                    rel_path=rel_path,
                    kind="canary_scope_violation",
                    expected_hash=expected_hash,
                    actual_hash=None,
                    size_bytes=None,
                    detected_at=detected_at,
                )
            )
            continue
        except FileNotFoundError:
            anomalies.append(
                ScrubAnomaly(
                    source="canary",
                    rel_path=rel_path,
                    kind="canary_missing",
                    expected_hash=expected_hash,
                    actual_hash=None,
                    size_bytes=None,
                    detected_at=detected_at,
                )
            )
            continue
        except OSError:
            anomalies.append(
                ScrubAnomaly(
                    source="canary",
                    rel_path=rel_path,
                    kind="canary_read_error",
                    expected_hash=expected_hash,
                    actual_hash=None,
                    size_bytes=None,
                    detected_at=detected_at,
                )
            )
            continue
        checked += 1
        checked_bytes += len(raw)
        actual_hash = sha256_bytes(raw)
        if actual_hash != expected_hash:
            kind = "canary_nul_hash_mismatch" if b"\x00" in raw else "canary_hash_mismatch"
            anomalies.append(
                ScrubAnomaly(
                    source="canary",
                    rel_path=rel_path,
                    kind=kind,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    size_bytes=len(raw),
                    detected_at=detected_at,
                )
            )
    return anomalies, checked_bytes, checked


async def _pace(
    notes: int,
    byte_count: int,
    started: float,
    settings: Settings,
    clock: Clock,
    sleeper: Sleeper,
) -> None:
    note_seconds = notes / settings.scrub_notes_per_second
    byte_seconds = byte_count / (_MEBIBYTE * settings.scrub_mebibytes_per_second)
    required_elapsed = max(note_seconds, byte_seconds)
    delay = required_elapsed - (clock() - started)
    if delay > 0:
        await sleeper(delay)


def _resume_or_start(
    prior: ScrubState | None,
    generation: int,
    generation_hash: str,
    total_notes: int,
) -> ScrubState:
    if (
        prior is not None
        and not prior.completed
        and prior.index_generation == generation
        and prior.index_generation_hash == generation_hash
        and prior.total_notes == total_notes
    ):
        return prior
    now = _utc_now()
    return ScrubState(
        pass_id=uuid4().hex,
        index_generation=generation,
        index_generation_hash=generation_hash,
        started_at=now,
        updated_at=now,
        last_completed_at=None if prior is None else prior.last_completed_at,
        next_index=0,
        checked_notes=0,
        checked_bytes=0,
        total_notes=total_notes,
        completed=False,
        canaries_checked=0,
        canaries_total=0,
        anomalies=() if prior is None else prior.anomalies,
    )


def _checkpoint_path(
    root: Path,
    settings: Settings,
    scope: VaultScope,
    *,
    access: Literal["read", "write"],
) -> Path:
    return scope.authorize_path(root / settings.scrub_checkpoint_path, access)


def _read_state_path(path: Path) -> ScrubState | None:
    if not path.is_file():
        return None
    try:
        payload: Any = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScrubCheckpointError(f"cannot decode scrub checkpoint: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScrubCheckpointError("scrub checkpoint must be a JSON object")
    return ScrubState.from_dict(payload)


def _write_state(path: Path, state: ScrubState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(state.to_dict(), sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode(
        "ascii"
    )
    atomic_durable_write(path, data)


def _health_payload(
    *,
    status: str,
    state: ScrubState | None,
    anomalies: tuple[ScrubAnomaly, ...],
    total_notes: int,
    canaries_total: int,
) -> dict[str, object]:
    checked_notes = 0 if state is None else state.checked_notes
    coverage = 1.0 if total_notes == 0 else checked_notes / total_notes
    canaries_checked = 0 if state is None else state.canaries_checked
    return {
        "status": status,
        "last_scrub": None if state is None else state.last_completed_at,
        "pass_id": None if state is None else state.pass_id,
        "index_generation": None if state is None else state.index_generation,
        "coverage": {
            "checked_notes": checked_notes,
            "total_notes": total_notes,
            "fraction": round(coverage, 6),
            "complete": False if state is None else state.completed,
        },
        "checked_bytes": 0 if state is None else state.checked_bytes,
        "anomalies_count": len(anomalies),
        "anomalies": [anomaly.to_dict() for anomaly in anomalies],
        "canaries": {
            "checked": canaries_checked,
            "total": canaries_total,
            "healthy": canaries_checked == canaries_total
            and not any(anomaly.source == "canary" for anomaly in anomalies),
        },
    }


def _sorted_anomalies(
    anomalies: Mapping[tuple[str, str], ScrubAnomaly],
) -> tuple[ScrubAnomaly, ...]:
    return tuple(anomalies[key] for key in sorted(anomalies))


def _log_alert(anomaly: ScrubAnomaly) -> None:
    _LOGGER.error(
        "SCRUBBER ALERT source=%s type=%s path=%s expected_hash=%s actual_hash=%s",
        anomaly.source,
        anomaly.kind,
        anomaly.rel_path,
        anomaly.expected_hash,
        anomaly.actual_hash,
    )


def _inject(
    fault_injector: FaultInjector | None,
    point: str,
    state: ScrubState,
) -> None:
    if fault_injector is not None:
        fault_injector(point, state)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ScrubCheckpointError(f"invalid {key} field")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScrubCheckpointError("invalid optional string field")
    return value


def _required_non_negative_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScrubCheckpointError(f"invalid {key} field")
    return value


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScrubCheckpointError("invalid optional integer field")
    return value
