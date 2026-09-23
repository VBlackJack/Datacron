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
"""One note saved in a legacy encoding must not take the whole vault down.

A note indexed as UTF-8 and later re-saved in cp1252 by an older editor kept its
index rows. Listing and search then read it again, for paging or for redaction,
and the decoding error failed the whole call, on every call, for every note.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import DatacronApp, build_app, create_server

_IDS = {
    "a.md": "01J00000000000000000000121",
    "b.md": "01J00000000000000000000122",
    "c.md": "01J00000000000000000000123",
}


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    built = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    for name, note_id in _IDS.items():
        (tmp_path / name).write_text(
            serialize({"id": note_id}, f"# {name}\n\nbudget review\n"), encoding="utf-8"
        )
    await built.store.open(sidecar_index_db(tmp_path))
    try:
        yield built
    finally:
        await built.store.close()


async def _call(app: DatacronApp, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await create_server(app).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    return dict(json.loads(result.content[0].text))


async def _resave_in_cp1252(app: DatacronApp) -> None:
    listed = await _call(app, "list_notes", {})
    assert "error" not in listed
    (app.vault_root / "b.md").write_bytes("# b.md\n\nbudget café\n".encode("cp1252"))


async def test_list_notes_survives_a_note_re_saved_in_cp1252(app: DatacronApp) -> None:
    await _resave_in_cp1252(app)

    listed = await _call(app, "list_notes", {})

    assert "error" not in listed
    paths = {note["rel_path"] for note in listed["notes"]}
    assert {"a.md", "c.md"} <= paths
    assert "b.md" not in paths


async def test_search_survives_a_note_re_saved_in_cp1252(app: DatacronApp) -> None:
    await _resave_in_cp1252(app)

    found = await _call(app, "search_text", {"query": "budget"})

    assert "error" not in found
    paths = {result["note_rel_path"] for result in found["results"]}
    assert paths == {"a.md", "c.md"}
