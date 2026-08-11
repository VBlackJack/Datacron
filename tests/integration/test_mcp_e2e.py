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

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import MCPError
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Implementation, TextResourceContents

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_DEMO_VAULT = Path(__file__).parents[1] / "fixtures" / "demo-vault"
_GET_NOTE_OUTPUT_SCHEMA_SHA256 = "dd235cf4bbb29547d0f6a294334f065422422e3d3aac10e44d7454a0544279d3"


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


async def _open_session(vault: Path, tmp_path: Path) -> tuple[Client, None]:
    """Return an initialized v2 Client plus a compatibility teardown sentinel."""
    params = _server_params(vault, tmp_path / "logs")
    session = Client(
        stdio_client(params),
        mode="auto",
        client_info=Implementation(name="datacron-tests", version="2.0"),
    )
    await session.__aenter__()
    return session, None


async def _close_session(session: Client, streams_ctx: object) -> None:
    del streams_ctx
    await session.__aexit__(None, None, None)


class TestMcpE2E:
    @pytest.mark.parametrize(
        ("mode", "expected_protocol"),
        [
            ("auto", "2026-07-28"),
            ("legacy", "2025-11-25"),
        ],
    )
    async def test_mcp_v2_stdio_modes_preserve_client_identity(
        self,
        vault: Path,
        tmp_path: Path,
        mode: str,
        expected_protocol: str,
    ) -> None:
        transport = stdio_client(_server_params(vault, tmp_path / "logs"))
        async with Client(
            transport,
            mode=mode,
            client_info=Implementation(name="bl0002-test", version="2.0"),
        ) as client:
            assert client.protocol_version == expected_protocol
            rel_path = f"_memory/facts/v2-{mode}.md"
            created = await client.call_tool(
                "create_note_ai",
                {
                    "rel_path": rel_path,
                    "title": f"V2 {mode}",
                    "body": f"# V2 {mode}\n\nTransport identity.\n",
                    "origin": "ai",
                    "tags": ["transport"],
                    "confidence": "high",
                },
            )
            history = await client.call_tool("get_note_history", {"note": rel_path})

        assert created.is_error is False
        assert history.structured_content is not None
        assert history.structured_content["operations"][0]["actor"] == (
            "mcp-client:bl0002-test/2.0"
        )

    async def test_mcp_v2_auto_stdio_preserves_read_and_error_contracts(
        self,
        vault: Path,
        tmp_path: Path,
    ) -> None:
        from mcp.types import INVALID_PARAMS

        transport = stdio_client(_server_params(vault, tmp_path / "logs"))
        async with Client(
            transport,
            mode="auto",
            client_info=Implementation(name="bl0002-read-test", version="2.0"),
        ) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == 17
            get_note_tool = next(tool for tool in tools.tools if tool.name == "get_note")
            assert get_note_tool.output_schema is not None
            encoded_schema = json.dumps(
                get_note_tool.output_schema,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert hashlib.sha256(encoded_schema).hexdigest() == _GET_NOTE_OUTPUT_SCHEMA_SHA256

            health = await client.call_tool("get_health", {})
            note = await client.call_tool("get_note", {"id_or_path": "welcome.md", "format": "map"})
            search = await client.call_tool("search_text", {"query": "Welcome", "limit": 5})
            missing = await client.call_tool(
                "get_note", {"id_or_path": "nope.md", "format": "full"}
            )
            with pytest.raises(MCPError) as resource_error:
                await client.read_resource("datacron://vault/missing")

        assert health.is_error is False
        assert health.structured_content is not None
        assert note.is_error is False
        assert note.structured_content is not None
        assert note.structured_content["headings"]
        assert search.is_error is False
        assert search.structured_content is not None
        assert search.structured_content["returned"] >= 1
        assert missing.is_error is True
        assert missing.structured_content is None
        payload = json.loads(missing.content[0].text)  # type: ignore[union-attr]
        assert payload == {
            "error": {
                "message": f"Note not found: {vault / 'nope.md'}",
                "type": "FileNotFoundError",
            }
        }
        assert resource_error.value.code == INVALID_PARAMS

    @pytest.mark.parametrize("mode", ["auto", "legacy"])
    async def test_mcp_v2_stdio_unknown_tool_uses_invalid_params(
        self,
        vault: Path,
        tmp_path: Path,
        mode: str,
    ) -> None:
        from mcp.types import INVALID_PARAMS

        transport = stdio_client(_server_params(vault, tmp_path / f"logs-{mode}"))
        async with Client(transport, mode=mode) as client:
            with pytest.raises(MCPError) as error:
                await client.call_tool("missing_tool", {})

        assert error.value.code == INVALID_PARAMS

    async def test_lists_expected_tools(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            response = await session.list_tools()
            tool_names = {t.name for t in response.tools}
            assert len(response.tools) == 17
            assert {
                "list_notes",
                "get_note",
                "delete_note_section",
                "rename_note_section",
                "patch_note_preamble",
                "revert_note",
                "get_note_history",
                "audit_query",
                "contradiction_scan",
            } <= tool_names
            get_note = next(tool for tool in response.tools if tool.name == "get_note")
            assert get_note.output_schema is not None
            encoded_schema = json.dumps(
                get_note.output_schema,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert hashlib.sha256(encoded_schema).hexdigest() == _GET_NOTE_OUTPUT_SCHEMA_SHA256
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

        assert not created.is_error
        assert not history.is_error
        history_payload = json.loads(history.content[0].text)  # type: ignore[union-attr]
        assert history_payload["total"] == 1
        actor = history_payload["operations"][0]["actor"]
        assert actor.startswith("mcp-client:")
        assert actor != "mcp-client:unidentified"

    async def test_rename_note_section_round_trips_over_stdio(
        self, vault: Path, tmp_path: Path
    ) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            renamed = await session.call_tool(
                "rename_note_section",
                {
                    "rel_path": "welcome.md",
                    "heading": "Quick links",
                    "new_heading": "Useful links",
                    "heading_level": 2,
                },
            )
            note_map = await session.call_tool(
                "get_note", {"id_or_path": "welcome.md", "format": "map"}
            )
        finally:
            await _close_session(session, streams)

        assert not renamed.is_error
        assert renamed.structured_content is not None
        assert renamed.structured_content["renamed"] == {
            "rel_path": "welcome.md",
            "old_heading": "Quick links",
            "new_heading": "Useful links",
            "level": 2,
        }
        assert renamed.structured_content["indexed"] is True
        assert not note_map.is_error
        assert note_map.structured_content is not None
        headings = note_map.structured_content["headings"]
        assert any(heading["text"] == "Useful links" for heading in headings)
        assert all(heading["text"] != "Quick links" for heading in headings)

    async def test_heading_occurrence_patches_second_duplicate_over_stdio(
        self, vault: Path, tmp_path: Path
    ) -> None:
        rel_path = "_memory/facts/stdio-heading-occurrence.md"
        session, streams = await _open_session(vault, tmp_path)
        try:
            created = await session.call_tool(
                "create_note_ai",
                {
                    "rel_path": rel_path,
                    "title": "Stdio heading occurrence",
                    "body": (
                        "# Root\n\n## Same\n\nfirstblocktoken\n\n## Same\n\nsecondblocktoken\n"
                    ),
                    "origin": "ai",
                    "confidence": "high",
                    "tags": ["integration"],
                },
            )
            before_map = await session.call_tool(
                "get_note", {"id_or_path": rel_path, "format": "map"}
            )
            assert before_map.structured_content is not None
            patched = await session.call_tool(
                "patch_note_section",
                {
                    "rel_path": rel_path,
                    "heading": "Same",
                    "new_content": "replacementblocktoken",
                    "expected_hash": before_map.structured_content["content_hash"],
                    "heading_level": 2,
                    "heading_occurrence": 2,
                },
            )
            after_map = await session.call_tool(
                "get_note", {"id_or_path": rel_path, "format": "map"}
            )
            after_full = await session.call_tool(
                "get_note", {"id_or_path": rel_path, "format": "full"}
            )
        finally:
            await _close_session(session, streams)

        assert not created.is_error
        assert not before_map.is_error
        assert not patched.is_error
        assert patched.structured_content is not None
        assert patched.structured_content["patched"]["heading_occurrence"] == 2
        assert not after_map.is_error
        assert after_map.structured_content is not None
        assert [
            heading["text"]
            for heading in after_map.structured_content["headings"]
            if heading["text"] == "Same"
        ] == ["Same", "Same"]
        assert not after_full.is_error
        assert after_full.structured_content is not None
        content = after_full.structured_content["content"]
        assert "firstblocktoken" in content
        assert "replacementblocktoken" in content
        assert "secondblocktoken" not in content

    async def test_patch_note_preamble_round_trips_over_stdio(
        self, vault: Path, tmp_path: Path
    ) -> None:
        rel_path = "_memory/facts/stdio-preamble.md"
        session, streams = await _open_session(vault, tmp_path)
        try:
            created = await session.call_tool(
                "create_note_ai",
                {
                    "rel_path": rel_path,
                    "title": "Stdio preamble",
                    "body": "oldpreambletoken\n\n# Root\n\nbodypreservedtoken\n",
                    "origin": "ai",
                    "confidence": "high",
                    "tags": ["integration"],
                },
            )
            assert created.structured_content is not None
            patched = await session.call_tool(
                "patch_note_preamble",
                {
                    "rel_path": rel_path,
                    "new_content": "newpreambletoken",
                    "expected_hash": created.structured_content["content_hash"],
                },
            )
            fetched = await session.call_tool(
                "get_note", {"id_or_path": rel_path, "format": "full"}
            )
        finally:
            await _close_session(session, streams)

        assert not created.is_error
        assert not patched.is_error
        assert patched.structured_content is not None
        assert patched.structured_content["patched"] == {"rel_path": rel_path}
        assert patched.structured_content["indexed"] is True
        assert not fetched.is_error
        assert fetched.structured_content is not None
        content = fetched.structured_content["content"]
        assert "newpreambletoken\n\n# Root" in content
        assert "oldpreambletoken" not in content
        assert "bodypreservedtoken" in content

    async def test_list_notes_tool_returns_demo_vault(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool("list_notes", {"limit": 50})
            assert not result.is_error
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload["total"] == 6
            rel_paths = {n["rel_path"] for n in payload["notes"]}
            assert "welcome.md" in rel_paths
            assert "subfolder/nested-thoughts.md" in rel_paths
        finally:
            await _close_session(session, streams)

    async def test_contradiction_scan_passes_output_schema_validation(
        self, vault: Path, tmp_path: Path
    ) -> None:
        """Scan must survive low-level structured-output validation end to end.

        Serialization materializes absent optional keys as None, so this only
        holds when every optional output key is nullable; the in-process unit
        tests bypass that validation layer entirely.
        """
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.call_tool("contradiction_scan", {"mode": "scan"})
            assert not result.is_error, result.content
            assert result.structured_content is not None
            assert result.structured_content["schema_version"] == 2
            assert result.structured_content["mode"] == "scan"
            assert isinstance(result.structured_content["candidate_count"], int)
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
            assert not result.is_error
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
            assert not result.is_error
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload["format"] == "map"
            assert payload["headings"]
            assert any(h["text"] == "Welcome" for h in payload["headings"])
        finally:
            await _close_session(session, streams)

    async def test_vault_info_resource(self, vault: Path, tmp_path: Path) -> None:
        session, streams = await _open_session(vault, tmp_path)
        try:
            result = await session.read_resource("datacron://vault/info")
            resource = result.contents[0]
            assert isinstance(resource, TextResourceContents)
            text = resource.text
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
            assert result.is_error is True
            assert result.structured_content is None
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            assert payload == {
                "error": {
                    "message": f"Note not found: {vault / 'nope.md'}",
                    "type": "FileNotFoundError",
                }
            }
        finally:
            await _close_session(session, streams)


def _ensure_python_runtime_compatible() -> None:
    """Skip integration tests on Python builds that lack stdio readiness."""
    if sys.platform == "win32" and sys.version_info < (3, 11):
        pytest.skip("Windows + Python < 3.11 has flaky stdio piping")


_ensure_python_runtime_compatible()
