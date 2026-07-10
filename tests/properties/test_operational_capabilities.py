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
"""Invariant properties for operational health and certified write policy."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from datacron import __version__
from datacron.core.config import Settings
from datacron.core.durability import (
    DurabilityStatus,
    DurabilityUnavailableError,
)
from datacron.core.frontmatter import serialize
from datacron.core.logger import configure_logging, shutdown_logging
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES, READ_ONLY_TOOL_NAMES
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools import (
    _append_journal_impl,
    _create_note_ai_impl,
    _get_health_impl,
    _patch_note_section_impl,
    _revert_note_impl,
    _search_text_impl,
    _set_frontmatter_impl,
)
from datacron.reliability import scan_vault_read_only

pytestmark = pytest.mark.invariants

_NOTE_A = "01J00000000000000000000061"
_NOTE_B = "01J00000000000000000000062"
_SUPPORTED = DurabilityStatus(backend="property-supported", directory_flush_supported=True)
_UNSUPPORTED = DurabilityStatus(
    backend="property-no-directory-flush",
    directory_flush_supported=False,
)


def _write_note(
    vault: Path,
    rel_path: str,
    note_id: str,
    title: str,
    body: str,
    *,
    mixed_eol: bool = False,
) -> None:
    raw = serialize(
        {
            "id": note_id,
            "title": title,
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "tags": ["lot6"],
        },
        body,
    )
    if mixed_eol:
        raw = raw.replace("\n", "\r\n", 1)
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw.encode("utf-8"))


def _settings(
    vault: Path,
    *,
    read_only: bool = False,
    durability: str = "best-effort",
    log_dir: Path | None = None,
) -> Settings:
    return Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        read_only=read_only,
        durability=durability,
        log_dir=log_dir or vault.parent / "logs",
        max_result_count=100,
        max_result_tokens=100_000,
    )


async def _build_index(vault: Path) -> Path:
    db_path = vault / ".datacron" / "index" / "datacron.db"
    store = SQLiteFTS5Store()
    await store.open(db_path)
    app = build_app(
        settings=_settings(vault),
        vault_root=vault,
        store=store,
        durability_status=_SUPPORTED,
    )
    try:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    finally:
        await store.close()
    return db_path


def _snapshot(vault: Path) -> dict[str, tuple[str, int, int]]:
    snapshot: dict[str, tuple[str, int, int]] = {}
    for path in sorted(item for item in vault.rglob("*") if item.is_file()):
        stat = path.stat()
        snapshot[path.relative_to(vault).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.st_mtime_ns,
            stat.st_size,
        )
    return snapshot


def _build_read_only_app(vault: Path) -> tuple[DatacronApp, SQLiteFTS5Store]:
    store = SQLiteFTS5Store()
    app = build_app(
        settings=_settings(vault, read_only=True),
        vault_root=vault,
        store=store,
        durability_status=_SUPPORTED,
    )
    return app, store


async def test_prop_read_only_blocks_writes(tmp_path: Path) -> None:
    """Certified mode removes mutators and leaves notes plus sidecar byte-identical."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "note.md",
        _NOTE_A,
        "Read-only target",
        "# Read-only target\n\nold-search-token\n\n## Journal\n\nStart.\n",
    )
    await _build_index(vault)
    target = vault / "note.md"
    target.write_bytes(target.read_bytes() + b"\nchanged-after-index\n")
    before = _snapshot(vault)
    app, _store = _build_read_only_app(vault)
    server = create_server(app)
    low_level = cast("Any", server)._mcp_server
    async with low_level.lifespan(low_level):
        live_tools = {tool.name for tool in await server.list_tools()}
        results = [
            await _create_note_ai_impl(
                app,
                rel_path="new.md",
                title="Denied",
                body="# Denied\n",
                origin="ai",
                confidence="high",
                tags=["lot6"],
            ),
            await _append_journal_impl(
                app,
                rel_path="note.md",
                heading="Journal",
                entry="Denied entry.",
            ),
            await _set_frontmatter_impl(app, rel_path="note.md", confidence="low"),
            await _patch_note_section_impl(
                app,
                rel_path="note.md",
                heading="Journal",
                new_content="Denied patch.",
            ),
            await _revert_note_impl(app, note="note.md", to_hash="0" * 64),
        ]
        search = await _search_text_impl(app, query="old-search-token", limit=20)
        health = await _get_health_impl(app)
    after = _snapshot(vault)

    assert live_tools == set(READ_ONLY_TOOL_NAMES)
    assert live_tools.isdisjoint(MUTATING_TOOL_NAMES)
    assert all(result["error"]["type"] == "ReadOnlyModeError" for result in results)
    assert search["results"]
    assert health["read_only"] is True
    assert health["index"]["consistent_with_vault"] is False
    assert before == after
    assert not (vault / "new.md").exists()


