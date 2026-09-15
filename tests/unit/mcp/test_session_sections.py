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
"""Bounded discontiguous orientation excerpts and honest continuations."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN, Settings
from datacron.core.memory_protocol import CONTRACT_TEXT, SESSION_DEFAULT_SECTIONS
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.session import rendered_size, session_context


def _settings(
    tmp_path: Path, *, chars: int = 100, sections: list[list[str]] | None = None
) -> Settings:
    return Settings(
        read_paths=[tmp_path],
        vault_root=tmp_path,
        session_context_paths=["note.md"],
        session_note_chars=chars,
        session_context_sections={
            "note.md": sections if sections is not None else [["Root", "One"], ["Root", "Two"]]
        },
    )


async def test_late_sections_share_allowance_and_continue_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "# Root\n"
        + "preamble " * 1000
        + "\n## One\n"
        + "a" * 200
        + "\n### Child\nchild\n## Gap\nexcluded\n## Two\n"
        + "b" * 200
    )
    (tmp_path / "note.md").write_bytes(body.encode())
    app = build_app(settings=_settings(tmp_path), vault_root=tmp_path)
    repair = AsyncMock(side_effect=AssertionError("must remain read-only"))
    monkeypatch.setattr("datacron.mcp.tools.read._repair_index_on_read", repair)
    result = await session_context(app)
    source = result["sources"][0]
    assert "content" not in source
    assert "next_read" not in source
    excerpts = source["excerpts"]
    assert len(excerpts) == 2
    assert sum(excerpt["returned_chars"] for excerpt in excerpts) == 100
    for excerpt, target in zip(excerpts, ["One", "Two"], strict=True):
        assert f"## {target}" in excerpt["content"]
        assert "excluded" not in excerpt["content"]
        pointer = excerpt["next_read"]
        assert pointer["heading_path"] == ["Root", target]
        assert pointer["offset"] == 50
        continuation = await _get_note_impl(
            app,
            id_or_path=pointer["id_or_path"],
            fmt=pointer["format"],
            heading_path=pointer["heading_path"],
            heading_occurrence=pointer["heading_occurrence"],
            offset=pointer["offset"],
            limit=10,
        )
        assert continuation["offset"] == 50
        assert continuation["section"] == excerpt["section"]
    assert result["contract"]["instructions"] == CONTRACT_TEXT
    assert rendered_size(result) <= app.settings.max_result_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    assert not result["index_repaired"]
    repair.assert_not_awaited()


@pytest.mark.parametrize(
    "body", ["# Root\n## One\nfirst\n", "# Root\n## One\nfirst\n## One\nduplicate\n## Two\nsecond"]
)
async def test_missing_or_ambiguous_section_falls_back_explicitly(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / "note.md").write_bytes(body.encode())
    app = build_app(settings=_settings(tmp_path), vault_root=tmp_path)
    source = (await session_context(app))["sources"][0]
    assert "content" in source
    assert "excerpts" not in source
    assert source["section_selection"]["mode"] == "full_fallback"
    assert source["section_selection"]["unavailable_sections"]
    assert len(source["section_selection"]["omitted_sections"]) == 2
    assert "heading_path" not in source["next_read"]


async def test_zero_quota_is_explicit_omission(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Root\n## One\na\n## Two\nb")
    app = build_app(settings=_settings(tmp_path, chars=1), vault_root=tmp_path)
    source = (await session_context(app))["sources"][0]
    assert len(source["excerpts"]) == 1
    assert source["excerpts"][0]["returned_chars"] == 1
    omitted = source["section_selection"]["omitted_sections"][0]
    assert omitted["next_read"]["heading_path"] == ["Root", "Two"]
    assert omitted["next_read"]["offset"] == 0
    assert source["truncated"]


async def test_nested_fences_redaction_and_finished_pointer(tmp_path: Path) -> None:
    body = "# Root\n## One\n```md\n## Two\n```\n### Child\npassword=supersecretvalue\n## Two\nshort"
    (tmp_path / "note.md").write_bytes(body.encode())
    app = build_app(settings=_settings(tmp_path, chars=1000), vault_root=tmp_path)
    source = (await session_context(app))["sources"][0]
    assert "supersecretvalue" not in str(source)
    assert "### Child" in source["excerpts"][0]["content"]
    assert all(excerpt["next_read"] is None for excerpt in source["excerpts"])


async def test_ordinary_notes_and_overall_budget_remain_bounded(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Root\n" + "x" * 1000)
    app = build_app(settings=_settings(tmp_path, sections=[]), vault_root=tmp_path)
    result = await session_context(app)
    assert "content" in result["sources"][0]
    assert result["sources"][0]["section_selection"]["mode"] == "full"
    bounded = await session_context(app, max_tokens=1400)
    if "error" in bounded:
        assert bounded["error"]["code"] == "context_budget_too_small"
    else:
        assert rendered_size(bounded) <= 1400 * TOKEN_ESTIMATE_CHARS_PER_TOKEN
        assert bounded["contract"]["instructions"] == CONTRACT_TEXT


@pytest.mark.parametrize(
    "preferences",
    [
        {"../note.md": [["Root"]]},
        {"note.md": [[]]},
        {"note.md": [[" "]]},
        {"note.md": [["Root"] * 7]},
        {"note.md": [["Root"], ["Root"]]},
        {"note.md": [["x" * 513]]},
        {"note.md": [[str(i)] for i in range(9)]},
    ],
)
def test_invalid_section_preferences(preferences: dict[str, list[list[str]]]) -> None:
    with pytest.raises(ValidationError):
        Settings(session_context_sections=preferences)


def test_default_preferences_select_no_sections() -> None:
    assert SESSION_DEFAULT_SECTIONS == {}
    assert Settings().session_context_sections == {}


async def test_unconfigured_orientation_note_reports_full_mode_explicitly(
    tmp_path: Path,
) -> None:
    (tmp_path / "_memory").mkdir()
    (tmp_path / "_memory" / "INIT.md").write_text("# INIT\n\n## Where things live\n\nHere.\n")
    app = build_app(
        settings=Settings(read_paths=[tmp_path], vault_root=tmp_path), vault_root=tmp_path
    )
    source = (await session_context(app))["sources"][0]
    assert "content" in source
    assert "excerpts" not in source
    assert source["section_selection"] == {
        "mode": "full",
        "reason": "no_sections_configured",
        "unavailable_sections": [],
        "omitted_sections": [],
    }


async def test_configured_sections_are_selected_from_the_vault_headings(
    tmp_path: Path,
) -> None:
    (tmp_path / "_memory").mkdir()
    (tmp_path / "_memory" / "INIT.md").write_text(
        "# INIT\n\n## Where things live\n\nHere.\n\n## How to write\n\nOne fact per note.\n"
    )
    settings = Settings(
        read_paths=[tmp_path],
        vault_root=tmp_path,
        session_context_sections={
            "_memory/INIT.md": [["INIT", "Where things live"], ["INIT", "How to write"]]
        },
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    source = (await session_context(app))["sources"][0]
    assert "content" not in source
    assert source["section_selection"]["mode"] == "sections"
    assert source["section_selection"]["unavailable_sections"] == []
    assert [excerpt["section"]["heading_path"][-1] for excerpt in source["excerpts"]] == [
        "Where things live",
        "How to write",
    ]


async def test_fallback_lists_never_share_an_entry(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_bytes(b"# Root\n## One\nfirst\n")
    app = build_app(settings=_settings(tmp_path), vault_root=tmp_path)
    selection = (await session_context(app))["sources"][0]["section_selection"]
    assert selection["mode"] == "full_fallback"
    shared = [
        unavailable is omitted
        for unavailable in selection["unavailable_sections"]
        for omitted in selection["omitted_sections"]
    ]
    assert not any(shared)


async def test_redacted_heading_has_no_false_continuation(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Root\n## PrivateHeading\n" + "x" * 500)
    settings = _settings(tmp_path, sections=[["Root", "PrivateHeading"]]).model_copy(
        update={"secret_redaction_patterns": ["PrivateHeading"]}
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    excerpt = (await session_context(app))["sources"][0]["excerpts"][0]
    assert excerpt["truncated"]
    assert excerpt["next_read"] is None
    assert excerpt["continuation_unavailable"] == "heading_selector_redacted"
    assert "PrivateHeading" not in str(excerpt)


async def test_get_note_cap_is_shared_across_sections(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Root\n## One\n" + "a" * 1000 + "\n## Two\n" + "b" * 1000)
    settings = _settings(tmp_path, chars=2000).model_copy(update={"get_note_max_tokens": 50})
    app = build_app(settings=settings, vault_root=tmp_path)
    excerpts = (await session_context(app))["sources"][0]["excerpts"]
    assert (
        sum(excerpt["returned_chars"] for excerpt in excerpts)
        == 50 * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    )


async def test_contextual_secret_heading_is_absent_from_every_excerpt_field(tmp_path: Path) -> None:
    raw = "# Root\nBEGIN_SECRET\n## HiddenValue\nbody\nEND_SECRET\n"
    (tmp_path / "note.md").write_bytes(raw.encode())
    settings = _settings(tmp_path, chars=5, sections=[["Root", "HiddenValue"]]).model_copy(
        update={"secret_redaction_patterns": [r"(?s)BEGIN_SECRET(?P<secret>.*?)END_SECRET"]}
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    result = await session_context(app)
    assert "HiddenValue" not in str(result)
    excerpt = result["sources"][0]["excerpts"][0]
    assert excerpt["next_read"] is None
    assert excerpt["continuation_unavailable"] == "heading_selector_redacted"


async def test_redacted_note_path_continues_through_stable_note_id(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Root\n## One\n" + "x" * 200)
    settings = _settings(tmp_path, chars=10, sections=[["Root", "One"]]).model_copy(
        update={"secret_redaction_patterns": ["note"]}
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    source = (await session_context(app))["sources"][0]
    assert source["rel_path"] == "[REDACTED].md"
    pointer = source["excerpts"][0]["next_read"]
    assert pointer["id_or_path"] == source["id"]
    continuation = await _get_note_impl(
        app,
        id_or_path=pointer["id_or_path"],
        fmt=pointer["format"],
        heading_path=pointer["heading_path"],
        offset=pointer["offset"],
    )
    assert "error" not in continuation
    assert continuation["offset"] == 10


@pytest.mark.parametrize("ambiguous_hidden", [False, True])
async def test_fallback_never_reflects_contextually_secret_selectors(
    tmp_path: Path, ambiguous_hidden: bool
) -> None:
    duplicate = "## HiddenValue\nsecond\n" if ambiguous_hidden else ""
    raw = "# Root\nBEGIN_SECRET\n## HiddenValue\nbody\n" + duplicate + "END_SECRET\n"
    (tmp_path / "note.md").write_bytes(raw.encode())
    settings = _settings(
        tmp_path, sections=[["Root", "HiddenValue"], ["Root", "Missing"]]
    ).model_copy(
        update={"secret_redaction_patterns": [r"(?s)BEGIN_SECRET(?P<secret>.*?)END_SECRET"]}
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    result = await session_context(app)
    assert "HiddenValue" not in str(result)
    selection = result["sources"][0]["section_selection"]
    assert selection["mode"] == "full_fallback"
    assert selection["unavailable_sections"][-1]["preference_index"] == 2
    assert all("heading_path" not in entry for entry in selection["unavailable_sections"])
    if not ambiguous_hidden:
        assert selection["omitted_sections"][0]["section"]["heading_path"] == ["Root", "[REDACTED]"]
