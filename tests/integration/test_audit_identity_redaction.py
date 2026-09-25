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
"""Regression coverage for contextual metadata and live identity authorities."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.hashing import hash_text
from datacron.core.paths import sidecar_index_db
from datacron.core.vault import DuplicateNoteIdentityError
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools.read import _get_note_impl

_ID = "01J00000000000000000000091"
_OTHER_ID = "01J00000000000000000000092"
_MARKER = "SyntheticConfidentialHeading"


@pytest.fixture
async def audit_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    app = build_app(
        settings=Settings(
            vault_root=tmp_path,
            read_paths=[tmp_path],
            write_paths=[tmp_path],
            redact_secrets="all",
            secret_redaction_patterns=[r"(?s)PRIVATE_BEGIN.*?PRIVATE_END"],
            repair_min_interval_seconds=0,
        ),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        yield app
    finally:
        await app.store.close()


async def _call(app: DatacronApp, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await create_server(app).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result
    assert isinstance(result.content[0], TextContent)
    return dict(json.loads(result.content[0].text))


def _assert_protected(payload: object) -> None:
    rendered = json.dumps(payload).casefold()
    assert _MARKER.casefold() not in rendered
    assert "syntheticconfidentialheading" not in rendered


@pytest.mark.parametrize("heading", [f"## {_MARKER}", f"{_MARKER}\n---"])
async def test_contextual_heading_metadata_and_navigation(
    audit_app: DatacronApp, heading: str
) -> None:
    app = audit_app
    body = (
        f"---\nid: {_ID}\ntitle: Public\n---\n# Public\n\n"
        f"PRIVATE_BEGIN\n\n{heading}\n\nPRIVATE_END\n\n"
        "### PublicChild\n\n[[Target]]\n\n## PublicSibling\n\nPublicTail\n"
    )
    note_path = app.vault_root / "note.md"
    note_path.write_bytes(body.encode())
    (app.vault_root / "target.md").write_text(
        f"---\nid: {_OTHER_ID}\n---\n# Target\n", encoding="utf-8"
    )
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    stored = await app.store.list_chunks_for_note(_ID)
    mapped = await _call(app, "get_note", {"id_or_path": "note.md", "format": "map"})
    _assert_protected(mapped)
    assert any(row["text"] == "PublicSibling" for row in mapped["headings"])
    assert any("::@redacted-" in row["chunk_id"] for row in mapped["headings"])
    for row in mapped["headings"]:
        payload = await _call(app, "get_note", {"id_or_path": row["chunk_id"]})
        _assert_protected(payload)
        for key in ("prev_chunk_id", "next_chunk_id"):
            if payload[key] is not None:
                neighbor = await _call(app, "get_note", {"id_or_path": payload[key]})
                _assert_protected(neighbor)
    for tool, arguments in (
        ("search_text", {"query": _MARKER}),
        ("search_regex", {"pattern": _MARKER}),
        ("get_backlinks", {"target": "Target"}),
    ):
        result = await _call(app, tool, arguments)
        # Queries are caller-supplied; inspect every returned result field.
        result.pop("query", None)
        result.pop("pattern", None)
        _assert_protected(result)
        assert result["returned"] > 0
    assert await app.store.list_chunks_for_note(_ID) == stored
    assert note_path.read_bytes() == body.encode()


async def test_opaque_chunk_alias_keeps_stale_and_unknown_refusals(audit_app: DatacronApp) -> None:
    app = audit_app
    path = app.vault_root / "note.md"
    path.write_text(f"# Public\n\nPRIVATE_BEGIN\n\n## {_MARKER}\n\nPRIVATE_END\n")
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    mapped = await _get_note_impl(app, id_or_path="note.md", fmt="map")
    chunk_id = mapped["headings"][1]["chunk_id"]
    assert "::@redacted-" in chunk_id
    unknown = await _get_note_impl(
        app, id_or_path=f"{_ID}::@redacted-{'0' * 64}::0000", fmt="chunk"
    )
    assert "error" in unknown
    path.write_text(path.read_text() + "\nChanged\n")
    stale = await _get_note_impl(app, id_or_path=chunk_id, fmt="chunk")
    assert stale["error"]["type"] == "StaleChunkError"


async def test_protected_h1_does_not_escape_as_note_title(audit_app: DatacronApp) -> None:
    app = audit_app
    (app.vault_root / "note.md").write_text(
        f"PRIVATE_BEGIN\n\n# {_MARKER}\n\nPRIVATE_END\n", encoding="utf-8"
    )
    for fmt in ("full", "map"):
        _assert_protected(await _get_note_impl(app, id_or_path="note.md", fmt=fmt))


async def test_heading_cannot_impersonate_an_opaque_chunk_alias(audit_app: DatacronApp) -> None:
    app = audit_app
    path = app.vault_root / "note.md"
    body = f"---\nid: {_ID}\n---\nPRIVATE_BEGIN\n\n# {_MARKER}\n\nPRIVATE_END\n"
    path.write_text(body, encoding="utf-8")
    note = await app.vault_reader.read_note(path)
    original = next(chunk for chunk in app.chunker.chunk(note) if chunk.section_title == _MARKER)
    path.write_text(
        body + f"\n# redacted-{hash_text(original.chunk_id)}\n\nDecoy\n", encoding="utf-8"
    )
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    mapped = await _get_note_impl(app, id_or_path="note.md", fmt="map")
    alias = mapped["headings"][0]["chunk_id"]
    resolved = await _get_note_impl(app, id_or_path=alias, fmt="chunk")
    assert resolved["chunk_content_hash"] == original.content_hash
    assert "Decoy" not in resolved["content"]


@pytest.mark.parametrize("seeded", [False, True])
async def test_duplicate_identity_refusal_preserves_index(
    audit_app: DatacronApp, seeded: bool
) -> None:
    app = audit_app
    (app.vault_root / "a.md").write_text(f"---\nid: {_ID}\n---\n# Alpha\n")
    if seeded:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    before = await app.store.list_indexed_notes_with_mtime()
    generation = await app.store.get_generation()
    (app.vault_root / "b.md").write_text(f"---\nid: {_ID}\n---\n# Beta\n")
    for _ in range(3):
        with pytest.raises(DuplicateNoteIdentityError, match=r"a\.md.*b\.md"):
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        assert await app.store.list_indexed_notes_with_mtime() == before
        assert await app.store.get_generation() == generation
    public = await create_server(app).call_tool("search_text", {"query": "Alpha"})
    assert isinstance(public, CallToolResult)
    assert public.is_error
    assert "duplicate_note_identity" in str(public.content)


async def test_store_refuses_cross_path_identity_replacement(audit_app: DatacronApp) -> None:
    app = audit_app
    for name in ("a.md", "b.md"):
        (app.vault_root / name).write_text(f"---\nid: {_ID}\n---\n# {name}\n")
    first = await app.vault_reader.read_note(app.vault_root / "a.md")
    second = await app.vault_reader.read_note(app.vault_root / "b.md")
    await app.store.upsert_note(first, app.chunker.chunk(first))
    with pytest.raises(DuplicateNoteIdentityError):
        await app.store.upsert_note(second, app.chunker.chunk(second))
    assert await app.store.get_note_rel_path(_ID) == "a.md"
    assert await app.store.list_chunks_for_note(_ID) == app.chunker.chunk(first)


async def test_valid_identity_exchange_and_move(audit_app: DatacronApp) -> None:
    app = audit_app
    for name, identity in (("a.md", _ID), ("b.md", _OTHER_ID)):
        (app.vault_root / name).write_text(f"---\nid: {identity}\n---\n# {name}\n")
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    for name, identity in (("a.md", _OTHER_ID), ("b.md", _ID)):
        (app.vault_root / name).write_text(f"---\nid: {identity}\n---\n# {name}\n")
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    assert await app.store.get_note_rel_path(_ID) == "b.md"
    (app.vault_root / "b.md").rename(app.vault_root / "c.md")
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    assert await app.store.get_note_rel_path(_ID) == "c.md"


async def test_complete_stale_sidecar_cannot_hide_frontmatter_identity(
    audit_app: DatacronApp,
) -> None:
    app = audit_app
    (app.vault_root / "note.md").write_text(f"---\nid: {_ID}\n---\n# Note\n")
    sidecar = app.vault_root / ".datacron" / "ulids.json"
    sidecar.write_text(json.dumps({"note.md": _OTHER_ID}), encoding="utf-8")
    before = sidecar.read_bytes()
    by_id = await _get_note_impl(app, id_or_path=_ID, fmt="full")
    assert by_id["id"] == _ID
    assert by_id["rel_path"] == "note.md"
    assert "error" in await _get_note_impl(app, id_or_path=_OTHER_ID, fmt="full")
    assert sidecar.read_bytes() == before
