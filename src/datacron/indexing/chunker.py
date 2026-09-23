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
from dataclasses import dataclass
from typing import Any, Final, final

from mistletoe import block_token

from datacron.core.config import DEFAULT_CHUNK_MAX_TOKENS, TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.hashing import hash_text
from datacron.core.logger import get_logger
from datacron.core.markdown_headings import token_text
from datacron.core.markdown_sections import heading_ancestry
from datacron.core.models import Chunk, ChunkType, Note
from datacron.indexing.wikilinks import OpenFence, extract_wikilink_targets, open_fence_after

_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")
_REPEATED_DASH_PATTERN = re.compile(r"-+")
_HEADING_SEPARATOR: Final[str] = " / "
# How far back a cut may move to avoid slicing a wikilink in half. A target is
# a note name, not a paragraph, so this is generous; the bound is what keeps an
# adversarial line of nothing but "[[" from turning the scan quadratic.
_WIKILINK_SCAN_WINDOW: Final[int] = 256

__all__ = ["MarkdownChunker", "content_line_offset"]


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
        self._max_chars = max_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN

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
        line_offset = content_line_offset(note)
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
        # Heading trails are computed once for the whole note; the frontmatter title
        # is not a virtual H1, so a trail only holds actual ancestors.
        trails = iter(
            heading_ancestry(
                [
                    _HeadingToken(_heading_level(token), _token_text(token).strip())
                    for token in blocks
                    if _is_heading(token)
                ]
            )
        )
        chunk_headings: list[str] = []
        ordinal_counters: dict[str, int] = {}

        # Every range below is derived from a block's line number, so a source
        # line mistletoe consumes without emitting a token is covered only when
        # some block starts before it. A link reference definition at the top of
        # a note emits nothing and is consumed into doc.footnotes: its lines
        # belonged to no chunk, so the URL was absent from the index and a
        # search for it returned a clean empty result. Lines consumed in the
        # middle of a note are already covered by the previous block's end.
        leading_end = max(int(getattr(blocks[0], "line_number", 1)), 1) - 1
        leading_lines = source_lines[:leading_end]
        if any(line.strip() for line in leading_lines):
            for content, rel_start, rel_end in _segment_block_content(
                leading_lines, ChunkType.NARRATIVE, self._max_chars
            ):
                chunks.append(
                    self._build_chunk(
                        note=note,
                        headings=chunk_headings,
                        chunk_type=ChunkType.NARRATIVE,
                        content=content,
                        line_start=1 + rel_start + line_offset,
                        line_end=1 + rel_end + line_offset,
                        ordinal_counters=ordinal_counters,
                    )
                )

        for index, token in enumerate(blocks):
            block_start, block_end = _block_line_range(source_lines, blocks, index)
            raw_lines = source_lines[block_start - 1 : block_end]
            chunk_type = _chunk_type_for_token(token)
            lang = _code_language(token) if chunk_type is ChunkType.CODE else None

            if _is_heading(token):
                chunk_headings = [item.title for item in next(trails)]
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
                    open_fence=(
                        open_fence_after("".join(raw_lines[:rel_start]))
                        if rel_start and chunk_type is not ChunkType.CODE
                        else None
                    ),
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
        open_fence: OpenFence | None = None,
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
            token_count=len(content) // TOKEN_ESTIMATE_CHARS_PER_TOKEN,
            line_start=line_start,
            line_end=line_end,
            wikilinks_out=extract_wikilink_targets(content, chunk_type, open_fence=open_fence),
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


@dataclass(frozen=True)
class _HeadingToken:
    """A heading block reduced to what a trail needs: its level and its text."""

    level: int
    title: str


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


def _cut_before_open_wikilink(text: str, start: int, end: int) -> int:
    """Pull a cut back before a ``[[`` whose ``]]`` lies past it.

    A cut at a fixed offset can land inside a wikilink, and the two halves then
    match nothing: ``extract_wikilink_targets`` runs per segment, so the link is
    absent from both and the note disappears from its own backlinks. Moving the
    cut to just before the ``[[`` keeps the span whole in the next piece. The
    scan back is bounded, and a link longer than a whole piece is left alone
    rather than made to loop.
    """
    window_start = max(start, end - _WIKILINK_SCAN_WINDOW)
    opening = text.rfind("[[", window_start, end)
    if opening <= start:
        return end
    if text.find("]]", opening, end) != -1:
        return end
    return opening


