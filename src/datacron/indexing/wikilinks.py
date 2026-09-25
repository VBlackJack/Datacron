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
from typing import Final, TypeAlias, final

from datacron.core.models import Chunk, ChunkType, Wikilink

__all__ = [
    "OpenFence",
    "RegexWikilinksExtractor",
    "extract_wikilink_anchors",
    "extract_wikilink_targets",
    "open_fence_after",
]

OpenFence: TypeAlias = tuple[str, int]
"""A fence open at a given point: its marker character and its length."""

_WIKILINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\[\["
    r"(?P<target>[^\]|#^]+?)"
    r"(?:#\^(?P<block>[^\]|]+?))?"
    r"(?:#(?P<header>[^\]|]+?))?"
    # Inside a table the pipe is escaped, ``[[Target\|label]]``, and the backslash
    # belongs to the separator, not to the target.
    r"(?:\\?\|(?P<display>[^\]]+?))?"
    r"\]\]",
    re.MULTILINE,
)
# A same-note anchor, ``[[#Heading]]``, has no target: the index ignores it (it never
# resolves to another note) while the offline library must verify its heading.
_ANCHOR_ONLY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\[\[#(?P<header>[^\]|#^]+?)(?:\\?\|(?P<display>[^\]]+?))?\]\]",
    re.MULTILINE,
)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
# A fence may open after a list marker ("- ```bash"): the closing line inside the
# item is then indented, and reading only indentation made that closer look like an
# opener, so the rest of the chunk was treated as code and its links were dropped.
_FENCE_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:[ \t]{0,3}|[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)(?P<fence>`{3,}|~{3,})"
)
_INLINE_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?P<ticks>`+)[^\n]*?(?P=ticks)")
_BASH_OPERATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)(?:-[A-Za-z]|==|!=|=~|<=|>=|<|>|-nt|-ot|-ef|-n|-z)(?:\s|$)"
)
_BASH_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:^|\b)(?:if|elif|while|until)\s*$")
_BASH_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:;?\s*(?:then|do)\b|&&|\|\|)")


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


def extract_wikilink_anchors(content: str, chunk_type: ChunkType) -> list[tuple[str, str | None]]:
    """Return ``(target, header)`` pairs from searchable Markdown regions, in order.

    The offline library resolves a wikilink with its header anchor attached, so it
    needs both parts as the same parser found them; ``extract_wikilink_targets``
    remains the target-only view of the same matches.
    """
    if chunk_type is ChunkType.CODE:
        return []
    excluded = _excluded_code_ranges(content)
    references: list[tuple[int, str, str | None]] = []
    for pattern in (_WIKILINK_PATTERN, _ANCHOR_ONLY_PATTERN):
        for match in pattern.finditer(content):
            if _inside(match.start(), excluded):
                continue
            if _looks_like_bash_condition(content, match.start(), match.end()):
                continue
            target = match.group("target") if "target" in match.groupdict() else ""
            references.append(
                (
                    match.start(),
                    _normalize_part(target),
                    _normalize_optional_part(match.group("header")),
                )
            )
    references.sort(key=lambda reference: reference[0])
    return [(target, header) for _, target, header in references]


def extract_wikilink_targets(
    content: str, chunk_type: ChunkType, *, open_fence: OpenFence | None = None
) -> list[str]:
    """Return normalized target aliases from searchable Markdown regions.

    ``open_fence`` names a fence already open where ``content`` starts, as
    :func:`open_fence_after` reports it for the text before a segment.
    """
    return [
        _normalize_part(match.group("target"))
        for match in _iter_wikilink_matches(content, chunk_type, open_fence)
    ]


def _iter_wikilink_matches(
    content: str,
    chunk_type: ChunkType,
    open_fence: OpenFence | None = None,
) -> Iterator[re.Match[str]]:
    if chunk_type is ChunkType.CODE:
        return
    excluded = _excluded_code_ranges(content, open_fence)
    for match in _WIKILINK_PATTERN.finditer(content):
        if _inside(match.start(), excluded):
            continue
        if _looks_like_bash_condition(content, match.start(), match.end()):
            continue
        yield match


