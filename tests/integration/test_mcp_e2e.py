# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""End-to-end tests for the Datacron MCP stdio server.

Spawns ``datacron-mcp`` (the script entry point) as a subprocess and
talks to it via the official MCP client SDK. Marked ``@pytest.mark.integration``
so the unit-test run stays fast; CI runs both suites.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_DEMO_VAULT = Path(__file__).parents[1] / "fixtures" / "demo-vault"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Copy the demo vault into ``tmp_path`` for the server to read."""
    target = tmp_path / "vault"
    shutil.copytree(_DEMO_VAULT, target)
    return target


def _server_params(vault: Path, log_dir: Path) -> StdioServerParameters:
    """Build StdioServerParameters that launch ``datacron-mcp`` via this venv.

    We invoke ``sys.executable -c "from datacron.cli import mcp_entry; mcp_entry()"``
    so the subprocess always uses the same Python and installed package, with
    no dependency on a PATH lookup.
    """
    env = dict(os.environ)
    env["DATACRON_VAULT_ROOT"] = str(vault)
    env["DATACRON_READ_PATHS"] = str(vault)
    env["DATACRON_WRITE_PATHS"] = str(vault)
    env["DATACRON_LOG_DIR"] = str(log_dir)
    env["DATACRON_LOG_LEVEL"] = "WARNING"
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from datacron.cli import mcp_entry; mcp_entry()"],
        env=env,
    )


async def _open_session(vault: Path, tmp_path: Path) -> tuple[ClientSession, object]:
    """Return an initialized ClientSession + the stdio context for teardown."""
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


class TestMcpE2E:
    async def test_lists_expected_tools(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            response = await session.list_tools()
            tool_names = {t.name for t in response.tools}
            assert {
                "list_notes",
                "get_note",
                "revert_note",
                "get_note_history",
                "audit_query",
            } <= tool_names
        finally:
            await _close_session(session, streams)

    async def test_lists_expected_resources(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            response = await session.list_resources()
            uris = {str(r.uri) for r in response.resources}
            assert {
                "datacron://vault/map",
                "datacron://vault/info",
                "datacron://policy/active",
            } <= uris
        finally:
            await _close_session(session, streams)

    async def test_write_records_initialized_mcp_client_actor(
        self, vault: Path, tmp_path: Path
    ) -> None:
        rel_path = "_memory/facts/mcp-actor.md"
        session, streams = await _open_session(vault, tmp_path)
        try:
            created = await session.call_tool(
                "create_note_ai",
                {
                    "rel_path": rel_path,
                    "title": "MCP actor",
                    "body": "# MCP actor\n\nAudited through the transport.\n",
                    "origin": "ai",
                    "confidence": "high",
                    "tags": ["audit"],
                },
            )
            history = await session.call_tool(
                "get_note_history",
                {"note": rel_path, "limit": 10},
            )
        finally:
            await _close_session(session, streams)

        assert not created.isError
        assert not history.isError
        history_payload = json.loads(history.content[0].text)  # type: ignore[union-attr]
        assert history_payload["total"] == 1
        actor = history_payload["operations"][0]["actor"]
        assert actor.startswith("mcp-client:")
        assert actor != "mcp-client:unidentified"

    async def test_list_notes_tool_returns_demo_vault(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool("list_notes", {"limit": 50})
            assert not result.isError
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload["total"] == 6
            rel_paths = {n["rel_path"] for n in payload["notes"]}
            assert "welcome.md" in rel_paths
            assert "subfolder/nested-thoughts.md" in rel_paths
        finally:
            await _close_session(session, streams)

    async def test_get_note_full_returns_sandbox_wrapped_content(
        self, vault: Path, tmp_path: Path
    ) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool(
                "get_note", {"id_or_path": "welcome.md", "format": "full"}
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload["format"] == "full"
            assert payload["content"].startswith('<vault_content path="welcome.md">\n')
            assert payload["content"].endswith("</vault_content>")
        finally:
            await _close_session(session, streams)

    async def test_get_note_map_returns_headings(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool(
                "get_note", {"id_or_path": "welcome.md", "format": "map"}
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload["format"] == "map"
            assert payload["headings"]
            assert any(h["text"] == "Welcome" for h in payload["headings"])
        finally:
            await _close_session(session, streams)

    async def test_vault_info_resource(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.read_resource("datacron://vault/info")  # type: ignore[arg-type]
            text = result.contents[0].text  # type: ignore[union-attr]
            info = json.loads(text)
            assert info["note_count"] == 6
            assert info["index"]["built"] is False
        finally:
            await _close_session(session, streams)

    async def test_invalid_tool_args_return_structured_error(
        self, vault: Path, tmp_path: Path
    ) -> None:
        """Server must respond with an error result rather than crashing."""
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool(
                "get_note", {"id_or_path": "nope.md", "format": "full"}
            )
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert "error" in payload
        finally:
            await _close_session(session, streams)


def _ensure_python_runtime_compatible() -> None:
    """Skip integration tests on Python builds that lack stdio readiness."""
    if sys.platform == "win32" and sys.version_info < (3, 11):
        pytest.skip("Windows + Python < 3.11 has flaky stdio piping")


_ensure_python_runtime_compatible()
