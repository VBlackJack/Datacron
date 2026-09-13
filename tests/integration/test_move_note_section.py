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
"""Section move preview, durable CAS and replay integration."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationJournal
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import build_app, create_server
from datacron.mcp.tools.section_move import _move_note_section_impl


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
async def test_preview_commit_replay_and_index(tmp_path: Path, eol: str) -> None:
    body = "# Root\n\n## Active\n\n### Item ##\n\nDone\n\n#### History\nkept\n\n## Archive\n\n"
    raw = (
        ("\ufeff" + serialize({"id": "01J00000000000000000000091"}, body))
        .replace("\n", eol)
        .encode()
    )
    path = tmp_path / "note.md"
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        args: dict[str, Any] = {
            "rel_path": "note.md",
            "heading": "Item",
            "destination_heading": "Archive",
            "expected_hash": sha256_bytes(raw),
        }
        before_files = {
            str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
        }
        preview = await _move_note_section_impl(app, **args)
        assert "error" not in preview, preview
        assert preview["committed"] is False
        assert preview["before_hash"] == sha256_bytes(raw)
        assert preview["projected_hash"] != preview["before_hash"]
        assert {
            str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
        } == before_files
        committed = await _move_note_section_impl(app, **args, confirm=True, request_id="move-item")
        assert "error" not in committed, committed
        assert committed["indexed"] is True
        assert committed["committed"] is True
        assert (
            committed["content_hash"]
            == preview["projected_hash"]
            == sha256_bytes(path.read_bytes())
        )
        assert path.read_bytes().count(b"### Item ##") == 1
        assert path.read_bytes().endswith(
            (
                "## Archive\n\n### Item ##\n\nDone\n\n#### History\nkept\n\n".replace("\n", eol)
            ).encode()
        )
        replay = await _move_note_section_impl(app, **args, confirm=True, request_id="move-item")
        assert replay["replayed"] is True
        assert replay["operation_id"] == committed["operation_id"]
        assert sha256_bytes(path.read_bytes()) == committed["content_hash"]
        operations = await app.vault_writer.list_operations()
        assert len(operations) == 1
        assert operations[0].tool == "move_note_section"
        assert operations[0].before_hash == sha256_bytes(raw)
        assert (
            OperationJournal(tmp_path, retention_days=30, history_mode="full").read_history(
                sha256_bytes(raw)
            )
            == raw
        )
        indexed = await app.store.list_indexed_notes()
        assert indexed["note.md"][1] == committed["content_hash"]
        tools = await create_server(app).list_tools()
        assert "move_note_section" in {tool.name for tool in tools}
    finally:
        await app.store.close()


@pytest.mark.parametrize("expected_hash", [None, "0" * 64])
@pytest.mark.parametrize("confirm", [False, True])
async def test_missing_stale_cas_never_writes(
    tmp_path: Path, expected_hash: str | None, confirm: bool
) -> None:
    path = tmp_path / "note.md"
    raw = serialize(
        {"id": "01J00000000000000000000091"}, "# Root\n\n### Item\n\n## Archive\n"
    ).encode()
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        result = await _move_note_section_impl(
            app,
            rel_path="note.md",
            heading="Item",
            destination_heading="Archive",
            expected_hash=expected_hash,
            confirm=confirm,
        )
        assert "error" in result
        assert path.read_bytes() == raw
        assert not await app.store.list_indexed_notes()
    finally:
        await app.store.close()


async def test_public_preview_commit_and_secret_heading(tmp_path: Path) -> None:
    secret_heading = "password=VerySensitiveExample123"  # noqa: S105 - synthetic leak probe
    raw = serialize(
        {"id": "01J00000000000000000000091"},
        "# Root\n\n### " + secret_heading + "\n\nHistory\n\n## Archive\n",
    ).encode()
    path = tmp_path / "note.md"
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        server = create_server(app)
        args: dict[str, Any] = {
            "rel_path": "note.md",
            "heading": secret_heading,
            "destination_heading": "Archive",
            "expected_hash": sha256_bytes(raw),
        }
        preview = await server.call_tool("move_note_section", args)
        assert isinstance(preview, CallToolResult)
        assert not preview.is_error, preview
        assert secret_heading not in str(preview)
        assert isinstance(preview.content[0], TextContent)
        payload = json.loads(preview.content[0].text)
        assert payload["committed"] is False
        assert path.read_bytes() == raw
        committed = await server.call_tool(
            "move_note_section", {**args, "confirm": True, "request_id": "public-move"}
        )
        assert isinstance(committed, CallToolResult)
        assert not committed.is_error, committed
        assert secret_heading not in str(committed)
        assert isinstance(committed.content[0], TextContent)
        receipt = json.loads(committed.content[0].text)
        assert receipt["content_hash"] == payload["projected_hash"]
    finally:
        await app.store.close()


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("confirm", "true"),
        ("confirm", 1),
        ("heading_level", True),
        ("heading_level", "3"),
        ("heading_occurrence", True),
        ("destination_level", 2.5),
        ("destination_occurrence", "1"),
    ],
)
async def test_public_rejects_coerced_selectors(tmp_path: Path, parameter: str, value: Any) -> None:
    raw = serialize(
        {"id": "01J00000000000000000000091"}, "# Root\n\n### Item\n\n## Archive\n"
    ).encode()
    path = tmp_path / "note.md"
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    args = {
        "rel_path": "note.md",
        "heading": "Item",
        "destination_heading": "Archive",
        "expected_hash": sha256_bytes(raw),
        parameter: value,
    }
    with pytest.raises(ToolError, match="validation error"):
        await create_server(app).call_tool("move_note_section", args)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "# Root\r\n\n### Item\n\n## Archive\n",
        "# Root\n\n### Item\n\n## Archive",
    ],
)
async def test_unsafe_eols_and_boundaries_refused(tmp_path: Path, body: str, confirm: bool) -> None:
    raw = serialize({"id": "01J00000000000000000000091"}, body).encode()
    path = tmp_path / "note.md"
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    result = await _move_note_section_impl(
        app,
        rel_path="note.md",
        heading="Item",
        destination_heading="Archive",
        expected_hash=sha256_bytes(raw),
        confirm=confirm,
    )
    assert "error" in result
    assert path.read_bytes() == raw


async def test_read_only_hides_move_tool(tmp_path: Path) -> None:
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], read_only=True),
        vault_root=tmp_path,
    )
    assert "move_note_section" not in {tool.name for tool in await create_server(app).list_tools()}
