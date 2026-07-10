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
"""Blocking properties for alert-only, resumable primary-byte scrubbing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from datacron.core.config import DEFAULT_SCRUB_CANARIES, Settings, VaultConfig
from datacron.core.durability import DurabilityStatus, WritePolicy
from datacron.core.frontmatter import serialize
from datacron.core.logger import configure_logging, shutdown_logging
from datacron.core.scope import SingleTenantVaultScope
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.mcp.health import build_health
from datacron.mcp.server import build_app
from datacron.scrubber import (
    ScrubState,
    initialize_canaries,
    read_scrub_state,
    run_integrity_scrub,
)

pytestmark = pytest.mark.invariants

_SUPPORTED = DurabilityStatus(backend="property-supported", directory_flush_supported=True)


def _settings(
    vault: Path,
    *,
    scrub_canaries: dict[str, str] | None = None,
    scrub_notes_per_second: float = 1_000_000.0,
    scrub_mebibytes_per_second: float = 1_000_000.0,
    scrub_max_duration_seconds: float = 100.0,
    scrub_checkpoint_interval_notes: int = 1,
) -> Settings:
    return Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        log_dir=vault.parent / "logs",
        max_result_count=100,
        max_result_tokens=100_000,
        scrub_canaries=(dict(DEFAULT_SCRUB_CANARIES) if scrub_canaries is None else scrub_canaries),
        scrub_notes_per_second=scrub_notes_per_second,
        scrub_mebibytes_per_second=scrub_mebibytes_per_second,
        scrub_max_duration_seconds=scrub_max_duration_seconds,
        scrub_checkpoint_interval_notes=scrub_checkpoint_interval_notes,
    )


def _note(note_id: str, body: str) -> bytes:
    return serialize(
        {
            "id": note_id,
            "title": "Scrub fixture",
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "tags": ["scrubber"],
        },
        body,
    ).encode("utf-8")


async def _build(vault: Path, settings: Settings) -> None:
    await rebuild_index_atomic(vault, settings, VaultConfig())


async def _open(vault: Path) -> SQLiteFTS5Store:
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db", read_only=True)
    return store


def _scope_policy(
    vault: Path,
    settings: Settings,
) -> tuple[SingleTenantVaultScope, WritePolicy]:
    return SingleTenantVaultScope(vault, settings), WritePolicy(settings, _SUPPORTED)


@pytest.mark.parametrize("mutation", ["flip", "truncate", "nul_padding"])
async def test_prop_scrub_detects_corruption(tmp_path: Path, mutation: str) -> None:
    """Flip, truncation, and NUL padding alert without repairing note or index."""
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "target.md"
    original = _note("01J00000000000000000000081", "# Target\n\nOriginal body.\n")
    target.write_bytes(original)
    settings = _settings(vault)
    await _build(vault, settings)
    scope, policy = _scope_policy(vault, settings)
    initialize_canaries(vault, settings, scope, policy)

    if mutation == "flip":
        changed = bytearray(original)
        changed[-3] ^= 0x01
        corrupted = bytes(changed)
    elif mutation == "truncate":
        corrupted = original[: len(original) // 2]
    else:
        corrupted = original + (b"\x00" * 32)
    target.write_bytes(corrupted)
    db_path = vault / ".datacron" / "index" / "datacron.db"
    db_before = (hashlib.sha256(db_path.read_bytes()).hexdigest(), db_path.stat().st_mtime_ns)

    store = await _open(vault)
    shutdown_logging()
    configure_logging(settings)
    try:
        state = await run_integrity_scrub(vault, settings, scope, policy, store)
    finally:
        await store.close()
        shutdown_logging()

    note_anomalies = [item for item in state.anomalies if item.source == "note"]
    assert state.completed
    assert len(note_anomalies) == 1
    assert note_anomalies[0].rel_path == "target.md"
    assert note_anomalies[0].kind == (
        "nul_bytes_hash_mismatch" if mutation == "nul_padding" else "content_hash_mismatch"
    )
    assert target.read_bytes() == corrupted
    db_after = (hashlib.sha256(db_path.read_bytes()).hexdigest(), db_path.stat().st_mtime_ns)
    assert db_after == db_before
    log_text = "\n".join(
        path.read_text(encoding="utf-8") for path in settings.log_dir.glob("*.log")
    )
    assert "SCRUBBER ALERT" in log_text
    assert "path=target.md" in log_text


async def test_prop_scrub_no_false_positive(tmp_path: Path) -> None:
    """Healthy exact bytes, including mixed EOL, produce no scrub anomaly."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "lf.md").write_bytes(_note("01J00000000000000000000082", "# LF\n\nExact.\n"))
    mixed = _note("01J00000000000000000000083", "# Mixed\n\nFirst.\nSecond.\n")
    mixed = mixed.replace(b"\n", b"\r\n", 1)
    (vault / "mixed.md").write_bytes(mixed)
    settings = _settings(vault)
    await _build(vault, settings)
    scope, policy = _scope_policy(vault, settings)
    initialize_canaries(vault, settings, scope, policy)

    store = await _open(vault)
    try:
        state = await run_integrity_scrub(vault, settings, scope, policy, store)
        read_only_settings = settings.model_copy(update={"read_only": True})
        app = build_app(
            settings=read_only_settings,
            vault_root=vault,
            store=store,
            durability_status=_SUPPORTED,
        )
        health = await build_health(app)
    finally:
        await store.close()

    scrubber = cast("dict[str, Any]", health["scrubber"])
    assert state.completed
    assert state.anomalies == ()
    assert scrubber["status"] == "complete"
    assert scrubber["anomalies_count"] == 0
    assert scrubber["coverage"] == {
        "checked_notes": 2,
        "total_notes": 2,
        "fraction": 1.0,
        "complete": True,
    }