def _excluded_code_ranges(
    content: str, open_fence: OpenFence | None = None
) -> list[tuple[int, int]]:
    frontmatter = _frontmatter_range(content)
    fences, _still_open = _fence_ranges(content, frontmatter, open_fence)
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


def open_fence_after(text: str, open_fence: OpenFence | None = None) -> OpenFence | None:
    """Return the fence still open at the end of ``text``, as ``(marker, length)``.

    ``open_fence`` is the fence already open where ``text`` starts, so a caller can
    carry the state across consecutive pieces instead of rescanning from the top.

    The chunker cuts a long block into segments and links are extracted per
    segment. A segment that starts inside a fence then begins with that fence's
    closing line, which reads as an opener, and everything after it was taken for
    code: its links vanished from the backlinks. The chunker asks this for the
    text before each segment and passes the answer to the extractor.
    """
    _ranges, still_open = _fence_ranges(text, None, open_fence)
    return None if still_open is None else (still_open[0], still_open[1])


def _fence_ranges(
    content: str,
    frontmatter: tuple[int, int] | None,
    open_fence: OpenFence | None,
) -> tuple[list[tuple[int, int]], tuple[str, int, int] | None]:
    ranges: list[tuple[int, int]] = []
    position = 0
    opening: tuple[str, int, int] | None = (
        None if open_fence is None else (open_fence[0], open_fence[1], 0)
    )
    for line in content.splitlines(keepends=True):
        start = position
        end = position + len(line)
        position = end
        if frontmatter is not None and frontmatter[0] <= start < frontmatter[1]:
            continue
        if opening is None:
            fence = _fence_opener(line)
            if fence is not None:
                opening = (fence[0], fence[1], start)
            continue
        marker, minimum, range_start = opening
        stripped = line.lstrip(" \t")
        marker_run = len(stripped) - len(stripped.lstrip(marker))
        if marker_run >= minimum and not stripped[marker_run:].strip():
            ranges.append((range_start, end))
            opening = None
    if opening is not None:
        ranges.append((opening[2], len(content)))
    return ranges, opening


def _fence_opener(line: str) -> OpenFence | None:
    """Return the fence ``line`` opens, as ``(marker, length)``, or ``None``.

    A backtick fence's info string may not contain a backtick (CommonMark), so
    "- ```git log``` shows the history" is inline code in a list item, not an
    opener. Reading it as one excluded every link after it from the backlinks.
    """
    match = _FENCE_OPEN_PATTERN.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in line[match.end() :]:
        return None
    return fence[0], len(fence)


def _inside(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _looks_like_bash_condition(content: str, start: int, end: int) -> bool:
    """Report a ``[[ ... ]]`` that is a shell test rather than a wikilink.

    Any one of these signals used to be enough, and each one fires on ordinary
    English. The keyword arm dropped "Applies only if [[Retention Policy]] is
    signed." and "Runs while [[Batch Job]] is active."; the operator arm dropped
    "See [[Plan A > Plan B]] for the comparison." on a bare ``>`` and
    "[[Runbook|use git log -n 5]]" on the ``-n``. A dropped candidate leaves no
    row at all, so ``get_backlinks`` omitted the source note and the link graph was
    incomplete with nothing to say so.

    Corroboration is required instead. A shell test carries an operator **and**
    something else that only shell has: the keyword that opens it, the syntax that
    closes it, or a variable reference between the brackets. Code fences and inline
    code are excluded upstream, so what reaches here is prose.
    """
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end < 0:
        line_end = len(content)
    prefix = content[line_start:start].strip()
    suffix = content[end:line_end].strip()
    inner = content[start + 2 : end - 2].strip()
    if _BASH_OPERATOR_PATTERN.search(inner) is None:
        return False
    return (
        _BASH_PREFIX_PATTERN.search(prefix) is not None
        or _BASH_SUFFIX_PATTERN.match(suffix) is not None
        or "$" in inner
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
