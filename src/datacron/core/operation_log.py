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
"""Durable JSONL operation evidence and content-addressed note history."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, NoReturn, TypeAlias

from datacron.core.config import (
    DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS,
    HISTORY_DIR_NAME,
    OPLOG_DIR_NAME,
    OPLOG_PENDING_DIR_NAME,
    SIDECAR_DIR_NAME,
)
from datacron.core.durability import atomic_durable_write, durable_flush_directory
from datacron.core.hashing import sha256_bytes
from datacron.core.logger import get_logger

JsonScalar: TypeAlias = str | int | float | bool | None

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS_FILENAME: Final[str] = "operations.jsonl"
_FORMAT_VERSION: Final[int] = 2
_MAX_PENDING_RECORD_BYTES: Final[int] = 64 * 1024
_PENDING_TEMP_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\..+\.json\.[0-9a-f]{32}\.tmp$")
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
_RECORD_KEYS_V2: Final[frozenset[str]] = frozenset(
    {
        "actor",
        "after_hash",
        "before_hash",
        "format_version",
        "history_stored",
        "note_id",
        "op",
        "operation_id",
        "parameters",
        "prev_hash",
        "rel_path",
        "timestamp",
        "tool",
    }
)
_RECORD_KEYS_V1: Final[frozenset[str]] = _RECORD_KEYS_V2 - {
    "format_version",
    "prev_hash",
}
_LOGGER = get_logger(__name__)


class OperationLogError(RuntimeError):
    """Raised when durable audit or history state is invalid."""


class HistoryUnavailableError(OperationLogError):
    """Raised when requested exact history bytes are absent or redacted."""


class _StrictJsonError(ValueError):
    """Raised when JSON uses an ambiguous or non-standard construct."""


def _assert_unlinked_operation_path(
    path: Path,
    *,
    anchor: Path,
    allow_missing: bool,
) -> Path:
    expanded = path.expanduser()
    expanded_anchor = anchor.expanduser()
    if not expanded.is_absolute() or not expanded_anchor.is_absolute():
        raise OperationLogError("operation journal paths must be absolute")
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    absolute_anchor = Path(os.path.abspath(os.fspath(expanded_anchor)))
    try:
        relative = absolute.relative_to(absolute_anchor)
    except ValueError as exc:
        raise OperationLogError(f"operation journal path escapes vault root: {absolute}") from exc
    current = absolute_anchor
    components = [current]
    for part in relative.parts:
        current /= part
        components.append(current)
    for component in components:
        try:
            component_stat = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                break
            raise
        attributes = getattr(component_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(component_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise OperationLogError(
                f"linked or reparse operation journal path is forbidden: {component}"
            )
    return absolute


@dataclass(frozen=True)
class OperationContext:
    """Non-content audit metadata supplied by one mutation tool."""

    op: str
    tool: str
    actor: str
    parameters: dict[str, JsonScalar]


@dataclass(frozen=True)
class OperationRecord:
    """One final, committed operation-log line."""

    operation_id: str
    timestamp: str
    op: str
    tool: str
    note_id: str | None
    rel_path: str
    before_hash: str | None
    after_hash: str
    actor: str
    parameters: dict[str, JsonScalar]
    history_stored: bool
    prev_hash: str | None = None
    format_version: int = _FORMAT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "timestamp": self.timestamp,
            "op": self.op,
            "tool": self.tool,
            "note_id": self.note_id,
            "rel_path": self.rel_path,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "actor": self.actor,
            "parameters": self.parameters,
            "history_stored": self.history_stored,
            "prev_hash": self.prev_hash,
            "format_version": self.format_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> OperationRecord:
        if not isinstance(payload, dict):
            raise OperationLogError("operation record must be a JSON object")
        _require_exact_record_keys(payload)
        required_strings = (
            "operation_id",
            "timestamp",
            "op",
            "tool",
            "rel_path",
            "after_hash",
            "actor",
        )
        for key in required_strings:
            if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
                raise OperationLogError(f"operation record field {key!r} must be a string")
        before_hash = payload.get("before_hash")
        note_id = payload.get("note_id")
        parameters = payload.get("parameters")
        history_stored = payload.get("history_stored")
        prev_hash = payload.get("prev_hash")
        format_version = payload.get("format_version", 1)
        if before_hash is not None and not isinstance(before_hash, str):
            raise OperationLogError("before_hash must be a string or null")
        if note_id is not None and not isinstance(note_id, str):
            raise OperationLogError("note_id must be a string or null")
        if not isinstance(parameters, dict):
            raise OperationLogError("parameters must be a JSON object")
        if not isinstance(history_stored, bool):
            raise OperationLogError("history_stored must be a boolean")
        if prev_hash is not None and not isinstance(prev_hash, str):
            raise OperationLogError("prev_hash must be a string or null")
        if not isinstance(format_version, int) or isinstance(format_version, bool):
            raise OperationLogError("format_version must be an integer")
        cleaned_parameters: dict[str, JsonScalar] = {}
        for key, value in parameters.items():
            scalar = isinstance(value, (str, int, float, bool, type(None)))
            if not isinstance(key, str) or not scalar:
                raise OperationLogError("parameters must contain scalar JSON values")
            if isinstance(value, float) and not math.isfinite(value):
                raise OperationLogError("parameters must contain only finite JSON numbers")
            cleaned_parameters[key] = value
        record = cls(
            operation_id=str(payload["operation_id"]),
            timestamp=str(payload["timestamp"]),
            op=str(payload["op"]),
            tool=str(payload["tool"]),
            note_id=note_id,
            rel_path=str(payload["rel_path"]),
            before_hash=before_hash,
            after_hash=str(payload["after_hash"]),
            actor=str(payload["actor"]),
            parameters=cleaned_parameters,
            history_stored=history_stored,
            prev_hash=prev_hash,
            format_version=format_version,
        )
        record.validate()
        return record

    def validate(self) -> None:
        for key, value in self.parameters.items():
            scalar = isinstance(value, (str, int, float, bool, type(None)))
            if not isinstance(key, str) or not scalar:
                raise OperationLogError("parameters must contain scalar JSON values")
            if isinstance(value, float) and not math.isfinite(value):
                raise OperationLogError("parameters must contain only finite JSON numbers")
        if self.before_hash is not None and not _HASH_PATTERN.fullmatch(self.before_hash):
            raise OperationLogError("before_hash is not a lowercase SHA-256")
        if not _HASH_PATTERN.fullmatch(self.after_hash):
            raise OperationLogError("after_hash is not a lowercase SHA-256")
        if self.prev_hash is not None and not _HASH_PATTERN.fullmatch(self.prev_hash):
            raise OperationLogError("prev_hash is not a lowercase SHA-256")
        if self.format_version not in {1, _FORMAT_VERSION}:
            raise OperationLogError(f"unsupported operation log format: {self.format_version}")
        if self.format_version == 1 and self.prev_hash is not None:
            raise OperationLogError("legacy operation records cannot contain prev_hash")
        try:
            parsed = datetime.fromisoformat(self.timestamp)
        except ValueError as exc:
            raise OperationLogError("timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise OperationLogError("timestamp must include a timezone")


class OperationJournal:
    """Manage final audit records, pending manifests, and exact history blobs."""

    def __init__(
        self,
        vault_root: Path,
        *,
        retention_days: int,
        history_mode: str,
        purge_min_interval_seconds: float = DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._sidecar = self._vault_root / SIDECAR_DIR_NAME
        self._oplog_dir = self._sidecar / OPLOG_DIR_NAME
        self._pending_dir = self._oplog_dir / OPLOG_PENDING_DIR_NAME
        self._operations_path = self._oplog_dir / _OPERATIONS_FILENAME
        self._history_dir = self._sidecar / HISTORY_DIR_NAME
        self._retention_days = retention_days
        self._history_mode = history_mode
        self._purge_min_interval = timedelta(seconds=purge_min_interval_seconds)
        self._last_purge_at: datetime | None = None
        self._tail_record: OperationRecord | None = None
        self._tail_hash: str | None = None
        self._tail_loaded = False

    @property
    def history_enabled(self) -> bool:
        return self._history_mode == "full"

    def _guard_path(self, path: Path, *, allow_missing: bool = True) -> Path:
        return _assert_unlinked_operation_path(
            path,
            anchor=self._vault_root,
            allow_missing=allow_missing,
        )

    def _guard_history_root(self) -> Path:
        self._guard_path(self._sidecar)
        return self._guard_path(self._history_dir)

    def _guard_oplog_root(self) -> Path:
        self._guard_path(self._sidecar)
        return self._guard_path(self._oplog_dir)

    def _guard_pending_root(self) -> Path:
        self._guard_oplog_root()
        return self._guard_path(self._pending_dir)

    def _guard_operations_path(self) -> Path:
        self._guard_oplog_root()
        return self._guard_path(self._operations_path)

    def _guard_history_target(self, path: Path) -> Path:
        history_dir = self._guard_history_root()
        target = self._guard_path(path)
        if target.parent != history_dir:
            raise OperationLogError("history target escapes the history directory")
        return target

    def _guard_pending_target(self, path: Path) -> Path:
        pending_dir = self._guard_pending_root()
        target = self._guard_path(path)
        if target.parent != pending_dir:
            raise OperationLogError("pending target escapes the pending directory")
        return target

    def next_timestamp(self, now: datetime | None = None) -> str:
        candidate = (now or datetime.now(tz=UTC)).astimezone(UTC)
        self._ensure_tail_state()
        if self._tail_record is not None:
            previous = datetime.fromisoformat(self._tail_record.timestamp).astimezone(UTC)
            if candidate <= previous:
                candidate = previous + timedelta(microseconds=1)
        return candidate.isoformat(timespec="microseconds")

    def store_history(self, data: bytes) -> str:
        content_hash = sha256_bytes(data)
        if not self.history_enabled:
            return content_hash
        history_dir = self._guard_history_root()
        history_dir.mkdir(parents=True, exist_ok=True)
        history_dir = self._guard_history_root()
        path = self._guard_history_target(history_dir / content_hash)
        if path.exists():
            path = self._guard_history_target(path)
            existing = path.read_bytes()
            if sha256_bytes(existing) != content_hash or existing != data:
                raise OperationLogError(f"history blob is corrupt: {content_hash}")
            return content_hash
        _atomic_write(path, data)
        return content_hash

    def read_history(self, content_hash: str) -> bytes:
        if not _HASH_PATTERN.fullmatch(content_hash):
            raise ValueError("to_hash must be a lowercase SHA-256")
        if not self.history_enabled:
            raise HistoryUnavailableError("history content is redacted by vault policy")
        path = self._guard_history_target(self._history_dir / content_hash)
        if not path.is_file():
            raise HistoryUnavailableError(f"history version not found: {content_hash}")
        path = self._guard_history_target(path)
        data = path.read_bytes()
        if sha256_bytes(data) != content_hash:
            raise OperationLogError(f"history blob hash mismatch: {content_hash}")
        return data

    def write_pending(self, record: OperationRecord) -> None:
        record.validate()
        payload = _record_line(record)
        if len(payload) > _MAX_PENDING_RECORD_BYTES:
            raise OperationLogError(
                f"pending operation manifest exceeds {_MAX_PENDING_RECORD_BYTES} bytes"
            )
        pending_dir = self._guard_pending_root()
        pending_dir.mkdir(parents=True, exist_ok=True)
        self._guard_pending_root()
        target = self._guard_pending_target(self.pending_path(record.operation_id))
        _atomic_write(target, payload)

    def read_pending(self, path: Path) -> OperationRecord:
        record, _snapshot = self.read_pending_snapshot(path)
        return record

    def read_pending_snapshot(self, path: Path) -> tuple[OperationRecord, bytes]:
        """Read one bounded receipt and bind its bytes to its operation filename."""
        safe_path = self._guard_pending_target(path)
        try:
            with safe_path.open("rb") as handle:
                snapshot = handle.read(_MAX_PENDING_RECORD_BYTES + 1)
            if len(snapshot) > _MAX_PENDING_RECORD_BYTES:
                raise OperationLogError(
                    f"pending operation manifest exceeds {_MAX_PENDING_RECORD_BYTES} bytes"
                )
            payload = _strict_json_loads(snapshot.decode("ascii", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, _StrictJsonError) as exc:
            raise OperationLogError(f"invalid pending operation manifest: {safe_path}") from exc
        record = OperationRecord.from_dict(payload)
        expected_name = f"{record.operation_id}.json"
        if safe_path.name != expected_name:
            raise OperationLogError(
                "pending operation filename does not match operation_id: "
                f"expected {expected_name!r}, actual {safe_path.name!r}"
            )
        return record, snapshot

    def pending_paths(self) -> list[Path]:
        pending_dir = self._guard_pending_root()
        if not pending_dir.is_dir():
            return []
        paths: list[Path] = []
        for candidate in sorted(pending_dir.iterdir()):
            path = self._guard_pending_target(candidate)
            if _PENDING_TEMP_PATTERN.fullmatch(path.name) and path.is_file():
                continue
            if path.suffix != ".json" or not path.is_file():
                raise OperationLogError(
                    f"unexpected entry in pending operation directory: {path.name!r}"
                )
            paths.append(path)
        return paths

    def pending_path(self, operation_id: str) -> Path:
        return self._guard_pending_target(self._pending_dir / f"{operation_id}.json")

    def append_record(self, record: OperationRecord) -> bool:
        record.validate()
        self._load_tail_state()
        operations_path = self._guard_operations_path()
        try:
            existing_bytes = operations_path.read_bytes() if operations_path.is_file() else b""
        except OSError as exc:
            raise OperationLogError("failed to read the operation log before append") from exc
        existing_records = _parse_records(existing_bytes, verify_chain=True)
        current_tail = existing_records[-1] if existing_records else None
        if current_tail is not None and current_tail.operation_id == record.operation_id:
            self._tail_record = current_tail
            self._tail_hash = sha256_bytes(_record_line(current_tail))
            self._tail_loaded = True
            return False
        current_tail_hash = (
            sha256_bytes(_record_line(current_tail)) if current_tail is not None else None
        )
        chained = replace(
            record,
            prev_hash=current_tail_hash,
            format_version=_FORMAT_VERSION,
        )
        chained.validate()
        line = _record_line(chained)
        oplog_dir = self._guard_oplog_root()
        oplog_dir.mkdir(parents=True, exist_ok=True)
        self._guard_oplog_root()
        operations_path = self._guard_operations_path()
        try:
            _atomic_write(operations_path, existing_bytes + line)
        except OSError as exc:
            self._tail_loaded = False
            raise OperationLogError("failed to append the operation log") from exc
        self._tail_record = chained
        self._tail_hash = sha256_bytes(line)
        self._tail_loaded = True
        return True

    def remove_pending(self, operation_id: str) -> None:
        path = self.pending_path(operation_id)
        if not path.exists():
            return
        path = self._guard_pending_target(path)
        path.unlink()
        _durable_flush_directory(self._guard_pending_root())

    def read_records(self) -> list[OperationRecord]:
        operations_path = self._guard_operations_path()
        if not operations_path.is_file():
            return []
        self._ensure_tail_state()
        operations_path = self._guard_operations_path()
        return _parse_records(operations_path.read_bytes(), verify_chain=True)

    def latest_record_for_path(self, rel_path: str) -> OperationRecord | None:
        """Return the latest committed record for one exact vault-relative path."""
        operations_path = self._guard_operations_path()
        if not operations_path.is_file():
            return None
        self._ensure_tail_state()
        operations_path = self._guard_operations_path()
        records = _parse_records(operations_path.read_bytes(), verify_chain=True)
        path_identity = _relative_path_identity(rel_path)
        return next(
            (
                record
                for record in reversed(records)
                if _relative_path_identity(record.rel_path) == path_identity
            ),
            None,
        )

    def has_record(self, operation_id: str) -> bool:
        # Recovery queries are outside the append hot path, so a full verified scan
        # preserves idempotence without maintaining a second durable index.
        return any(record.operation_id == operation_id for record in self.read_records())

    def _ensure_tail_state(self) -> None:
        if not self._tail_loaded:
            self._load_tail_state()

    def _load_tail_state(self) -> None:
        tail = _read_tail_records(self._guard_operations_path())
        if not tail:
            self._tail_record = None
            self._tail_hash = None
            self._tail_loaded = True
            return
        tail_record, tail_line = tail[-1]
        if tail_record.format_version == 1:
            self._migrate_legacy_log()
            return
        if any(record.format_version != _FORMAT_VERSION for record, _line in tail):
            raise OperationLogError("operation log tail mixes legacy and chained records")
        expected_prev_hash = sha256_bytes(tail[-2][1]) if len(tail) == 2 else None
        if tail_record.prev_hash != expected_prev_hash:
            raise OperationLogError("operation log tail hash chain mismatch")
        self._tail_record = tail_record
        self._tail_hash = sha256_bytes(tail_line)
        self._tail_loaded = True

    def _migrate_legacy_log(self) -> None:
        operations_path = self._guard_operations_path()
        data = operations_path.read_bytes()
        legacy_records = _parse_records(data, verify_chain=False)
        if any(record.format_version != 1 for record in legacy_records):
            raise OperationLogError("operation log contains mixed legacy and chained records")
        chained_records: list[OperationRecord] = []
        previous_hash: str | None = None
        for record in legacy_records:
            chained = replace(
                record,
                prev_hash=previous_hash,
                format_version=_FORMAT_VERSION,
            )
            chained_records.append(chained)
            previous_hash = sha256_bytes(_record_line(chained))
        migrated = b"".join(_record_line(record) for record in chained_records)
        operations_path = self._guard_operations_path()
        _atomic_write(operations_path, migrated)
        _LOGGER.warning(
            "Migrated %d legacy operation records to chained format version %d",
            len(chained_records),
            _FORMAT_VERSION,
        )
        self._tail_record = chained_records[-1] if chained_records else None
        self._tail_hash = previous_hash
        self._tail_loaded = True

    def purge_history(
        self,
        now: datetime | None = None,
        *,
        preserve_hashes: set[str] | None = None,
    ) -> list[str]:
        purge_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
        if (
            self.history_enabled
            and self._last_purge_at is not None
            and purge_at - self._last_purge_at < self._purge_min_interval
        ):
            return []
        history_dir = self._guard_history_root()
        if not history_dir.is_dir():
            return []
        retained = set(preserve_hashes or ())
        if self.history_enabled:
            cutoff = purge_at - timedelta(days=self._retention_days)
            for record in self.read_records():
                timestamp = datetime.fromisoformat(record.timestamp).astimezone(UTC)
                if timestamp < cutoff:
                    continue
                if record.before_hash is not None:
                    retained.add(record.before_hash)
                retained.add(record.after_hash)
        removed: list[str] = []
        for candidate in sorted(history_dir.iterdir()):
            path = self._guard_history_target(candidate)
            if not path.is_file() or not _HASH_PATTERN.fullmatch(path.name):
                continue
            if path.name in retained:
                continue
            path = self._guard_history_target(path)
            path.unlink()
            removed.append(path.name)
        if removed:
            _durable_flush_directory(self._guard_history_root())
        self._last_purge_at = purge_at
        return removed


def _relative_path_identity(rel_path: str) -> str:
    return os.path.normcase(rel_path)


def _require_exact_record_keys(payload: dict[object, object]) -> None:
    non_string_keys = [key for key in payload if not isinstance(key, str)]
    if non_string_keys:
        raise OperationLogError(
            "operation record fields differ from schema; "
            f"missing=[], unexpected={sorted(repr(key) for key in non_string_keys)}"
        )
    actual: frozenset[str] = frozenset(key for key in payload if isinstance(key, str))
    is_v2 = "format_version" in actual or "prev_hash" in actual
    expected = _RECORD_KEYS_V2 if is_v2 else _RECORD_KEYS_V1
    if actual != expected:
        missing = sorted(str(key) for key in expected - actual)
        unexpected = sorted(str(key) for key in actual - expected)
        raise OperationLogError(
            "operation record fields differ from schema; "
            f"missing={missing}, unexpected={unexpected}"
        )
    if is_v2 and payload.get("format_version") != _FORMAT_VERSION:
        raise OperationLogError(
            f"operation record schema requires format_version {_FORMAT_VERSION}"
        )


def _record_line(record: OperationRecord) -> bytes:
    rendered = json.dumps(
        record.to_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{rendered}\n".encode("ascii")


def _parse_records(data: bytes, *, verify_chain: bool) -> list[OperationRecord]:
    try:
        text = data.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise OperationLogError("operation log must be ASCII JSONL") from exc
    records: list[OperationRecord] = []
    seen: set[str] = set()
    previous_hash: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            payload = _strict_json_loads(line)
        except (json.JSONDecodeError, _StrictJsonError) as exc:
            raise OperationLogError(f"invalid JSONL at line {line_number}") from exc
        record = OperationRecord.from_dict(payload)
        if record.operation_id in seen:
            raise OperationLogError(f"duplicate operation_id at line {line_number}")
        if verify_chain:
            if record.format_version != _FORMAT_VERSION:
                raise OperationLogError(f"legacy operation record remains at line {line_number}")
            if record.prev_hash != previous_hash:
                raise OperationLogError(f"operation hash chain mismatch at line {line_number}")
        seen.add(record.operation_id)
        records.append(record)
        previous_hash = sha256_bytes(_record_line(record))
    return records


def _read_tail_records(path: Path) -> list[tuple[OperationRecord, bytes]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            if end == 0:
                return []
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise OperationLogError("operation log does not end at a JSONL boundary")
            position = end
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count < 3:
                read_size = min(4096, position)
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            lines = b"".join(reversed(chunks)).splitlines()[-2:]
    except OSError as exc:
        raise OperationLogError("failed to read operation log tail") from exc
    records: list[tuple[OperationRecord, bytes]] = []
    for line in lines:
        try:
            payload = _strict_json_loads(line.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, _StrictJsonError) as exc:
            raise OperationLogError("invalid operation log tail record") from exc
        records.append((OperationRecord.from_dict(payload), line + b"\n"))
    return records


def _strict_json_loads(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise _StrictJsonError(f"non-finite JSON number is forbidden: {value}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_durable_write(path, data)


def _durable_flush_directory(path: Path) -> None:
    durable_flush_directory(path)