async def test_prop_scrub_resumes(tmp_path: Path) -> None:
    """A checkpointed interruption resumes the same pass without duplicate reads."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for ordinal, name in enumerate(("a.md", "b.md", "c.md"), start=84):
        (vault / name).write_bytes(_note(f"01J000000000000000000000{ordinal}", f"# {name}\n"))
    settings = _settings(vault, scrub_checkpoint_interval_notes=1)
    await _build(vault, settings)
    scope, policy = _scope_policy(vault, settings)
    initialize_canaries(vault, settings, scope, policy)
    observed: list[str] = []

    def interrupt(point: str, state: ScrubState) -> None:
        if point == "after_checkpoint" and state.checked_notes == 1:
            raise RuntimeError("simulated scrub stop")

    store = await _open(vault)
    try:
        with pytest.raises(RuntimeError, match="simulated scrub stop"):
            await run_integrity_scrub(
                vault,
                settings,
                scope,
                policy,
                store,
                fault_injector=interrupt,
                note_observer=observed.append,
            )
        interrupted = read_scrub_state(vault, settings, scope)
        assert interrupted is not None
        assert interrupted.checked_notes == 1
        resumed = await run_integrity_scrub(
            vault,
            settings,
            scope,
            policy,
            store,
            note_observer=observed.append,
        )
    finally:
        await store.close()

    assert resumed.completed
    assert resumed.pass_id == interrupted.pass_id
    assert observed == ["a.md", "b.md", "c.md"]
    assert len(set(observed)) == 3
    assert resumed.anomalies == ()


async def test_prop_canary_detects_regression(tmp_path: Path) -> None:
    """An altered canary is critical evidence and is never recreated or repaired."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note_path = vault / "healthy.md"
    note_bytes = _note("01J00000000000000000000087", "# Healthy\n")
    note_path.write_bytes(note_bytes)
    settings = _settings(vault)
    await _build(vault, settings)
    scope, policy = _scope_policy(vault, settings)
    initialize_canaries(vault, settings, scope, policy)
    canary = vault / settings.scrub_canary_dir / "exact-byte-lf.md"
    corrupted = canary.read_bytes() + b"corruption\x00"
    canary.write_bytes(corrupted)

    store = await _open(vault)
    try:
        state = await run_integrity_scrub(vault, settings, scope, policy, store)
        app = build_app(
            settings=settings.model_copy(update={"read_only": True}),
            vault_root=vault,
            store=store,
            durability_status=_SUPPORTED,
        )
        health = await build_health(app)
    finally:
        await store.close()

    canary_anomalies = [item for item in state.anomalies if item.source == "canary"]
    assert len(canary_anomalies) == 1
    assert canary_anomalies[0].kind == "canary_nul_hash_mismatch"
    assert canary.read_bytes() == corrupted
    assert note_path.read_bytes() == note_bytes
    assert health["status"] == "critical"


async def test_prop_io_budget_respected(tmp_path: Path) -> None:
    """Configured pacing and duration stop before another note starts."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for ordinal, name in enumerate(("a.md", "b.md", "c.md"), start=88):
        (vault / name).write_bytes(_note(f"01J000000000000000000000{ordinal}", f"# {name}\n"))
    settings = _settings(
        vault,
        scrub_canaries={"canary.md": "known\n"},
        scrub_notes_per_second=2.0,
        scrub_mebibytes_per_second=1_000_000.0,
        scrub_max_duration_seconds=1.1,
    )
    await _build(vault, settings)
    scope, policy = _scope_policy(vault, settings)
    initialize_canaries(vault, settings, scope, policy)
    observed: list[str] = []

    class FakeTime:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, delay: float) -> None:
            self.sleeps.append(delay)
            self.now += delay

    fake = FakeTime()
    store = await _open(vault)
    try:
        state = await run_integrity_scrub(
            vault,
            settings,
            scope,
            policy,
            store,
            clock=fake.monotonic,
            sleeper=fake.sleep,
            note_observer=observed.append,
        )
    finally:
        await store.close()

    assert observed == ["a.md", "b.md"]
    assert not state.completed
    assert state.checked_notes == 2
    assert fake.now == pytest.approx(1.5)
    assert sum(fake.sleeps) == pytest.approx(1.5)
