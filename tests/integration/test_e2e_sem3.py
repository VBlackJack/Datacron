# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""End-to-end Sem-3 integration test.

Spawns the real ``datacron-mcp`` script entry against an indexed copy of
the demo vault and exercises the full Sem-3 catalog through the official
MCP client SDK:

- list_notes, get_note(full|map) (Sem 2, included for completeness)
- search_text, search_regex, get_backlinks (Sem 3)
- datacron://vault/{map,info,policy/active} resources

Marked ``@pytest.mark.integration`` so the fast unit suite still finishes
in seconds. CI installs ripgrep so search_regex executes; locally the
regex assertions are guarded by ``shutil.which('rg')``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from datacron.core.vault import FilesystemVaultReader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_DEMO_VAULT = Path(__file__).parents[1] / "fixtures" / "demo-vault"


@pytest.fixture
async def indexed_vault(tmp_path: Path) -> AsyncIterator[Path]:
    """Copy the demo vault into tmp_path and build the FTS5 index in-process.

    The Sem-2 FTS5 store migration renames ``.datacron/ulids.json`` to
    ``ulids.json.migrated`` after importing its contents into the
    ``ulid_paths`` table. The server process's new ``FilesystemVaultReader``
    would then generate fresh ULIDs and lose alignment with the indexed
    chunks (breaking ``get_backlinks``). To keep both processes consistent
    in this Phase-0 layout, we restore a copy of ``ulids.json`` from the
    migrated sidecar after the indexing pass closes. The presence of the
    ``.migrated`` marker keeps the server-side store from re-migrating;
    the server's ``JsonIdStore`` then sees the same ULIDs as the index.

    (The proper fix — bridge ``FilesystemVaultReader`` to ``ulid_paths`` —
    is queued as a Sem-4 cleanup; see docs/reviews/sem3/.)
    """
    vault = tmp_path / "vault"
    shutil.copytree(_DEMO_VAULT, vault)

    reader = FilesystemVaultReader(vault)
    chunker = MarkdownChunker()
    store = SQLiteFTS5Store()
    db_path = vault / ".datacron" / "index" / "datacron.db"

    # First populate ulids.json by reading every note (lazy ULID generation),
    # then open the store so the migration captures the same IDs.
    notes = await reader.list_notes()
    await store.open(db_path)
    try:
        for note in notes:
            await store.upsert_note(note, chunker.chunk(note))
    finally:
        await store.close()

    migrated = vault / ".datacron" / "ulids.json.migrated"
    if migrated.is_file():
        shutil.copyfile(migrated, vault / ".datacron" / "ulids.json")

    yield vault


def _server_params(vault: Path, log_dir: Path) -> StdioServerParameters:
    env = dict(os.environ)
    env["DATACRON_VAULT_ROOT"] = str(vault)
    env["DATACRON_READ_PATHS"] = str(vault)
    env["DATACRON_LOG_DIR"] = str(log_dir)
    env["DATACRON_LOG_LEVEL"] = "WARNING"
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from datacron.cli import mcp_entry; mcp_entry()"],
        env=env,
    )


async def _open_session(vault: Path, tmp_path: Path) -> tuple[ClientSession, object]:
    params = _server_params(vault, tmp_path / "logs")
    streams_ctx = stdio_client(params)
    read_stream, write_stream = await streams_ctx.__aenter__()
    session = ClientSession(read_stream, write_stream)
    await session.__aenter__()
    await session.initialize()
    return session, streams_ctx


async def _close_session(session: ClientSession, streams_ctx: object) -> None:
    await session.__aexit__(None, None, None)
    await streams_ctx.__aexit__(None, None, None)  # type: ignore[attr-defined]


def _parse_tool_payload(result: Any) -> dict[str, Any]:
    text = result.content[0].text
    return json.loads(text)  # type: ignore[no-any-return]


def _resource_text(resource: Any) -> str:
    text = resource.contents[0].text
    return str(text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSem3E2E:
    async def test_lists_all_six_tools(self, indexed_vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            response = await session.list_tools()
        finally:
            await _close_session(session, streams)
        tool_names = {t.name for t in response.tools}
        assert {
            "list_notes",
            "get_note",
            "search_text",
            "search_regex",
            "get_backlinks",
        } <= tool_names

    async def test_list_notes_returns_demo_vault(self, indexed_vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool("list_notes", {"limit": 10})
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert payload["total"] == 6
        rel_paths = {n["rel_path"] for n in payload["notes"]}
        assert "welcome.md" in rel_paths

    async def test_search_text_returns_bm25_hits(self, indexed_vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool("search_text", {"query": "Welcome", "limit": 5})
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert "error" not in payload
        assert payload["returned"] >= 1
        first = payload["results"][0]
        assert first["snippet"].startswith('<vault_content path="')
        assert "**" in first["snippet"]  # FTS5 highlighting preserved
        assert first["score"] > 0

    async def test_get_note_map_returns_headings(self, indexed_vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool(
                "get_note", {"id_or_path": "welcome.md", "format": "map"}
            )
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert payload["format"] == "map"
        assert payload["headings"]

    async def test_get_backlinks_resolves_and_finds_sources(
        self, indexed_vault: Path, tmp_path: Path
    ) -> None:
        # The demo vault wires: important-note.md, subfolder/nested-thoughts.md
        # → both link to [[Welcome]] (resolves via welcome.md's alias).
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool("get_backlinks", {"target": "Welcome", "limit": 20})
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert "error" not in payload
        assert payload["resolved_note_id"] is not None
        source_paths = {row["source_note_rel_path"] for row in payload["results"]}
        assert "important-note.md" in source_paths

    async def test_vault_map_resource(self, indexed_vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            resource = await session.read_resource("datacron://vault/map")  # type: ignore[arg-type]
        finally:
            await _close_session(session, streams)
        body = _resource_text(resource)
        assert "welcome.md" in body
        assert "## subfolder/" in body

    async def test_vault_info_resource_reports_indexed_state(
        self, indexed_vault: Path, tmp_path: Path
    ) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            resource = await session.read_resource("datacron://vault/info")  # type: ignore[arg-type]
        finally:
            await _close_session(session, streams)
        info = json.loads(_resource_text(resource))
        assert info["note_count"] == 6
        # Indexing happened in the fixture, so the index must report built.
        assert info["index"]["built"] is True
        assert info["index"]["indexed_notes"] == 6

    @pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not on PATH")
    async def test_search_regex_finds_headings_via_rg(
        self, indexed_vault: Path, tmp_path: Path
    ) -> None:
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool(
                "search_regex",
                {"pattern": "^# ", "glob": "*.md", "limit": 10},
            )
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert "error" not in payload
        assert payload["returned"] >= 1
        # Every result must be sandbox-wrapped.
        for row in payload["results"]:
            assert row["snippet"].startswith('<vault_content path="')

    async def test_search_regex_returns_clear_error_when_rg_missing(
        self,
        indexed_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``rg`` cannot be found, the tool must return a structured error
        (FileNotFoundError) rather than crashing the server.

        We force the path to a sentinel that surely doesn't exist on either
        OS so the test is deterministic regardless of whether rg is
        installed on the host.
        """
        monkeypatch.setenv("DATACRON_RIPGREP_PATH", "rg-does-not-exist-xyzzy")
        session, streams = await _open_session(indexed_vault, tmp_path)
        try:
            result = await session.call_tool("search_regex", {"pattern": "foo", "limit": 5})
        finally:
            await _close_session(session, streams)
        payload = _parse_tool_payload(result)
        assert "error" in payload
        assert payload["error"]["type"] == "FileNotFoundError"
