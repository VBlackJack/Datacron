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
    """

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
        document = block_token.Document(source_lines)
        blocks = list(document.children or [])
        if not blocks:
            return [self._build_chunk(note, [], ChunkType.NARRATIVE, "")]

        chunks: list[Chunk] = []
        headings: list[str] = []
        ordinal_counters: dict[str, int] = {}

        for index, token in enumerate(blocks):
            content = _raw_block_content(source_lines, blocks, index)
            chunk_type = _chunk_type_for_token(token)
            lang = _code_language(token) if chunk_type is ChunkType.CODE else None

            if _is_heading(token):
                headings = _updated_heading_stack(
                    headings,
                    _heading_level(token),
                    _token_text(token),
                )

            chunk_headings = list(headings)
            chunk = self._build_chunk(
                note=note,
                headings=chunk_headings,
                chunk_type=chunk_type,
                content=content,
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


def _raw_block_content(source_lines: list[str], blocks: list[Any], index: int) -> str:
    start = max(int(getattr(blocks[index], "line_number", 1)) - 1, 0)
    if index + 1 < len(blocks):
        end = max(int(getattr(blocks[index + 1], "line_number", len(source_lines) + 1)) - 1, start)
    else:
        end = len(source_lines)
    return _join_without_outer_blank_lines(source_lines[start:end])


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
