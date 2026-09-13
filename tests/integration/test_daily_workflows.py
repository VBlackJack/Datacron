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
"""Cross-session public MCP scenarios and bounded orientation regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.memory_protocol import CONTRACT_HASH, CONTRACT_TEXT
from datacron.core.paths import sidecar_index_db
from datacron.eval.transport import e2e_tool_transport
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import build_app, create_server
from datacron.mcp.tools.session import rendered_size, session_context


def settings(root: Path) -> Settings:
    return Settings(
        vault_root=root,
        read_paths=[root],
        write_paths=[root],
        session_context_paths=[],
        log_dir=root / "logs",
        repair_min_interval_seconds=0,
    )


async def test_contract_cache_and_adaptive_source_budget(tmp_path: Path) -> None:
    (tmp_path / "project.md").write_text("# Project\n\nNext action: verify delivery.\n" * 500)
    app = build_app(settings=settings(tmp_path), vault_root=tmp_path)
    full = await session_context(app, note_paths=["project.md"], max_tokens=2000)
    assert full["contract"]["instructions"] == CONTRACT_TEXT
    assert full["sources"], full
    assert rendered_size(full) <= 2000 * 4
    cached = await session_context(
        app, note_paths=["project.md"], max_tokens=2500, known_contract_hash=CONTRACT_HASH
    )
    assert cached["contract"]["delivery"] == "unchanged"
    assert "instructions" not in cached["contract"]
    assert cached["sources"][0]["next_offset"] > full["sources"][0]["next_offset"]
    stale = await session_context(app, known_contract_hash="old", max_tokens=2500)
    assert stale["contract"]["instructions"] == CONTRACT_TEXT


@pytest.mark.parametrize(
    "signal",
    [
        {"archived": True},
        {"status": "archived"},
        {"tags": ["memory/archive"]},
        {"tags": ["meta/archive"]},
    ],
)
async def test_explicit_archive_ranking_and_history_access(
    tmp_path: Path, signal: dict[str, Any]
) -> None:
    for path, metadata in (("current.md", {}), ("history.md", signal)):
        (tmp_path / path).write_text(serialize(metadata, "# Project status\n\nProject status.\n"))
    app = build_app(settings=settings(tmp_path), vault_root=tmp_path)
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
        response = await create_server(app).call_tool(
            "search_text", {"query": "Project status", "group_by_note": True}
        )
        assert isinstance(response, CallToolResult)
        assert isinstance(response.content[0], TextContent)
        payload = json.loads(response.content[0].text)
        assert payload["results"][0]["note_rel_path"] == "current.md"
        assert payload["results"][1]["lifecycle"] == "archived"
        history = await create_server(app).call_tool("get_note", {"id_or_path": "history.md"})
        assert isinstance(history, CallToolResult)
        assert not history.is_error
    finally:
        await app.store.close()


async def test_real_process_reconnect_correction_and_partial_write_recovery(tmp_path: Path) -> None:
    for path in ("project.md", "person.md", "waiting.md"):
        (tmp_path / path).write_text(serialize({}, "# Project\n\n## Journal\n"))
    config = settings(tmp_path)
    async with e2e_tool_transport(tmp_path, config) as call:
        original = await call("get_note", {"id_or_path": "project.md"})
        arguments = {
            "rel_path": "project.md",
            "heading": "Journal",
            "entry": "Decision A. Owner unknown.",
            "expected_hash": original["content_hash"],
            "request_id": "decision-v1",
        }
        receipt = await call("append_journal", arguments)
        assert receipt["indexed"] is True
        person = await call("get_note", {"id_or_path": "person.md"})
        await call(
            "append_journal",
            {"rel_path": "person.md", "heading": "Journal", "entry": "Concurrent update."},
        )
    # A new OS process must recover memory from the vault, not conversation state.
    async with e2e_tool_transport(tmp_path, config) as call:
        replay = await call("append_journal", arguments)
        assert replay["replayed"] is True
        current = await call("get_note", {"id_or_path": "project.md"})
        assert current["content"].count("Decision A.") == 1
        conflict = await call(
            "append_journal",
            {
                "rel_path": "person.md",
                "heading": "Journal",
                "entry": "Follow up.",
                "expected_hash": person["content_hash"],
                "request_id": "person-follow-up",
            },
        )
        assert "error" in conflict
        progress = await call(
            "get_write_progress",
            {
                "requests": [
                    {"note": "project.md", "request_id": "decision-v1"},
                    {
                        "note": "person.md",
                        "request_id": "person-follow-up",
                        "expected_hash": person["content_hash"],
                    },
                    {"note": "waiting.md", "request_id": "waiting-follow-up"},
                ]
            },
        )
        assert progress["counts"] == {"committed_current": 1, "conflict": 1, "not_recorded": 1}
        correction = await call(
            "append_journal",
            {
                "rel_path": "project.md",
                "heading": "Journal",
                "entry": "Correction: Decision B supersedes A. Validation remains open.",
                "expected_hash": current["content_hash"],
                "request_id": "decision-v2",
            },
        )
        assert correction["indexed"]
    async with e2e_tool_transport(tmp_path, config) as call:
        final = await call("get_note", {"id_or_path": "project.md"})
        assert "Decision B" in final["content"]
        assert "Validation remains open" in final["content"]
        progress = await call(
            "get_write_progress",
            {"requests": [{"note": "project.md", "request_id": "decision-v1"}]},
        )
        assert progress["items"][0]["status"] == "committed_changed"
        absent = await call("search_text", {"query": "NonexistentAppointmentUnique"})
        assert absent["returned"] == 0


async def test_archive_writer_requires_cas_and_preserves_replay(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text(serialize({"title": "Note"}, "# Note\n"))
    app = build_app(settings=settings(tmp_path), vault_root=tmp_path)
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        from datacron.mcp.tools.write import _set_frontmatter_impl

        note = await app.vault_reader.read_note(tmp_path / "note.md")
        refused = await _set_frontmatter_impl(app, rel_path="note.md", archived=True)
        assert "error" in refused
        first = await _set_frontmatter_impl(
            app,
            rel_path="note.md",
            archived=True,
            expected_hash=note.content_hash,
            request_id="archive-one",
        )
        assert first["indexed"]
        replay = await _set_frontmatter_impl(
            app,
            rel_path="note.md",
            archived=True,
            expected_hash=note.content_hash,
            request_id="archive-one",
        )
        assert replay["replayed"]
        active = await _set_frontmatter_impl(
            app,
            rel_path="note.md",
            archived=False,
            expected_hash=first["content_hash"],
            request_id="unarchive-one",
        )
        assert active["indexed"]
        metadata = await app.store.list_temporal_metadata()
        assert not metadata[note.id].archived
    finally:
        await app.store.close()


async def test_progress_reports_index_loss_and_unknown_commit_honestly(tmp_path: Path) -> None:
    from datacron.mcp.tools.write import _append_journal_impl
    from datacron.mcp.tools.write_progress import WriteReference, get_write_progress

    (tmp_path / "note.md").write_text(serialize({"title": "Note"}, "# Note\n\n## Journal\n"))
    app = build_app(settings=settings(tmp_path), vault_root=tmp_path)
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        receipt = await _append_journal_impl(
            app, rel_path="note.md", heading="Journal", entry="Saved", request_id="save-one"
        )
        assert receipt["indexed"]
        note = await app.vault_reader.read_note(tmp_path / "note.md")
        await app.store.delete_note(note.id)
        result = await get_write_progress(
            app,
            [
                WriteReference(note="note.md", request_id="save-one"),
                WriteReference(note="note.md", request_id="not-sent"),
            ],
        )
        assert result["items"][0]["status"] == "committed_index_incomplete"
        assert result["items"][1]["committed"] is None
        assert result["items"][1]["status"] == "not_recorded"
        refused = await get_write_progress(
            app, [WriteReference(note="../outside.md", request_id="save-one")]
        )
        assert "error" in refused
        assert "items" not in refused
        assert (await app.vault_reader.read_note(tmp_path / "note.md")).content_hash == receipt[
            "content_hash"
        ]
    finally:
        await app.store.close()
