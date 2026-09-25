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
"""Chunker headings, fence tracking and wikilink edge cases shared with the readers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from datacron.core.config import Settings
from datacron.core.models import ChunkType, Note
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.wikilinks import OpenFence, extract_wikilink_targets, open_fence_after
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _get_note_impl

NoteFactory = Callable[..., Note]

_HIDDEN_TOP: Final[str] = "<!--\n# Hidden\n-->\n\ntext\n"
_HIDDEN_NESTED: Final[str] = "# Real\n\n<!--\n### Hidden\n-->\n\ntext\n"
# Enough list items that a small chunk budget cuts the block into many segments.
_LIST_ITEMS: Final[int] = 400
_SEGMENT_TOKENS: Final[int] = 16


class TestCommentedHeadings:
    def test_a_heading_inside_a_comment_is_not_a_heading_chunk(
        self, note_factory: NoteFactory
    ) -> None:
        chunks = MarkdownChunker().chunk(note_factory(raw_content=_HIDDEN_TOP))
        assert [chunk for chunk in chunks if chunk.chunk_type is ChunkType.HEADING] == []
        assert any("# Hidden" in chunk.content for chunk in chunks)

    def test_a_commented_heading_does_not_nest_under_a_real_one(
        self, note_factory: NoteFactory
    ) -> None:
        chunks = MarkdownChunker().chunk(note_factory(raw_content=_HIDDEN_NESTED))
        assert {chunk.header_path for chunk in chunks} == {"Real"}

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (_HIDDEN_TOP, []),
            (_HIDDEN_NESTED, [{"level": 1, "text": "Real", "path": "Real"}]),
        ],
    )
    async def test_map_matches_the_heading_model(
        self, tmp_path: Path, content: str, expected: list[dict[str, Any]]
    ) -> None:
        (tmp_path / "note.md").write_text(
            f"---\nid: 01J00000000000000000000091\n---\n{content}", encoding="utf-8"
        )
        app = build_app(
            settings=Settings(read_paths=[tmp_path], vault_root=tmp_path), vault_root=tmp_path
        )
        payload = await _get_note_impl(app, id_or_path="note.md", fmt="map")
        assert "error" not in payload, payload
        assert [
            {key: heading[key] for key in ("level", "text", "path")}
            for heading in payload["headings"]
        ] == expected


class TestFenceState:
    def test_segment_fence_state_is_carried_not_rescanned(
        self, note_factory: NoteFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "".join(f"- item {index} see [[Note {index}]]\n" for index in range(_LIST_ITEMS))
        scanned: list[int] = []
        original = open_fence_after

        def counting(text: str, open_fence: OpenFence | None = None) -> OpenFence | None:
            scanned.append(len(text))
            return original(text) if open_fence is None else original(text, open_fence)

        monkeypatch.setattr("datacron.indexing.chunker.open_fence_after", counting)
        chunks = MarkdownChunker(max_tokens=_SEGMENT_TOKENS).chunk(note_factory(raw_content=body))
        assert len(chunks) > _LIST_ITEMS // 10
        # Each line is scanned once in total, not once per later segment.
        assert sum(scanned) <= len(body)
        assert sum(len(chunk.wikilinks_out) for chunk in chunks) == _LIST_ITEMS


class TestWikilinkEdgeCases:
    @pytest.mark.parametrize("chunk_type", [ChunkType.LIST, ChunkType.NARRATIVE])
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (
                "- ```git log``` shows history, see [[Git Notes]]\n- and [[Other]]\n",
                ["Git Notes", "Other"],
            ),
            ("1. ```npm ci``` then read [[Build]]\n2. [[Deploy]]\n", ["Build", "Deploy"]),
            ("```git log``` shows [[History]]\n\nthen [[After]]\n", ["History", "After"]),
            ("- ```bash\n  [[inside]]\n  ```\n- [[outside]]\n", ["outside"]),
            ("~~~ `odd` info\n[[inside]]\n~~~\n[[outside]]\n", ["outside"]),
        ],
    )
    def test_inline_triple_backticks_do_not_open_a_fence(
        self, content: str, expected: list[str], chunk_type: ChunkType
    ) -> None:
        assert extract_wikilink_targets(content, chunk_type) == expected

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("| a | b |\n|---|---|\n| [[Target\\|x]] | 2 |\n", ["Target"]),
            ("| [[Target#Section\\|label]] |\n", ["Target"]),
            ("see [[Target|label]]\n", ["Target"]),
        ],
    )
    def test_escaped_table_pipe_separates_the_display_text(
        self, content: str, expected: list[str]
    ) -> None:
        assert extract_wikilink_targets(content, ChunkType.TABLE) == expected
