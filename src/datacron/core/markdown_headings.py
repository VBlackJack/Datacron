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
"""Shared AST heading identities and physical spans for reading and editing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mistletoe import block_token

_SETEXT = re.compile(r"^ {0,3}(?:=|-)+[ \t]*$")
"""The underline language mistletoe's own setext pattern accepts.

It reads ``(=|-)+``, which admits a mixed run: ``-=-=-`` under a line of text is
a setext heading to the parser. Requiring one repeated character here made the
parser and this module disagree about what a heading is, and the search for the
underline of a heading mistletoe had already built then found nothing at all.
"""
_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
_COMMENT_BLOCK_START = re.compile(r" {0,3}<!--")
_COMMENT_CLOSE = "-->"


def _html_comment_lines(lines: list[str]) -> frozenset[int]:
    """Return the indices of the lines that sit inside an HTML comment.

    mistletoe's default token set carries no HTML block, so ``<!--`` only opens
    an ordinary paragraph and a following ``##`` line interrupts it as a real
    ATX heading. Editing through such a heading relocates the comment markers:
    a move takes the closing ``-->`` with the section and buries whatever
    followed it, a delete orphans the opener and swallows the rest of the note.
    The heading sequence is unchanged either way, so the move verifier accepts
    it and the tool reports that every byte was preserved.

    A comment that is never closed masks nothing. An opener with no ``-->`` after
    it is a typo, not an instruction to comment out the rest of the note, and
    treating it as one hid every heading below it: the section above then reached
    the end of the file, and patching that section replaced everything under it.
    Lines are therefore held back until the closing marker is actually found, and
    a span still open at the end of the file is discarded.

    Only an opener at the start of a line counts, as in CommonMark's HTML block
    (up to three spaces of indentation). An opener anywhere in the line used to
    count, so ``<!--`` quoted in inline code or prose paired with the next ``-->``
    of any kind, a prose arrow or a mermaid ``A --> B``, and every heading in
    between vanished: patching or deleting the section above then replaced the
    sections it had swallowed. Once a block is open, the first ``-->`` closes it
    wherever it sits in the line, and the rest of that line belongs to the block
    and opens nothing.
    """
    inside_comment = False
    fence: str | None = None
    masked: set[int] = set()
    pending: set[int] = set()
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        if not inside_comment:
            fence_match = _FENCE.match(line)
            if fence_match is not None:
                marker = fence_match.group("fence")
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
                continue
            if fence is not None:
                continue
            opener = _COMMENT_BLOCK_START.match(line)
            if opener is None:
                continue
            inside_comment = True
            search_from = opener.end()
        else:
            search_from = 0
        pending.add(index)
        if line.find(_COMMENT_CLOSE, search_from) >= 0:
            inside_comment = False
            masked |= pending
            pending.clear()
    return frozenset(masked)


@dataclass(frozen=True)
class MarkdownHeading:
    """Zero-based heading span, with an exclusive end including the underline."""

    start: int
    end: int
    level: int
    text: str


def token_text(token: Any) -> str:
    """Return the same inline text identity for maps, chunks, and write selectors."""
    children = getattr(token, "children", None) or []
    if children:
        return "".join(token_text(child) for child in children)
    content = getattr(token, "content", None)
    return content if isinstance(content, str) else ""


def markdown_headings(lines: list[str]) -> list[MarkdownHeading]:
    """Select top-level document headings, excluding code, quotes, and list contents."""
    document = block_token.Document(
        [line.replace("\r\n", "\n").replace("\r", "\n") for line in lines]
    )
    commented = _html_comment_lines(lines)
    result = []
    for token in document.children or []:
        if not isinstance(token, block_token.Heading | block_token.SetextHeading):
            continue
        start = int(getattr(token, "line_number", 1)) - 1
        if start in commented:
            continue
        end = start + 1
        if isinstance(token, block_token.SetextHeading):
            # A default rather than a bare next(). The underline is a heading the
            # parser has already built, so failing to find it means this module
            # and the parser disagree, and a disagreement must degrade to a wrong
            # span rather than to StopIteration: that exception is not in any
            # write tool's expected set, and inside the generator below PEP 479
            # turns it into a RuntimeError, which left the note unreadable and
            # unwritable through every tool at once.
            end = next(
                (
                    i + 1
                    for i in range(start + 1, len(lines))
                    if _SETEXT.fullmatch(lines[i].rstrip("\r\n"))
                ),
                min(start + 2, len(lines)),
            )
        result.append(MarkdownHeading(start, end, int(token.level), token_text(token).strip()))
    return result


def heading_before(lines: list[str], content_start: int) -> MarkdownHeading:
    """Resolve a previously selected content boundary back to its heading."""
    return next(item for item in markdown_headings(lines) if item.end == content_start)
