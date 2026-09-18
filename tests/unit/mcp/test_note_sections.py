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
"""Exact heading subtree retrieval and missing chunk identity regression tests."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN, Settings
from datacron.core.hashing import hash_text
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools.read import _get_note_impl


@pytest.fixture
def section_app(tmp_path: Path) -> DatacronApp:
    return build_app(
        settings=Settings(read_paths=[tmp_path], vault_root=tmp_path), vault_root=tmp_path
    )


async def test_subtree_ancestry_bounds_hash_and_no_index_repair(
    section_app: DatacronApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = (
        "---\ntitle: Example\n---\n# Root\nintro\n## **Target** ###\n"
        "first\n### Child\nchild\n## Other\nexcluded\n"
    )
    (tmp_path / "note.md").write_bytes(raw.encode("utf-8"))
    repair = AsyncMock(side_effect=AssertionError("section reads must not repair the index"))
    monkeypatch.setattr("datacron.mcp.tools.read._repair_index_on_read", repair)
    payload = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root", "Target"]
    )
    assert "## **Target** ###\nfirst\n### Child\nchild\n" in payload["content"]
    assert "excluded" not in payload["content"]
    assert payload["section"] == {
        "heading_path": ["Root", "Target"],
        "heading_occurrence": 1,
        "matching_headings": 1,
        "line_start": 6,
        "line_end": 9,
    }
    assert payload["content_hash"] == payload["note_content_hash"] == hash_text(raw)
    repair.assert_not_awaited()


async def test_duplicate_paths_require_occurrence_and_exact_ancestry(
    section_app: DatacronApp, tmp_path: Path
) -> None:
    (tmp_path / "note.md").write_text(
        "# Root\n## Same\nfirst\n## Same\nsecond\n# Other\n## Same\nthird\n"
    )
    ambiguous = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root", "Same"]
    )
    assert "ambiguous" in ambiguous["error"]["message"]
    selected = await _get_note_impl(
        section_app,
        id_or_path="note.md",
        fmt="full",
        heading_path=["Root", "Same"],
        heading_occurrence=2,
    )
    assert "second" in selected["content"]
    assert "first" not in selected["content"]
    assert "third" not in selected["content"]
    assert selected["section"]["matching_headings"] == 2
    for path, occurrence in [(["Same"], None), (["Root", "Same"], 3)]:
        missing = await _get_note_impl(
            section_app,
            id_or_path="note.md",
            fmt="full",
            heading_path=path,
            heading_occurrence=occurrence,
        )
        assert "error" in missing


async def test_setext_fences_and_relative_pagination(
    section_app: DatacronApp, tmp_path: Path
) -> None:
    body = "Root\n====\nTarget\n------\n```md\n# Fake\n```\ntext\n\nSibling\n-------\nexcluded"
    (tmp_path / "note.md").write_bytes(body.encode("utf-8"))
    section = "Target\n------\n```md\n# Fake\n```\ntext\n\n"
    full = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root", "Target"]
    )
    assert section in full["content"]
    assert full["total_chars"] == len(section)
    page = await _get_note_impl(
        section_app,
        id_or_path="note.md",
        fmt="full",
        heading_path=["Root", "Target"],
        offset=4,
        limit=7,
    )
    assert section[4:11] in page["content"]
    assert (page["offset"], page["returned_chars"], page["next_offset"]) == (4, 7, 11)
    end = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root", "Target"], offset=1000
    )
    assert end["returned_chars"] == 0
    assert end["next_offset"] is None
    fake = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Fake"]
    )
    assert "error" in fake


@pytest.mark.parametrize(
    ("path", "occurrence", "fmt", "identifier"),
    [
        ([], None, "full", "note.md"),
        (["Root"] * 7, None, "full", "note.md"),
        ([""], None, "full", "note.md"),
        ([" "], None, "full", "note.md"),
        (["Root"], 0, "full", "note.md"),
        (["Root"], -1, "full", "note.md"),
        (None, 1, "full", "note.md"),
        (["Root"], None, "map", "note.md"),
        (["Root"], None, "chunk", "note.md"),
        (["Root"], None, "full", "01HQXR7K9YZ8M2N3PQRSTV4WX5::999"),
    ],
)
async def test_invalid_selectors(
    section_app: DatacronApp,
    path: list[str] | None,
    occurrence: int | None,
    fmt: str,
    identifier: str,
) -> None:
    assert "error" in await _get_note_impl(
        section_app,
        id_or_path=identifier,
        fmt=fmt,
        heading_path=path,
        heading_occurrence=occurrence,
    )


@pytest.mark.parametrize("unavailable", [False, True])
async def test_missing_chunk_never_falls_back_to_note(
    section_app: DatacronApp, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    lookup = AsyncMock(
        return_value=None, side_effect=RuntimeError("closed") if unavailable else None
    )
    fallback = AsyncMock(side_effect=AssertionError("must not resolve the parent note"))
    monkeypatch.setattr(section_app.store, "get_chunk", lookup)
    monkeypatch.setattr("datacron.mcp.tools.read._resolve_note", fallback)
    result = await _get_note_impl(
        section_app, id_or_path="01HQXR7K9YZ8M2N3PQRSTV4WX5::999", fmt="full"
    )
    assert "chunk_id" in result["error"]["message"]
    fallback.assert_not_awaited()


async def test_section_budget_and_redaction(section_app: DatacronApp, tmp_path: Path) -> None:
    section_app = build_app(
        settings=section_app.settings.model_copy(update={"get_note_max_tokens": 50}),
        vault_root=tmp_path,
    )
    body = "# Root\npassword=supersecretvalue\n" + "x" * 1000
    (tmp_path / "note.md").write_bytes(body.encode("utf-8"))
    payload = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root"], limit=10000
    )
    assert payload["returned_chars"] <= 50 * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    assert payload["truncated"] is True
    assert "supersecretvalue" not in payload["content"]
    assert payload["content_hash"] == hash_text(body)


async def test_no_selector_preserves_full_note_contract(
    section_app: DatacronApp, tmp_path: Path
) -> None:
    body = "# Root\nintro\n## Child\nbody"
    (tmp_path / "note.md").write_bytes(body.encode("utf-8"))
    payload = await _get_note_impl(section_app, id_or_path="note.md", fmt="full")
    assert body in payload["content"]
    assert "section" not in payload
    assert payload["total_chars"] == len(body)


async def test_mcp_rejects_boolean_occurrence_and_exposes_section_schema(
    section_app: DatacronApp, tmp_path: Path
) -> None:
    from jsonschema import Draft202012Validator
    from mcp.client import Client

    (tmp_path / "note.md").write_text("# Root\nbody")
    server = create_server(section_app)
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["get_note"]
    assert "heading_path" in tool.input_schema["properties"]
    assert "heading_occurrence" in tool.input_schema["properties"]
    assert tool.output_schema is not None
    Draft202012Validator.check_schema(tool.output_schema)
    payload = await _get_note_impl(
        section_app, id_or_path="note.md", fmt="full", heading_path=["Root"]
    )
    Draft202012Validator(tool.output_schema).validate(payload)
    async with Client(server, mode="auto") as client:
        result = await client.call_tool(
            "get_note",
            {"id_or_path": "note.md", "heading_path": ["Root"], "heading_occurrence": True},
        )
        assert result.is_error


async def test_section_ancestor_metadata_uses_parent_secret_context(tmp_path: Path) -> None:
    raw = "# Root\nBEGIN_SECRET\n## HiddenValue\nbody\nEND_SECRET\n"
    (tmp_path / "note.md").write_bytes(raw.encode())
    app = build_app(
        settings=Settings(
            read_paths=[tmp_path],
            vault_root=tmp_path,
            secret_redaction_patterns=[r"(?s)BEGIN_SECRET(?P<secret>.*?)END_SECRET"],
        ),
        vault_root=tmp_path,
    )
    payload = await _get_note_impl(
        app, id_or_path="note.md", fmt="full", heading_path=["Root", "HiddenValue"]
    )
    assert "HiddenValue" not in str(payload)
    assert payload["section"]["heading_path"] == ["Root", "[REDACTED]"]
