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
"""FDL-001 invariants for non-persistent MCP reads and index repair."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest
from mcp.client import Client
from mcp.types import Implementation

from datacron.core.config import Settings
from datacron.core.durability import DurabilityStatus
from datacron.core.vault import JsonIdStore, build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.mcp.security_manifest import READ_ONLY_TOOL_NAMES
from datacron.mcp.server import DatacronApp, build_app, create_server

pytestmark = pytest.mark.invariants

_CHUNK_MAX_TOKENS: Final[int] = 800
_MAX_RESULT_COUNT: Final[int] = 100
_MAX_RESULT_TOKENS: Final[int] = 100_000
_MTIME_STEP_NS: Final[int] = 2_000_000_000
_STABLE_TERM: Final[str] = "stableauthorityterm"
_LEGACY_TERM: Final[str] = "legacyrepairterm"
_CURRENT_TERM: Final[str] = "currentrepairterm"
_SUPPORTED: Final[DurabilityStatus] = DurabilityStatus(
    backend="fdl-property-supported",
    directory_flush_supported=True,
)

READ_ONLY_CALLS: Final[Mapping[str, Mapping[str, object]]] = MappingProxyType(
    {
        "list_notes": MappingProxyType({"limit": _MAX_RESULT_COUNT}),
        "get_note": MappingProxyType({"id_or_path": "source.md", "format": "full"}),
        "search_text": MappingProxyType({"query": _STABLE_TERM, "limit": 20}),
        "search_regex": MappingProxyType({"pattern": _STABLE_TERM, "limit": 20}),
        "get_backlinks": MappingProxyType({"target": "Target", "limit": 20}),
        "contradiction_scan": MappingProxyType({"mode": "scan", "detail": "summary"}),
        "get_health": MappingProxyType({"detail": "summary"}),
        "get_note_history": MappingProxyType({"note": "source.md", "limit": 20}),
        "audit_query": MappingProxyType({"limit": 20}),
    }
)

_REPAIR_CALLS: Final[Mapping[str, Mapping[str, object]]] = MappingProxyType(
    {
        "search_text": MappingProxyType({"query": _CURRENT_TERM, "limit": 20}),
        "search_regex": MappingProxyType({"pattern": _CURRENT_TERM, "limit": 20}),
        "get_backlinks": MappingProxyType({"target": "Target", "limit": 20}),
        "list_notes": MappingProxyType({"limit": _MAX_RESULT_COUNT}),
        "contradiction_scan": MappingProxyType({"mode": "scan", "detail": "summary"}),
    }
)

_MIXED_READ_SEQUENCES: Final[tuple[tuple[str, ...], ...]] = (
    (
        "list_notes",
        "search_text",
        "get_backlinks",
        "get_health",
        "get_note_history",
    ),
    (
        "get_note",
        "search_regex",
        "contradiction_scan",
        "audit_query",
    ),
)

_AuthorityState = tuple[bool, str, int, int]
_AuthoritySnapshot = dict[str, _AuthorityState]


def _settings(vault: Path) -> Settings:
    """Return a writable MCP configuration whose reader must still be non-persistent."""
    return Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        log_dir=vault.parent / "logs",
        max_result_count=_MAX_RESULT_COUNT,
        max_result_tokens=_MAX_RESULT_TOKENS,
        repair_min_interval_seconds=0.0,
    )


def _write_note(vault: Path, rel_path: str, content: str) -> Path:
    """Write one setup note without frontmatter identity."""
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _write_base_notes(vault: Path) -> None:
    """Create the small linked corpus shared by fresh and stale fixtures."""
    _write_note(vault, "target.md", "# Target\n\nTarget authority note.\n")
    _write_note(
        vault,
        "source.md",
        f"# Source\n\n[[Target]] {_STABLE_TERM} {_LEGACY_TERM}.\n",
    )
    _write_note(vault, "touched.md", "# Touched\n\nContent remains byte-identical.\n")
    _write_note(vault, "deleted.md", "# Deleted\n\nThis note becomes stale.\n")
    _write_note(vault, "excluded/hidden.md", "# Hidden\n\nThis route becomes excluded.\n")


async def _build_initial_index(vault: Path) -> Path:
    """Build an index through the writable reader used by CLI indexing."""
    db_path = vault / ".datacron" / "index" / "datacron.db"
    reader = build_configured_reader(vault)
    store = SQLiteFTS5Store()
    await store.open(db_path)
    try:
        stats = await reconcile(
            store,
            reader,
            MarkdownChunker(max_tokens=_CHUNK_MAX_TOKENS),
            mtime_gate=True,
        )
    finally:
        await store.close()
    assert stats["reindexed_notes"] == 5
    return db_path


def _move_mapping_to_migrated(vault: Path, rel_path: str) -> None:
    """Create the synthetic migrated-only identity branch required by FDL-001."""
    sidecar = vault / ".datacron" / "ulids.json"
    migrated = vault / ".datacron" / "ulids.json.migrated"
    primary_data = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(primary_data, dict):
        raise AssertionError("ULID sidecar must be a JSON object")
    note_id = primary_data.pop(rel_path)
    if not isinstance(note_id, str):
        raise AssertionError("ULID mapping must be a string")
    sidecar.write_text(
        json.dumps(primary_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    migrated.write_text(
        json.dumps({rel_path: note_id}, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _advance_mtime(path: Path, baseline_ns: int) -> None:
    """Move one file mtime forward by a deterministic, observable interval."""
    stat = path.stat()
    next_mtime_ns = max(stat.st_mtime_ns, baseline_ns) + _MTIME_STEP_NS
    os.utime(path, ns=(stat.st_atime_ns, next_mtime_ns))


async def _prepare_vault(tmp_path: Path, *, stale: bool) -> tuple[Path, Path]:
    """Return an indexed vault, optionally with four independent stale conditions."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_base_notes(vault)
    db_path = await _build_initial_index(vault)
    _move_mapping_to_migrated(vault, "source.md")
    if not stale:
        return vault, db_path

    touched = vault / "touched.md"
    touched_baseline = touched.stat().st_mtime_ns
    _advance_mtime(touched, touched_baseline)

    source = vault / "source.md"
    source_baseline = source.stat().st_mtime_ns
    source.write_text(
        f"# Source\n\n[[Target]] {_STABLE_TERM} {_CURRENT_TERM}.\n",
        encoding="utf-8",
    )
    _advance_mtime(source, source_baseline)

    (vault / "deleted.md").unlink()
    (vault / ".datacron" / "VAULT.yaml").write_text(
        "excluded_folders:\n  - excluded\n",
        encoding="utf-8",
    )
    return vault, db_path