async def test_prop_health_reports_truth(tmp_path: Path) -> None:
    """Every health counter and rollup equals independently observed ground truth."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "a.md",
        _NOTE_A,
        "Health A",
        "# Health A\n\nSee [[missing-health-target]].\n",
        mixed_eol=True,
    )
    _write_note(vault, "b.md", _NOTE_B, "Health B", "# Health B\n\nStable.\n")
    db_path = await _build_index(vault)
    store = SQLiteFTS5Store()
    await store.open(db_path, read_only=True)
    app = build_app(
        settings=_settings(vault, read_only=True),
        vault_root=vault,
        store=store,
        durability_status=_SUPPORTED,
    )
    before = _snapshot(vault)
    try:
        health = await _get_health_impl(app)
        scan = scan_vault_read_only(vault)
        notes = await app.vault_reader.list_notes()
        indexed = await app.store.list_indexed_notes()
        stats = await app.store.stats()
    finally:
        await store.close()
    after = _snapshot(vault)

    expected_vault_digest = hashlib.sha256()
    for note in sorted(notes, key=lambda item: item.rel_path):
        expected_vault_digest.update(note.rel_path.encode("utf-8"))
        expected_vault_digest.update(b"\x00")
        expected_vault_digest.update(note.content_hash.encode("ascii"))
        expected_vault_digest.update(b"\n")
    expected_index_digest = hashlib.sha256()
    for rel_path, (note_id, content_hash) in sorted(indexed.items()):
        expected_index_digest.update(rel_path.encode("utf-8"))
        expected_index_digest.update(b"\x00")
        expected_index_digest.update(note_id.encode("ascii"))
        expected_index_digest.update(b"\x00")
        expected_index_digest.update(content_hash.encode("ascii"))
        expected_index_digest.update(b"\n")

    assert before == after
    assert health["status"] == "degraded"
    assert health["server_version"] == __version__
    assert health["index"]["notes_count"] == stats.note_count == scan.notes_count
    assert health["index"]["vault_notes_count"] == scan.notes_count
    assert health["index"]["staleness_seconds"] == 0.0
    assert health["index"]["generation"] == 1
    assert health["index"]["generation_hash"] == expected_index_digest.hexdigest()
    assert health["index"]["hash_divergences"] == 0
    assert health["integrity"] == {
        "notes_count": scan.notes_count,
        "id_mismatches": len(scan.id_violations),
        "broken_wikilinks": len(scan.broken_wikilinks),
        "mixed_eol_notes": len(scan.mixed_eol_notes),
        "supersedes_cycles": len(scan.supersedes_cycles),
        "frontmatter_parse_errors": len(scan.parse_errors),
    }
    assert health["integrity"]["broken_wikilinks"] == 1
    assert health["integrity"]["mixed_eol_notes"] == 1
    assert health["vault_checksum"]["value"] == expected_vault_digest.hexdigest()
    assert health["scrubber"] == {
        "status": "not_run",
        "last_scrub": None,
        "pass_id": None,
        "index_generation": None,
        "coverage": {
            "checked_notes": 0,
            "total_notes": scan.notes_count,
            "fraction": 0.0,
            "complete": False,
        },
        "checked_bytes": 0,
        "anomalies_count": 0,
        "anomalies": [],
        "canaries": {"checked": 0, "total": 2, "healthy": False},
    }
    assert health["invariants"]["summary"] == {
        "proven": 13,
        "baseline_tracked": 2,
        "deferred": 0,
    }


async def test_prop_strict_mode_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported strict mode blocks before disk; best-effort writes with warning."""
    strict_vault = tmp_path / "strict-vault"
    strict_vault.mkdir()
    strict_app = build_app(
        settings=_settings(strict_vault, durability="strict"),
        vault_root=strict_vault,
        durability_status=_UNSUPPORTED,
    )
    with pytest.raises(DurabilityUnavailableError, match="strict durability refuses writes"):
        await strict_app.vault_writer.write_note_atomic(
            "blocked.md",
            "# Blocked\n",
            overwrite=False,
        )
    assert not (strict_vault / "blocked.md").exists()

    best_vault = tmp_path / "best-effort-vault"
    best_vault.mkdir()
    log_dir = tmp_path / "durability-logs"
    best_settings = _settings(best_vault, durability="best-effort", log_dir=log_dir)
    shutdown_logging()
    configure_logging(best_settings)
    monkeypatch.setattr(
        "datacron.core.vault_writer._fsync_directory",
        lambda _path: False,
    )
    best_app = build_app(
        settings=best_settings,
        vault_root=best_vault,
        durability_status=_UNSUPPORTED,
    )
    await best_app.vault_writer.write_note_atomic(
        "accepted.md",
        "# Accepted\n",
        overwrite=False,
    )
    shutdown_logging()
    logs = "\n".join(path.read_text(encoding="utf-8") for path in log_dir.glob("*.log"))

    assert (best_vault / "accepted.md").read_text(encoding="utf-8") == "# Accepted\n"
    assert "BEST-EFFORT DURABILITY" in logs
    assert "property-no-directory-flush" in logs
