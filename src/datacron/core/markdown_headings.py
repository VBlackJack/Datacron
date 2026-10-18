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
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
"""One physical line with its terminator, or the unterminated last line."""
_LINE_TERMINATORS = ("\n", "\r")
_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


def split_markdown_lines(text: str) -> list[str]:
    """Split ``text`` into lines on ``\\r\\n``, ``\\r`` and ``\\n`` only, keeping the ends.

    This is the one line model of the chunker, the heading map and the section
    selectors. ``str.splitlines`` also breaks on U+2028, U+2029, NEL, VT, FF and the
    information separators, which no Markdown renderer and no ripgrep line number
    treats as a line end: a heading-looking run after one of them became a phantom
    heading, and every chunk line number after it drifted from the line a
    ``search_regex`` match reported, so the match resolved to the wrong chunk.
    """
    return _LINE.findall(text)


def normalized_line_ends(lines: list[str]) -> list[str]:
    """Return ``lines`` with ``\\r\\n`` and lone ``\\r`` ends rewritten as ``\\n``.

    mistletoe only knows ``\\n``; the parser input is normalised the same way by the
    chunker and the heading map so both see the same blocks.
    """
    return [line.replace("\r\n", "\n").replace("\r", "\n") for line in lines]


def _physical_lines(lines: list[str]) -> tuple[list[str], list[int]]:
    """Regroup ``lines`` into physical lines and return where each one starts.

    A caller may still split with ``str.splitlines``, which cuts a line at U+2028 or
    a form feed. An item whose predecessor did not end on ``\\r`` or ``\\n`` is the
    rest of the same physical line, so it is joined back before any heading or
    block is recognised. The returned starts map a physical index to the caller's
    index, so spans stay expressed in the caller's list.
    """
    physical: list[str] = []
    starts: list[int] = []
    for index, line in enumerate(lines):
        if physical and not physical[-1].endswith(_LINE_TERMINATORS):
            physical[-1] += line
            continue
        physical.append(line)
        starts.append(index)
    return physical, starts


_COMMENT_CLOSE = "-->"
_RAW_TEXT_TAGS = ("pre", "script", "style", "textarea")
"""CommonMark type-1 HTML block tags: their content is raw text up to the closing tag."""

# One opener per CommonMark HTML block type whose end is a fixed marker, each
# paired with that marker. Order matters: a comment and CDATA both start with
# ``<!`` and must be tried before the generic declaration.
_HTML_BLOCK_KINDS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (
        re.compile(
            rf" {{0,3}}<(?P<tag>{'|'.join(_RAW_TEXT_TAGS)})(?=[ \t>]|$)",
            re.IGNORECASE,
        ),
        None,
    ),
    (re.compile(r" {0,3}<!--"), _COMMENT_CLOSE),
    (re.compile(r" {0,3}<\?"), "?>"),
    (re.compile(r" {0,3}<!\[CDATA\["), "]]>"),
    (re.compile(r" {0,3}<![A-Za-z]"), ">"),
)


def _html_block_opener(line: str) -> tuple[int, str] | None:
    """Return where a masked HTML block opener ends on ``line`` and its closing marker."""
    for pattern, close in _HTML_BLOCK_KINDS:
        match = pattern.match(line)
        if match is None:
            continue
        if close is None:
            # A type-1 block closes on its own end tag, compared case-insensitively.
            return match.end(), f"</{match.group('tag').lower()}>"
        return match.end(), close
    return None


def _html_block_scan(lines: list[str]) -> tuple[frozenset[int], bool]:
    """Return the masked line indices, and whether a block is still open at the end."""
    close: str | None = None
    fence: str | None = None
    masked: set[int] = set()
    pending: set[int] = set()
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        if close is None:
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
            opener = _html_block_opener(line)
            if opener is None:
                continue
            search_from, close = opener
        else:
            search_from = 0
        pending.add(index)
        if line.lower().find(close, search_from) >= 0:
            close = None
            masked |= pending
            pending.clear()
    return frozenset(masked), close is not None


def leaves_html_block_open(lines: list[str]) -> bool:
    """Return whether ``lines`` open an HTML comment or raw block they never close.

    The masker discards such a span rather than hiding the rest of the note, so
    the headings below still count; but the first matching closer written later
    anywhere below would pair with it and hide every heading in between. An edit
    that inserts content can therefore ask whether that content is closed.
    """
    return _html_block_scan(_physical_lines(lines)[0])[1]


def html_comment_lines(lines: list[str]) -> frozenset[int]:
    """Return the indices of the lines that sit inside an HTML comment or raw block.

    Besides comments, the CommonMark HTML blocks that end on a fixed marker are
    masked the same way: the raw-text blocks (``<pre>``, ``<script>``, ``<style>``,
    ``<textarea>``, up to the matching close tag), processing instructions
    (``<?`` to ``?>``), declarations (``<!X`` to ``>``) and CDATA (``<![CDATA[`` to
    ``]]>``). A renderer shows a ``### Inner`` inside ``<pre>`` as literal text;
    counting it as a heading let ``move_note_section`` carry it out of the block,
    leaving an orphan ``<pre>`` that swallowed the next section, and the move
    verifier accepted it because the heading sequence was unchanged.

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
    and opens nothing. The same rules hold for every masked block kind.

    Indices are the caller's: a physical line the caller split in several items
    masks all of them.
    """
    physical, starts = _physical_lines(lines)
    masked = _html_block_scan(physical)[0]
    bounds = [*starts, len(lines)]
    return frozenset(index for line in masked for index in range(bounds[line], bounds[line + 1]))


@dataclass(frozen=True)
class MarkdownHeading:
    """Zero-based heading span, with an exclusive end including the underline."""

    start: int
    end: int
    level: int
    text: str


def heading_identity(raw_heading: str) -> str:
    """Return the identity the parser gives a heading written as ``raw_heading``.

    Selectors compare parsed text, which drops inline markup: a heading written
    ``Use `rg` flags`` is found as ``Use rg flags``. A caller copying the raw line
    from a note therefore matched nothing, so ``append_journal`` created the
    section again on every call, and a rename to such a title was refused as not
    surviving. Rendering the caller's string the same way makes both forms agree.
    """
    headings = markdown_headings([f"## {raw_heading.strip()}\n"])
    return headings[0].text if headings else raw_heading.strip()


def token_text(token: Any) -> str:
    """Return the same inline text identity for maps, chunks, and write selectors."""
    children = getattr(token, "children", None) or []
    if children:
        return "".join(token_text(child) for child in children)
    content = getattr(token, "content", None)
    return content if isinstance(content, str) else ""


def markdown_headings(lines: list[str]) -> list[MarkdownHeading]:
    """Select top-level document headings, excluding code, quotes, and list contents.

    Headings are recognised on physical lines (see :func:`split_markdown_lines`), and
    their spans are returned in the indices of ``lines``.
    """
    physical, starts = _physical_lines(lines)
    bounds = [*starts, len(lines)]
    return [
        MarkdownHeading(bounds[item.start], bounds[item.end], item.level, item.text)
        for item in _physical_headings(physical)
    ]


def _physical_headings(lines: list[str]) -> list[MarkdownHeading]:
    """Return the headings of physical ``lines``, in physical line indices."""
    document = block_token.Document(normalized_line_ends(lines))
    commented = _html_block_scan(lines)[0]
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