def _file_state(path: Path) -> _AuthorityState:
    """Return existence, digest, size, and mtime for one authority path."""
    if not path.is_file():
        return False, "-", -1, -1
    raw = path.read_bytes()
    stat = path.stat()
    return True, hashlib.sha256(raw).hexdigest(), stat.st_size, stat.st_mtime_ns


def _authority_snapshot(vault: Path) -> _AuthoritySnapshot:
    """Capture all FDL authorities except the intentionally repairable index."""
    snapshot: _AuthoritySnapshot = {}
    for path in sorted(item for item in vault.rglob("*.md") if item.is_file()):
        snapshot[path.relative_to(vault).as_posix()] = _file_state(path)

    for rel_path in (
        ".datacron/ulids.json",
        ".datacron/ulids.json.migrated",
        ".datacron/ulids.json.tmp",
    ):
        snapshot[rel_path] = _file_state(vault / rel_path)

    for rel_dir in (".datacron/oplog", ".datacron/history"):
        directory = vault / rel_dir
        if not directory.is_dir():
            snapshot[f"{rel_dir}/"] = (False, "-", -1, -1)
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            snapshot[path.relative_to(vault).as_posix()] = _file_state(path)
    return snapshot


def _install_write_spy(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Fail closed even when an MCP implementation translates the raised exception."""
    calls: list[Path] = []

    def forbidden_write(store: JsonIdStore, data: dict[str, str]) -> None:
        del data
        calls.append(store.path)
        raise AssertionError("non-mutating MCP call invoked JsonIdStore._write_sync")

    monkeypatch.setattr(JsonIdStore, "_write_sync", forbidden_write)
    return calls


def _build_mcp_app(vault: Path) -> DatacronApp:
    """Build the production MCP wiring with a writable repair index."""
    return build_app(
        settings=_settings(vault),
        vault_root=vault,
        store=SQLiteFTS5Store(),
        durability_status=_SUPPORTED,
    )


async def _call_public_tool(
    client: Client,
    tool_name: str,
    calls: Mapping[str, Mapping[str, object]],
) -> None:
    """Invoke one registered MCP tool and require a successful public result."""
    result = await client.call_tool(tool_name, dict(calls[tool_name]))
    assert not result.is_error, result.content


def test_read_only_call_manifest_is_complete() -> None:
    """Keep the executable call inventory equal to the security manifest."""
    assert set(READ_ONLY_CALLS) == set(READ_ONLY_TOOL_NAMES)
    mixed_tools = {tool_name for sequence in _MIXED_READ_SEQUENCES for tool_name in sequence}
    assert len(_MIXED_READ_SEQUENCES) == 2
    assert mixed_tools == set(READ_ONLY_TOOL_NAMES)
    assert sum(map(len, _MIXED_READ_SEQUENCES)) == len(mixed_tools)
    assert set(_REPAIR_CALLS) == {
        "search_text",
        "search_regex",
        "get_backlinks",
        "list_notes",
        "contradiction_scan",
    }


@pytest.mark.parametrize("tool_name", sorted(READ_ONLY_CALLS))
async def test_fresh_read_tool_preserves_every_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    """Each singleton read leaves fresh vault, sidecars, oplog, and index untouched."""
    vault, db_path = await _prepare_vault(tmp_path, stale=False)
    app = _build_mcp_app(vault)
    async with Client(
        create_server(app),
        mode="auto",
        client_info=Implementation(name="fdl-authority-tests", version="2.0"),
    ) as client:
        before_authorities = _authority_snapshot(vault)
        before_generation = await app.store.get_generation()
        before_db_mtime = db_path.stat().st_mtime_ns
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()
        write_calls = _install_write_spy(monkeypatch)

        await _call_public_tool(client, tool_name, READ_ONLY_CALLS)

        after_generation = await app.store.get_generation()
        after_db_mtime = db_path.stat().st_mtime_ns
        after_authorities = _authority_snapshot(vault)
        assert write_calls == []
        assert after_authorities == before_authorities
        assert after_generation == before_generation
        assert after_db_mtime == before_db_mtime
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()


@pytest.mark.parametrize("tool_names", _MIXED_READ_SEQUENCES)
async def test_fresh_mixed_read_sequence_preserves_every_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_names: tuple[str, ...],
) -> None:
    """Two mixed sequences collectively exercise every read without persistent effects."""
    vault, db_path = await _prepare_vault(tmp_path, stale=False)
    app = _build_mcp_app(vault)
    async with Client(
        create_server(app),
        mode="auto",
        client_info=Implementation(name="fdl-authority-tests", version="2.0"),
    ) as client:
        before_authorities = _authority_snapshot(vault)
        before_generation = await app.store.get_generation()
        before_db_mtime = db_path.stat().st_mtime_ns
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()
        write_calls = _install_write_spy(monkeypatch)

        for tool_name in tool_names:
            await _call_public_tool(client, tool_name, READ_ONLY_CALLS)

        after_generation = await app.store.get_generation()
        after_db_mtime = db_path.stat().st_mtime_ns
        after_authorities = _authority_snapshot(vault)
        assert write_calls == []
        assert after_authorities == before_authorities
        assert after_generation == before_generation
        assert after_db_mtime == before_db_mtime
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()


@pytest.mark.parametrize("tool_name", sorted(_REPAIR_CALLS))
async def test_stale_repair_tool_converges_only_the_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    """Each repair singleton converges stale rows without mutating vault authorities."""
    vault, _db_path = await _prepare_vault(tmp_path, stale=True)
    app = _build_mcp_app(vault)
    async with Client(
        create_server(app),
        mode="auto",
        client_info=Implementation(name="fdl-authority-tests", version="2.0"),
    ) as client:
        before_authorities = _authority_snapshot(vault)
        before_generation = await app.store.get_generation()
        before_index = await app.store.list_indexed_notes_with_mtime()
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()
        write_calls = _install_write_spy(monkeypatch)

        await _call_public_tool(client, tool_name, _REPAIR_CALLS)

        after_index = await app.store.list_indexed_notes_with_mtime()
        after_generation = await app.store.get_generation()
        after_authorities = _authority_snapshot(vault)
        current_results = await app.store.search(_CURRENT_TERM, limit=20)
        legacy_results = await app.store.search(_LEGACY_TERM, limit=20)

        source = vault / "source.md"
        touched = vault / "touched.md"
        assert write_calls == []
        assert after_authorities == before_authorities
        assert after_generation == before_generation + 1
        assert before_index["source.md"][1] != after_index["source.md"][1]
        assert after_index["source.md"][1] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert after_index["source.md"][2] == source.stat().st_mtime_ns
        assert before_index["touched.md"][1] == after_index["touched.md"][1]
        assert before_index["touched.md"][2] != after_index["touched.md"][2]
        assert after_index["touched.md"][2] == touched.stat().st_mtime_ns
        assert "deleted.md" not in after_index
        assert "excluded/hidden.md" not in after_index
        assert any(result.chunk.note_rel_path == "source.md" for result in current_results)
        assert all(result.chunk.note_rel_path != "source.md" for result in legacy_results)
        assert not (vault / ".datacron" / "ulids.json.tmp").exists()
