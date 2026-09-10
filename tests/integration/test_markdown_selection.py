# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Read/write agreement on AST headings and exact untouched suffix bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.markdown_sections import append_entry_to_heading, find_section_span
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.write import (
    _delete_note_section_impl,
    _patch_note_preamble_impl,
    _patch_note_section_impl,
    _rename_note_section_impl,
)


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("operation", ["patch", "rename", "delete", "preamble"])
async def test_setext_and_atx_edits_match_read_map(
    tmp_path: Path,
    eol: str,
    operation: str,
) -> None:
    raw = serialize(
        {"id": "01J00000000000000000000091"},
        "Preamble\n\n```\n# Fake\n```\n\n# Root\n\n"
        "**Section**\n---\n\nOriginal\n\n```\n## Section\n```\n\n"
        "## Tail ##\n\nPreserved\n",
    )
    path = tmp_path / "note.md"
    path.write_bytes(("\ufeff" + raw.replace("\n", eol)).encode())
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        mapping = await _get_note_impl(app, id_or_path="note.md", fmt="map")
        assert [(h["text"], h["level"]) for h in mapping["headings"]] == [
            ("Root", 1),
            ("Section", 2),
            ("Tail", 2),
        ]
        before_hash = sha256_bytes(path.read_bytes())
        if operation == "patch":
            result = await _patch_note_section_impl(
                app,
                rel_path="note.md",
                heading="Section",
                heading_level=2,
                new_content="Replacement",
                expected_hash=before_hash,
            )
        elif operation == "rename":
            result = await _rename_note_section_impl(
                app,
                rel_path="note.md",
                heading="Section",
                heading_level=2,
                new_heading="Renamed",
                expected_hash=before_hash,
            )
        elif operation == "delete":
            result = await _delete_note_section_impl(
                app,
                rel_path="note.md",
                heading="Section",
                heading_level=2,
                expected_hash=before_hash,
            )
        else:
            result = await _patch_note_preamble_impl(
                app, rel_path="note.md", new_content="New", expected_hash=before_hash
            )
        assert "error" not in result, result
        after = path.read_bytes()
        assert after.startswith(b"\xef\xbb\xbf")
        assert after.endswith(f"## Tail ##{eol}{eol}Preserved{eol}".encode())
        if operation == "rename":
            assert f"Renamed{eol}---{eol}".encode() in after
            assert f"## Section{eol}".encode() in after  # untouched fenced code
        elif operation == "delete":
            assert b"Original" not in after
            assert b"---" not in after.split(b"# Root", 1)[1]
    finally:
        await app.store.close()


def test_fences_duplicates_and_multiline_setext_selection() -> None:
    body = "```\n## Same\n```\n\n## Same ##\n\nFirst\n\nSame\n----\n\nSecond\n"
    lines = body.splitlines(keepends=True)
    assert find_section_span(lines, "Same", 2, heading_occurrence=1) == (5, 8)
    assert find_section_span(lines, "Same", 2, heading_occurrence=2) == (10, 12)
    with pytest.raises(ValueError, match="ambiguous"):
        append_entry_to_heading(body, "Same", "No arbitrary selection")
    assert find_section_span("First\nsecond\n---\nBody\n".splitlines(True), "Firstsecond", 2) == (
        3,
        4,
    )


@pytest.mark.parametrize("operation", ["patch", "delete", "rename"])
@pytest.mark.parametrize("alternate_message", [False, True])
async def test_absent_heading_reports_selection_without_writing(
    tmp_path: Path, operation: str, alternate_message: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from mcp.types import CallToolResult, TextContent

    from datacron.core.markdown_sections import HeadingNotFoundError
    from datacron.mcp.server import create_server

    if alternate_message:

        def missing_heading(*args: Any, **kwargs: Any) -> tuple[int, int]:
            raise HeadingNotFoundError("The requested section is absent")

        monkeypatch.setattr("datacron.mcp.tools.write.find_section_span", missing_heading)

    path = tmp_path / "note.md"
    path.write_text(serialize({"id": "01J00000000000000000000091"}, "## Present\n\nBody.\n"))
    before = path.read_bytes()
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        arguments: dict[str, Any] = {
            "rel_path": "note.md",
            "heading": "Absent",
            "expected_hash": sha256_bytes(before),
        }
        if operation == "patch":
            arguments["new_content"] = "Replacement"
        elif operation == "rename":
            arguments["new_heading"] = "Renamed"
        result = await create_server(app).call_tool(operation + "_note_section", arguments)
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert isinstance(result.content[0], TextContent)
        error = json.loads(result.content[0].text)["error"]
        assert error["code"] == "heading_not_found"
        assert error["type"] == "HeadingNotFoundError"
        if operation == "rename":
            assert error["message"] == "heading not found; nothing to rename"
        assert "nothing to patch" not in str(result)
        assert path.read_bytes() == before
    finally:
        await app.store.close()
