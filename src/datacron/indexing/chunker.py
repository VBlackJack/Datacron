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
"""Markdown AST chunking for Datacron notes."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any, Final, final

from mistletoe import block_token

from datacron.core.config import DEFAULT_CHUNK_MAX_TOKENS
from datacron.core.hashing import hash_text
from datacron.core.logger import get_logger
from datacron.core.models import Chunk, ChunkType, Note

_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")
_REPEATED_DASH_PATTERN = re.compile(r"-+")
_WIKILINK_TARGET_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]"
)
_HEADING_SEPARATOR: Final[str] = " / "
_TOKEN_ESTIMATE_DIVISOR: Final[int] = 4

__all__ = ["MarkdownChunker"]


@final
class MarkdownChunker:
    """Parse Markdown notes into deterministic Datacron chunks.

    Datacron uses ``mistletoe`` rather than ``markdown-it-py`` here because
    mistletoe exposes a structured block-token tree where fenced code blocks,
    GFM tables, lists, and quotes are distinguishable before rendering.

    A single Markdown block whose estimated token count exceeds ``max_tokens``
    is split into deterministic sub-chunks on line boundaries (repeating the
    table header / code fence so each part stays self-describing), so no chunk
    blows the search token budget.
    """

    def __init__(self, max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        self._max_tokens = max_tokens
        self._max_chars = max_tokens * _TOKEN_ESTIMATE_DIVISOR

    def chunk(self, note: Note) -> list[Chunk]:
        """Return all chunks for ``note`` in document order.

        Args:
            note: A Markdown note whose ``content`` excludes frontmatter.

        Returns:
            The stable list of semantic chunks extracted from the note.

        Raises:
            Exception: Re-raises parser/model errors after logging the note path.
        """
        try:
            return self._chunk(note)
        except Exception:
            get_logger(__name__).exception("Failed to chunk note: %s", note.rel_path)
            raise

    def _chunk(self, note: Note) -> list[Chunk]:
        source_lines = note.content.splitlines(keepends=True)
        line_offset = _content_line_offset(note)
        document = block_token.Document(source_lines)
        blocks = list(document.children or [])
        if not blocks:
            line_number = max(1, line_offset + 1)
            return [
                self._build_chunk(
                    note,
                    [],
                    ChunkType.NARRATIVE,
                    "",
                    line_start=line_number,
                    line_end=line_number,
                )
            ]

        chunks: list[Chunk] = []
        headings: list[str] = []
        ordinal_counters: dict[str, int] = {}

        for index, token in enumerate(blocks):
            block_start, block_end = _block_line_range(source_lines, blocks, index)
            raw_lines = source_lines[block_start - 1 : block_end]
            chunk_type = _chunk_type_for_token(token)
            lang = _code_language(token) if chunk_type is ChunkType.CODE else None

            if _is_heading(token):
                headings = _updated_heading_stack(
                    headings,
                    _heading_level(token),
                    _token_text(token),
                )

            chunk_headings = list(headings)
            for content, rel_start, rel_end in _segment_block_content(
                raw_lines, chunk_type, self._max_chars
            ):
                chunk = self._build_chunk(
                    note=note,
                    headings=chunk_headings,
                    chunk_type=chunk_type,
                    content=content,
                    line_start=block_start + rel_start + line_offset,
                    line_end=block_start + rel_end + line_offset,
                    ordinal_counters=ordinal_counters,
                    lang=lang,
                )
                chunks.append(chunk)

        return chunks

    def _build_chunk(
        self,
        note: Note,
        headings: list[str],
        chunk_type: ChunkType,
        content: str,
        line_start: int,
        line_end: int,
        ordinal_counters: dict[str, int] | None = None,
        lang: str | None = None,
    ) -> Chunk:
        header_path = _header_path(headings)
        slug_path = _slug_header_path(headings)
        ordinal = _next_ordinal(ordinal_counters, slug_path)
        return Chunk(
            chunk_id=f"{note.id}::{slug_path}::{ordinal:04d}",
            note_id=note.id,
            note_rel_path=note.rel_path,
            header_path=header_path,
            section_title=headings[-1] if headings else None,
            chunk_type=chunk_type,
            content=content,
            ordinal=ordinal,
            content_hash=hash_text(content),
            token_count=len(content) // _TOKEN_ESTIMATE_DIVISOR,
            line_start=line_start,
            line_end=line_end,
            wikilinks_out=_extract_wikilink_targets(content),
            lang=lang,
        )


def _slug_header_path(headings: list[str]) -> str:
    """Return the slugged heading path used inside deterministic chunk IDs."""
    return "/".join(_slug_heading(heading) for heading in headings)


def _slug_heading(heading: str) -> str:
    normalized = unicodedata.normalize("NFKD", heading)
    ascii_heading = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_heading.lower()
    replaced = _NON_ALPHANUMERIC_PATTERN.sub("-", lowered)
    collapsed = _REPEATED_DASH_PATTERN.sub("-", replaced)
    return collapsed.strip("-")


def _next_ordinal(ordinal_counters: dict[str, int] | None, slug_path: str) -> int:
    if ordinal_counters is None:
        return 0
    ordinal = ordinal_counters.get(slug_path, 0)
    ordinal_counters[slug_path] = ordinal + 1
    return ordinal


def _header_path(headings: Iterable[str]) -> str:
    return _HEADING_SEPARATOR.join(headings)


def _is_heading(token: Any) -> bool:
    return isinstance(token, block_token.Heading | block_token.SetextHeading)


def _heading_level(token: Any) -> int:
    return int(getattr(token, "level", 1))


def _updated_heading_stack(headings: list[str], level: int, title: str) -> list[str]:
    retained = headings[: max(level - 1, 0)]
    retained.append(title.strip())
    return retained


def _chunk_type_for_token(token: Any) -> ChunkType:
    if _is_heading(token):
        return ChunkType.HEADING
    if isinstance(token, block_token.CodeFence | block_token.BlockCode):
        return ChunkType.CODE
    if isinstance(token, block_token.Table):
        return ChunkType.TABLE
    if isinstance(token, block_token.List):
        return ChunkType.LIST
    if isinstance(token, block_token.Quote):
        return ChunkType.QUOTE
    return ChunkType.NARRATIVE


def _code_language(token: Any) -> str | None:
    language = getattr(token, "language", None)
    if isinstance(language, str) and language.strip():
        return language.strip()
    return None


def _segment_block_content(
    raw_lines: list[str], chunk_type: ChunkType, max_chars: int
) -> list[tuple[str, int, int]]:
    """Split a block's raw source lines into budget-bounded segments.

    Returns ``(content, rel_start, rel_end)`` tuples whose ``rel_*`` are 0-based
    offsets into ``raw_lines`` (inclusive). Offsets are contiguous and gap-free
    across the block, so a ripgrep line match resolves to exactly one sub-chunk
    (resolution is first-match containment on ``line_start..line_end``). A
    repeated table header / code fence is synthetic content and never widens a
    segment's offset range. When the block fits the budget a single segment
    spanning the whole block is returned (identical to the un-split behavior).
    """
    n = len(raw_lines)
    if n == 0:
        return [("", 0, 0)]
    full = _join_without_outer_blank_lines(raw_lines)
    if max_chars <= 0 or len(full) <= max_chars:
        return [(full, 0, n - 1)]
    if chunk_type is ChunkType.TABLE:
        return _segment_table(raw_lines, max_chars)
    if chunk_type is ChunkType.CODE and _is_fence_line(raw_lines[_first_nonblank(raw_lines)]):
        return _segment_code(raw_lines, max_chars)
    return _segment_generic(raw_lines, max_chars)


def _first_nonblank(raw_lines: list[str]) -> int:
    for index, line in enumerate(raw_lines):
        if line.strip():
            return index
    return 0


def _is_fence_line(line: str) -> bool:
    return line.lstrip().startswith(("```", "~~~"))


def _brute_split_line(text: str, max_chars: int) -> list[str]:
    """Split a single over-long line into ``<= max_chars`` pieces (deterministic)."""
    if len(text) <= max_chars:
        return [text]
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def _segment_generic(raw_lines: list[str], max_chars: int) -> list[tuple[str, int, int]]:
    """Greedily group whole lines; brute-split any single line over the budget."""
    segments: list[tuple[str, int, int]] = []
    i = 0
    n = len(raw_lines)
    while i < n:
        if len(raw_lines[i].rstrip("\n")) > max_chars:
            for piece in _brute_split_line(raw_lines[i].rstrip("\n"), max_chars):
                segments.append((piece, i, i))
            i += 1
            continue
        j = i
        while j + 1 < n:
            if len(raw_lines[j + 1].rstrip("\n")) > max_chars:
                break
            if len(_join_without_outer_blank_lines(raw_lines[i : j + 2])) > max_chars:
                break
            j += 1
        segments.append((_join_without_outer_blank_lines(raw_lines[i : j + 1]), i, j))
        i = j + 1
    return segments


def _segment_table(raw_lines: list[str], max_chars: int) -> list[tuple[str, int, int]]:
    """Split a GFM table by data-row groups, repeating the header + separator."""
    if len(raw_lines) < 3:
        return [(_join_without_outer_blank_lines(raw_lines), 0, len(raw_lines) - 1)]
    prefix = f"{raw_lines[0].rstrip(chr(10))}\n{raw_lines[1].rstrip(chr(10))}"
    body = raw_lines[2:]
    segments: list[tuple[str, int, int]] = []
    i = 0
    m = len(body)
    while i < m:
        j = i
        while j + 1 < m:
            candidate = f"{prefix}\n{_join_without_outer_blank_lines(body[i : j + 2])}"
            if len(candidate) > max_chars:
                break
            j += 1
        content = f"{prefix}\n{_join_without_outer_blank_lines(body[i : j + 1])}"
        # First group covers the real header + separator lines; later groups
        # carry a synthetic header copy that does not widen their line range.
        rel_start = 0 if i == 0 else i + 2
        segments.append((content, rel_start, j + 2))
        i = j + 1
    return segments


def _segment_code(raw_lines: list[str], max_chars: int) -> list[tuple[str, int, int]]:
    """Split a fenced code block by code-line groups, repeating the fence."""
    n = len(raw_lines)
    stripped = [line.rstrip("\n") for line in raw_lines]
    open_idx = _first_nonblank(raw_lines)
    close_idx = next(
        (k for k in range(n - 1, open_idx, -1) if _is_fence_line(stripped[k])),
        -1,
    )
    if close_idx <= open_idx:
        return _segment_generic(raw_lines, max_chars)
    fence_open = stripped[open_idx]
    fence_close = stripped[close_idx]
    inner = list(range(open_idx + 1, close_idx))
    if not inner:
        return [(_join_without_outer_blank_lines(raw_lines), 0, n - 1)]
    body_budget = max(max_chars - len(fence_open) - len(fence_close) - 2, 1)

    segments: list[tuple[str, int, int]] = []
    pos = 0
    m = len(inner)
    first = True
    while pos < m:
        line_text = stripped[inner[pos]]
        if len(line_text) > body_budget:
            for piece in _brute_split_line(line_text, body_budget):
                rel_end = (n - 1) if pos == m - 1 else inner[pos]
                segments.append(
                    (f"{fence_open}\n{piece}\n{fence_close}", 0 if first else inner[pos], rel_end)
                )
                first = False
            pos += 1
            continue
        end = pos
        while end + 1 < m:
            if len(stripped[inner[end + 1]]) > body_budget:
                break
            block = "\n".join(stripped[inner[k]] for k in range(pos, end + 2))
            if len(block) > body_budget:
                break
            end += 1
        block = "\n".join(stripped[inner[k]] for k in range(pos, end + 1))
        rel_end = (n - 1) if end == m - 1 else inner[end]
        segments.append(
            (f"{fence_open}\n{block}\n{fence_close}", 0 if first else inner[pos], rel_end)
        )
        first = False
        pos = end + 1
    return segments


def _block_line_range(source_lines: list[str], blocks: list[Any], index: int) -> tuple[int, int]:
    line_start = max(int(getattr(blocks[index], "line_number", 1)), 1)
    if index + 1 < len(blocks):
        next_line_start = int(getattr(blocks[index + 1], "line_number", len(source_lines) + 1))
        line_end = max(next_line_start - 1, line_start)
    else:
        line_end = max(len(source_lines), line_start)
    return line_start, line_end


def _content_line_offset(note: Note) -> int:
    if not note.content:
        return 0
    content_start = note.raw_content.find(note.content)
    if content_start < 0:
        return 0
    return note.raw_content[:content_start].count("\n")


def _join_without_outer_blank_lines(lines: list[str]) -> str:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end]).rstrip("\n")


def _token_text(token: Any) -> str:
    parts: list[str] = []
    _append_token_text(token, parts)
    return "".join(parts).strip()


def _append_token_text(token: Any, parts: list[str]) -> None:
    children = getattr(token, "children", None) or []
    if children:
        for child in children:
            _append_token_text(child, parts)
        return
    content = getattr(token, "content", None)
    if isinstance(content, str):
        parts.append(content)


def _extract_wikilink_targets(content: str) -> list[str]:
    return [match.group(1).strip() for match in _WIKILINK_TARGET_PATTERN.finditer(content)]


# Structural conformance check for mypy.
from datacron.core.protocols import ASTChunker as _ASTChunkerProtocol  # noqa: E402


def _conformance_check(_: _ASTChunkerProtocol) -> None:
    """Mypy structural conformance: MarkdownChunker must satisfy ASTChunker Protocol."""


_conformance_check(MarkdownChunker())
