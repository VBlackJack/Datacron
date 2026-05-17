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
"""Tests for the Markdown chunker."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from datacron.core.hashing import hash_text
from datacron.core.models import Chunk, ChunkType, Note
from datacron.indexing.chunker import MarkdownChunker, _slug_header_path

_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "chunker"
_NOTE_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_NOW = datetime(2026, 5, 17, tzinfo=UTC)


def _fixture_text(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _make_note(name: str, content: str | None = None) -> Note:
    resolved_content = _fixture_text(name) if content is None else content
    return Note(
        id=_NOTE_ID,
        path=(_FIXTURE_DIR / name).resolve(),
        rel_path=name,
        title=Path(name).stem,
        frontmatter={},
        content=resolved_content,
        raw_content=resolved_content,
        created=_NOW,
        updated=_NOW,
        content_hash="0" * 64,
    )


def _chunks_for(name: str, content: str | None = None) -> list[Chunk]:
    return MarkdownChunker().chunk(_make_note(name, content))


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        ([], ""),
        (["Architecture", "Chunking strategy"], "architecture/chunking-strategy"),
        (["Café déjà vu", "Résumé & notes"], "cafe-deja-vu/resume-notes"),
        (["  API: v2.1 / MCP  ", "FTS5 + BM25"], "api-v2-1-mcp/fts5-bm25"),
        (["Repeated---punctuation", "A__B"], "repeated-punctuation/a-b"),
    ],
)
def test_slug_header_path_follows_contract_rules(
    headings: list[str],
    expected: str,
) -> None:
    assert _slug_header_path(headings) == expected


def test_nested_headings_emit_chunks_in_document_order() -> None:
    chunks = _chunks_for("nested-headings.md")

    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.HEADING,
        ChunkType.NARRATIVE,
        ChunkType.HEADING,
        ChunkType.NARRATIVE,
        ChunkType.HEADING,
        ChunkType.NARRATIVE,
        ChunkType.HEADING,
        ChunkType.NARRATIVE,
    ]


def test_nested_heading_paths_are_human_readable() -> None:
    chunks = _chunks_for("nested-headings.md")

    assert chunks[0].header_path == "Architecture"
    assert chunks[2].header_path == "Architecture / Chunking strategy"
    assert chunks[4].header_path == "Architecture / Chunking strategy / Atomic blocks"
    assert chunks[6].header_path == "Architecture / Retrieval"


def test_nested_section_titles_use_immediate_heading() -> None:
    chunks = _chunks_for("nested-headings.md")

    assert chunks[1].section_title == "Architecture"
    assert chunks[3].section_title == "Chunking strategy"
    assert chunks[5].section_title == "Atomic blocks"
    assert chunks[7].section_title == "Retrieval"


def test_chunk_ids_use_slugged_header_path_only() -> None:
    chunks = _chunks_for("nested-headings.md")

    assert chunks[2].chunk_id == f"{_NOTE_ID}::architecture/chunking-strategy::0000"
    assert chunks[2].header_path == "Architecture / Chunking strategy"


def test_wikilinks_out_extracts_raw_targets_from_chunks() -> None:
    chunks = _chunks_for("nested-headings.md")

    assert chunks[1].wikilinks_out == ["Project Charter"]


def test_code_blocks_are_atomic_and_keep_language() -> None:
    chunks = _chunks_for("code-blocks.md")

    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.HEADING,
        ChunkType.NARRATIVE,
        ChunkType.CODE,
        ChunkType.CODE,
    ]
    assert [chunk.lang for chunk in chunks if chunk.chunk_type is ChunkType.CODE] == [
        "python",
        "bash",
    ]


def test_code_block_content_preserves_fences() -> None:
    chunks = _chunks_for("code-blocks.md")

    assert chunks[2].content == "```python\ndef answer() -> int:\n    return 42\n```"
    assert chunks[3].content == '```bash\nset -euo pipefail\necho "index"\n```'


def test_bare_code_fence_has_no_language() -> None:
    content = "# Untagged Code\n\n```\nplain text\n```"
    chunks = _chunks_for("code-blocks.md", content=content)

    assert chunks[1].chunk_type is ChunkType.CODE
    assert chunks[1].lang is None


def test_tables_are_atomic_chunks() -> None:
    chunks = _chunks_for("gfm-table.md")

    assert chunks[1].chunk_type is ChunkType.TABLE
    assert chunks[1].content.startswith("| Tool | Purpose |")
    assert "| ripgrep | Regex and exact search |" in chunks[1].content


def test_table_following_text_is_a_separate_narrative_chunk() -> None:
    chunks = _chunks_for("gfm-table.md")

    assert chunks[2].chunk_type is ChunkType.NARRATIVE
    assert chunks[2].content.startswith("After-table text belongs")


def test_lists_and_blockquotes_are_atomic_chunks() -> None:
    chunks = _chunks_for("lists-and-blockquotes.md")

    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.HEADING,
        ChunkType.LIST,
        ChunkType.LIST,
        ChunkType.QUOTE,
    ]
    assert chunks[1].content.count("\n- ") == 2
    assert chunks[2].content.startswith("1. Parse Markdown.")
    assert chunks[3].content.startswith("> Quoted material")


def test_list_chunk_extracts_wikilinks() -> None:
    chunks = _chunks_for("lists-and-blockquotes.md")

    assert chunks[1].wikilinks_out == ["Core Models"]


def test_no_heading_note_has_top_level_chunks() -> None:
    chunks = _chunks_for("no-headings.md")

    assert [chunk.header_path for chunk in chunks] == ["", "", ""]
    assert [chunk.section_title for chunk in chunks] == [None, None, None]


def test_no_heading_note_splits_paragraph_units() -> None:
    chunks = _chunks_for("no-headings.md")

    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.NARRATIVE,
        ChunkType.NARRATIVE,
        ChunkType.LIST,
    ]
    assert chunks[0].content == "Top-level paragraph with no heading."
    assert chunks[1].content == "Second paragraph with a [[Loose Link|display label]]."


def test_empty_note_produces_single_empty_narrative_chunk() -> None:
    chunks = _chunks_for("empty.md")

    assert len(chunks) == 1
    assert chunks[0].chunk_type is ChunkType.NARRATIVE
    assert chunks[0].content == ""
    assert chunks[0].header_path == ""
    assert chunks[0].section_title is None


def test_frontmatter_only_note_body_produces_single_empty_narrative_chunk() -> None:
    chunks = _chunks_for("frontmatter-only.md", content="")

    assert len(chunks) == 1
    assert chunks[0].chunk_type is ChunkType.NARRATIVE
    assert chunks[0].content == ""


def test_chunk_metadata_is_propagated_from_note() -> None:
    chunks = _chunks_for("code-blocks.md")

    assert all(chunk.note_id == _NOTE_ID for chunk in chunks)
    assert all(chunk.note_rel_path == "code-blocks.md" for chunk in chunks)


def test_chunk_hash_uses_lf_normalized_content() -> None:
    chunks = _chunks_for("code-blocks.md")

    assert chunks[2].content_hash == hash_text(chunks[2].content)


def test_token_count_uses_deterministic_divisor() -> None:
    chunks = _chunks_for("no-headings.md")

    assert chunks[0].token_count == len(chunks[0].content) // 4


def test_chunking_is_deterministic_across_runs() -> None:
    first = _chunks_for("nested-headings.md")
    second = _chunks_for("nested-headings.md")

    assert first == second


def test_chunk_ids_are_unique_with_mixed_chunk_types() -> None:
    chunks = _chunks_for("code-blocks.md")

    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_top_level_chunk_id_uses_empty_slug_segment() -> None:
    chunks = _chunks_for("no-headings.md")

    assert chunks[0].chunk_id == f"{_NOTE_ID}::::0000"


def test_escaped_wikilinks_are_not_extracted_by_chunker() -> None:
    content = r"Escaped \[[No Link]] and real [[Real Link#Heading|display]]."
    chunks = _chunks_for("no-headings.md", content=content)

    assert chunks[0].wikilinks_out == ["Real Link"]