def _brute_split_line(text: str, max_chars: int) -> list[str]:
    """Split a single over-long line into ``<= max_chars`` pieces (deterministic)."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            end = _cut_before_open_wikilink(text, start, end)
        pieces.append(text[start:end])
        start = end
    return pieces


def _segment_generic(raw_lines: list[str], max_chars: int) -> list[tuple[str, int, int]]:
    """Greedily group whole lines; brute-split any single line over the budget.

    Measuring a candidate group re-joins it, which looks quadratic and was
    reported as such. Measured instead: 4000 short lines chunk in 51 ms as
    written and in 67 ms with the join replaced by a counted length, because
    str.join runs in C while the arithmetic runs in Python. An incremental
    running total would beat both, and the two attempts at one disagreed with
    the join on blank-line trimming in 374 of 4000 random shapes - which would
    move chunk boundaries, and a chunk boundary is part of a chunk's identity.
    Left as it is, deliberately.
    """
    segments: list[tuple[str, int, int]] = []
    i = 0
    n = len(raw_lines)
    while i < n:
        if len(raw_lines[i].rstrip("\n")) > max_chars:
            for piece in _brute_split_line(raw_lines[i].rstrip("\n"), max_chars):
                # NOTE (ADR-016): sub-pieces of a brute-split over-long line share the line
                # range (i, i); a ripgrep match on line i resolves to the first piece.
                # Accepted limitation - content is fully indexed.
                # See docs/en/architecture.md ADR-016.
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
    """Split a GFM table by data-row groups, repeating the header + separator.

    A single data row too long for the budget is brute-split the way
    ``_segment_code`` splits an over-long code line. Without that the first row
    of every group was emitted whatever its size, so one pasted cell produced a
    chunk half again over the budget the class docstring promises and every
    downstream consumer is sized against.
    """
    if len(raw_lines) < 3:
        # Header and separator alone, with no data row to group by. There is
        # nothing table-shaped left to preserve, so fall back rather than
        # return a segment that ignores the budget.
        return _segment_generic(raw_lines, max_chars)
    prefix = f"{raw_lines[0].rstrip(chr(10))}\n{raw_lines[1].rstrip(chr(10))}"
    body = raw_lines[2:]
    row_budget = max(max_chars - len(prefix) - 1, 1)
    segments: list[tuple[str, int, int]] = []
    i = 0
    m = len(body)
    while i < m:
        # First group covers the real header + separator lines; later groups
        # carry a synthetic header copy that does not widen their line range.
        rel_start = 0 if i == 0 else i + 2
        row = body[i].rstrip("\n")
        if len(row) > row_budget:
            for piece in _brute_split_line(row, row_budget):
                segments.append((f"{prefix}\n{piece}", rel_start, i + 2))
            i += 1
            continue
        j = i
        while j + 1 < m:
            if len(body[j + 1].rstrip("\n")) > row_budget:
                break
            candidate = f"{prefix}\n{_join_without_outer_blank_lines(body[i : j + 2])}"
            if len(candidate) > max_chars:
                break
            j += 1
        content = f"{prefix}\n{_join_without_outer_blank_lines(body[i : j + 1])}"
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


def content_line_offset(note: Note) -> int:
    """Return the number of raw lines that precede the body of ``note``.

    Chunk line numbers are body-relative; readers add this offset to report
    positions in the note as stored, frontmatter included.
    """
    if not note.content:
        return 0
    # Frontmatter parsing normalizes EOLs and strips surrounding whitespace.
    # The final occurrence is the body, even when YAML contains identical text.
    raw = note.raw_content.replace("\r\n", "\n").replace("\r", "\n")
    content = note.content.replace("\r\n", "\n").replace("\r", "\n")
    content_start = raw.rfind(content)
    if content_start < 0:
        return 0
    return raw[:content_start].count("\n")


def _join_without_outer_blank_lines(lines: list[str]) -> str:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end]).rstrip("\n")


def _token_text(token: Any) -> str:
    return token_text(token).strip()


# Structural conformance check for mypy.
from datacron.core.protocols import ASTChunker as _ASTChunkerProtocol  # noqa: E402


def _conformance_check(_: _ASTChunkerProtocol) -> None:
    """Mypy structural conformance: MarkdownChunker must satisfy ASTChunker Protocol."""


_conformance_check(MarkdownChunker())
