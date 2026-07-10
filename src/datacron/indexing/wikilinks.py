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
"""Syntax-aware wikilink extraction for Datacron chunks."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final, final

from datacron.core.models import Chunk, ChunkType, Wikilink

__all__ = ["RegexWikilinksExtractor", "extract_wikilink_targets"]

_WIKILINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\[\["
    r"(?P<target>[^\]|#^]+?)"
    r"(?:#\^(?P<block>[^\]|]+?))?"
    r"(?:#(?P<header>[^\]|]+?))?"
    r"(?:\|(?P<display>[^\]]+?))?"
    r"\]\]",
    re.MULTILINE,
)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_FENCE_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
_INLINE_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?P<ticks>`+)[^\n]*?(?P=ticks)")
_BASH_OPERATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)(?:-[A-Za-z]|==|!=|=~|<=|>=|<|>|-nt|-ot|-ef|-n|-z)(?:\s|$)"
)
_BASH_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:^|\b)(?:if|elif|while)\s*$")


@final
class RegexWikilinksExtractor:
    """Extract unresolved wikilink records from searchable Markdown regions."""

    def extract(self, chunk: Chunk) -> list[Wikilink]:
        """Return one unresolved :class:`Wikilink` per valid occurrence."""
        return [
            Wikilink(
                source_chunk_id=chunk.chunk_id,
                target_alias=_normalize_part(match.group("target")),
                resolved_note_id=None,
                display_text=_normalize_optional_part(match.group("display")),
                header_anchor=_normalize_optional_part(match.group("header")),
                block_ref=_normalize_optional_part(match.group("block")),
            )
            for match in _iter_wikilink_matches(chunk.content, chunk.chunk_type)
        ]


def extract_wikilink_targets(content: str, chunk_type: ChunkType) -> list[str]:
    """Return normalized target aliases from searchable Markdown regions."""
    return [
        _normalize_part(match.group("target"))
        for match in _iter_wikilink_matches(content, chunk_type)
    ]


def _iter_wikilink_matches(
    content: str,
    chunk_type: ChunkType,
) -> Iterator[re.Match[str]]:
    if chunk_type is ChunkType.CODE:
        return
    excluded = _excluded_code_ranges(content)
    for match in _WIKILINK_PATTERN.finditer(content):
        if _inside(match.start(), excluded):
            continue
        if _looks_like_bash_condition(content, match.start(), match.end()):
            continue
        yield match


def _excluded_code_ranges(content: str) -> list[tuple[int, int]]:
    frontmatter = _frontmatter_range(content)
    fences = _fence_ranges(content, frontmatter)
    structural = [*([] if frontmatter is None else [frontmatter]), *fences]
    inline = [
        match.span()
        for match in _INLINE_CODE_PATTERN.finditer(content)
        if not _inside(match.start(), structural)
    ]
    return [*structural, *inline]


def _frontmatter_range(content: str) -> tuple[int, int] | None:
    offset = 1 if content.startswith("\ufeff") else 0
    lines = content[offset:].splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    position = offset + len(lines[0])
    for line in lines[1:]:
        position += len(line)
        if line.strip() == "---":
            return offset, position
    return None


def _fence_ranges(
    content: str,
    frontmatter: tuple[int, int] | None,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    position = 0
    opening: tuple[str, int, int] | None = None
    for line in content.splitlines(keepends=True):
        start = position
        end = position + len(line)
        position = end
        if frontmatter is not None and frontmatter[0] <= start < frontmatter[1]:
            continue
        if opening is None:
            match = _FENCE_OPEN_PATTERN.match(line)
            if match is not None:
                fence = match.group("fence")
                opening = (fence[0], len(fence), start)
            continue
        marker, minimum, range_start = opening
        stripped = line.lstrip(" \t")
        marker_run = len(stripped) - len(stripped.lstrip(marker))
        if marker_run >= minimum and not stripped[marker_run:].strip():
            ranges.append((range_start, end))
            opening = None
    if opening is not None:
        ranges.append((opening[2], len(content)))
    return ranges


def _inside(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _looks_like_bash_condition(content: str, start: int, end: int) -> bool:
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end < 0:
        line_end = len(content)
    prefix = content[line_start:start].strip()
    suffix = content[end:line_end].strip()
    inner = content[start + 2 : end - 2].strip()
    return (
        _BASH_OPERATOR_PATTERN.search(inner) is not None
        or _BASH_PREFIX_PATTERN.search(prefix) is not None
        or suffix.startswith("; then")
        or suffix == "then"
    )


def _normalize_optional_part(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_part(value)
    return normalized or None


def _normalize_part(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


# Structural conformance check for mypy.
from datacron.core.protocols import WikilinksExtractor as _WikilinksExtractorProtocol  # noqa: E402


def _conformance_check(_: _WikilinksExtractorProtocol) -> None:
    """Mypy structural conformance: RegexWikilinksExtractor satisfies the Protocol."""


_conformance_check(RegexWikilinksExtractor())
