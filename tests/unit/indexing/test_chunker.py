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
from itertools import pairwise
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
        (["Caf\u00e9 d\u00e9j\u00e0 vu", "R\u00e9sum\u00e9 & notes"], "cafe-deja-vu/resume-notes"),
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


def test_code_and_inline_code_do_not_emit_wikilink_targets() -> None:
    content = (
        "# Links\n\n"
        "[[Real target]] and `[[Inline false target]]`.\n\n"
        "```bash\n"
        'if [[ -f "$file" ]]; then\n'
        "  echo [[Fenced false target]]\n"
        "fi\n"
        "```\n"
    )

    chunks = _chunks_for("wikilinks.md", content=content)

    assert [target for chunk in chunks for target in chunk.wikilinks_out] == ["Real target"]


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


def test_chunk_hash_uses_exact_derived_text() -> None:
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


def test_paragraph_chunks_capture_line_ranges() -> None:
    content = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = _chunks_for("no-headings.md", content=content)

    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [
        (1, 2),
        (3, 4),
        (5, 5),
    ]


def test_code_block_line_range_spans_entire_fence() -> None:
    content = "# Code\n\n```python\na = 1\nb = 2\nc = 3\n```"
    chunks = _chunks_for("code-blocks.md", content=content)
    code_chunk = chunks[1]

    assert code_chunk.chunk_type is ChunkType.CODE
    assert code_chunk.line_end - code_chunk.line_start + 1 == 5


def test_nested_heading_line_ranges_do_not_overlap() -> None:
    content = "# Alpha\n\nText alpha.\n\n## Beta\n\nText beta.\n\n### Gamma\n\nText gamma."
    chunks = _chunks_for("nested-headings.md", content=content)

    for previous, current in pairwise(chunks):
        assert previous.line_end < current.line_start


def test_line_ranges_are_relative_to_raw_content() -> None:
    raw_content = "---\ntitle: Lines\n---\n\nFirst paragraph.\n\nSecond paragraph."
    note = _make_note("no-headings.md", content="First paragraph.\n\nSecond paragraph.")
    note = note.model_copy(update={"raw_content": raw_content})
    chunks = MarkdownChunker().chunk(note)

    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [(5, 6), (7, 7)]


# ---------------------------------------------------------------------------
# Chantier B: per-chunk size guard (max_tokens split)
# ---------------------------------------------------------------------------


def _budget_chunks(content: str, max_tokens: int) -> list[Chunk]:
    return MarkdownChunker(max_tokens=max_tokens).chunk(_make_note("budget.md", content))


def test_default_max_tokens_is_noarg_constructible() -> None:
    # The structural conformance check constructs MarkdownChunker() with no args;
    # a small note must stay a single chunk under the generous default budget.
    chunks = MarkdownChunker().chunk(_make_note("budget.md", "Short paragraph.\n"))
    assert len(chunks) == 1


def test_invalid_max_tokens_rejected() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        MarkdownChunker(max_tokens=0)


def test_oversized_narrative_splits_each_under_budget() -> None:
    content = "\n".join(f"word{i:02d}" for i in range(20)) + "\n"
    chunks = _budget_chunks(content, max_tokens=5)  # 20-char budget
    assert len(chunks) > 1
    assert all(c.chunk_type is ChunkType.NARRATIVE for c in chunks)
    assert all(c.token_count <= 5 for c in chunks)
    assert all(len(c.content) <= 20 for c in chunks)


def test_split_is_deterministic() -> None:
    content = "\n".join(f"word{i:02d}" for i in range(20)) + "\n"
    first = _budget_chunks(content, max_tokens=5)
    second = _budget_chunks(content, max_tokens=5)
    assert first == second


def test_subchunk_line_ranges_are_disjoint_and_gap_free() -> None:
    content = "\n".join(f"word{i:02d}" for i in range(20)) + "\n"
    chunks = _budget_chunks(content, max_tokens=5)
    for prev, nxt in pairwise(chunks):
        assert prev.line_end < nxt.line_start, "ranges must not overlap"
        assert nxt.line_start == prev.line_end + 1, "ranges must be contiguous (no gap)"


def test_subchunk_ids_use_successive_ordinals_no_part_segment() -> None:
    content = "\n".join(f"word{i:02d}" for i in range(20)) + "\n"
    chunks = _budget_chunks(content, max_tokens=5)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for index, chunk in enumerate(chunks):
        assert chunk.chunk_id == f"{_NOTE_ID}::::{index:04d}"  # no ::{part} segment


def test_single_long_line_is_brute_split() -> None:
    long_line = "x" * 100 + "\n"
    chunks = _budget_chunks(long_line, max_tokens=5)  # 20-char budget
    assert len(chunks) == 5
    assert all(len(c.content) <= 20 for c in chunks)
    assert "".join(c.content for c in chunks) == "x" * 100


def test_table_split_repeats_header_and_separator() -> None:
    rows = "\n".join(f"| r{i:02d}a | r{i:02d}b |" for i in range(8))
    content = f"| Col A | Col B |\n| --- | --- |\n{rows}\n"
    chunks = _budget_chunks(content, max_tokens=12)  # 48-char budget
    assert len(chunks) > 1
    assert all(c.chunk_type is ChunkType.TABLE for c in chunks)
    for chunk in chunks:
        assert chunk.content.startswith("| Col A | Col B |\n| --- | --- |")


def test_code_split_preserves_fence_and_language() -> None:
    body = "\n".join(f"call_{i:02d}();" for i in range(12))
    content = f"```python\n{body}\n```\n"
    chunks = _budget_chunks(content, max_tokens=10)  # 40-char budget
    assert len(chunks) > 1
    assert all(c.chunk_type is ChunkType.CODE for c in chunks)
    assert all(c.lang == "python" for c in chunks)
    for chunk in chunks:
        assert chunk.content.startswith("```python\n")
        assert chunk.content.endswith("\n```")
