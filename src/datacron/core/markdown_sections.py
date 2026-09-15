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
"""Pure Markdown heading-section editing helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Literal, Protocol, TypedDict, TypeVar

from datacron.core.markdown_headings import MarkdownHeading, heading_before, markdown_headings

__all__ = [
    "HEADING_SUGGESTION_MAX_CHARS",
    "AmbiguousHeadingError",
    "HeadingNotFoundError",
    "SectionSelector",
    "SectionSelectorError",
    "append_entry_to_heading",
    "find_section_span",
    "heading_ancestry",
    "move_note_section",
    "parse_heading_line",
    "patch_note_preamble",
    "rename_atx_heading_line",
    "section_replacement_block",
]

HEADING_SUGGESTION_MAX_CHARS: Final[int] = 160


class _Leveled(Protocol):
    """Anything with a heading level: a parsed heading or a chunker token summary."""

    @property
    def level(self) -> int: ...


_HeadingT = TypeVar("_HeadingT", bound=_Leveled)


def heading_ancestry(headings: Iterable[_HeadingT]) -> list[list[_HeadingT]]:
    """Return, for each heading in document order, its ancestors followed by itself.

    An ancestor is the most recent earlier heading of a strictly shallower level, so a
    skipped level (H1 then H3) keeps the H1, and two consecutive headings of the same
    level are siblings. Each returned list is independent of the others.
    """
    stack: list[_HeadingT] = []
    ancestries: list[list[_HeadingT]] = []
    for heading in headings:
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        stack.append(heading)
        ancestries.append(list(stack))
    return ancestries


_HEADING_SUGGESTION_LIMIT: Final[int] = 5
_HEADING_SUGGESTION_MIN_SIMILARITY: Final[float] = 0.35
_HEADING_SUGGESTION_PREFIX_SIMILARITY: Final[float] = 0.8

_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")


class HeadingSuggestion(TypedDict):
    """Internal candidate; text must be sanitized before retrieval export."""

    heading: str
    heading_level: int
    heading_occurrence: int


SectionSelector = Literal["source", "destination"]

# The parameter names a caller must use to disambiguate each selector of a move.
_SELECTOR_PARAMETERS: Final[dict[str, tuple[str, str]]] = {
    "source": ("heading_level", "heading_occurrence"),
    "destination": ("destination_level", "destination_occurrence"),
}
_SELECTOR_SUBJECTS: Final[dict[str, str]] = {
    "source": "heading",
    "destination": "destination heading",
}


class SectionSelectorError(ValueError):
    """A heading selector did not identify exactly one section."""

    def __init__(self, message: str, *, selector: SectionSelector = "source") -> None:
        super().__init__(message)
        self.selector: SectionSelector = selector


class AmbiguousHeadingError(SectionSelectorError):
    """More than one section matches the requested heading."""

    code: Final[str] = "heading_ambiguous"


class HeadingNotFoundError(SectionSelectorError):
    """No section matches the requested heading."""

    code: Final[str] = "heading_not_found"

    def __init__(
        self,
        message: str,
        *,
        suggestions: list[HeadingSuggestion] | None = None,
        source_context: str = "",
        suggestion_spans: list[tuple[int, int]] | None = None,
        selector: SectionSelector = "source",
    ) -> None:
        super().__init__(message, selector=selector)
        self.suggestions = suggestions if suggestions is not None else []
        self.source_context = source_context
        self.suggestion_spans = suggestion_spans if suggestion_spans is not None else []


def append_entry_to_heading(body: str, heading: str, entry: str) -> str:
    """Append ``entry`` under ``heading``, creating a level-two section if absent."""
    lines = body.splitlines(keepends=True)
    section = _find_heading_section(lines, heading)
    if section is None:
        # The body now arrives with its exact trailing newlines instead of an
        # rstripped one, so normalize them here: exactly one blank line separates
        # the existing body from the created section, whatever it ended with.
        leading = body.rstrip("\n")
        separator = "" if not leading else "\n\n"
        entry_block = entry if entry.endswith("\n") else f"{entry}\n"
        return f"{leading}{separator}## {heading}\n\n{entry_block}"

    _heading_index, _level, insert_at = section
    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])
    block = _entry_block(entry, prefix=prefix, suffix=suffix)
    return f"{prefix}{block}{suffix}"


def find_section_span(
    lines: list[str],
    heading: str,
    heading_level: int | None,
    *,
    heading_occurrence: int | None = None,
    source_context: str | None = None,
    selector: SectionSelector = "source",
    typed_errors: bool = False,
) -> tuple[int, int]:
    """Return the content span for one unambiguous matching heading.

    ``selector`` names the side of a move the lookup serves; ``typed_errors`` makes an
    ambiguous or out-of-range selection raise a ``SectionSelectorError`` carrying it
    instead of a plain ``ValueError``.
    """
    headings = markdown_headings(lines)
    matches = [
        (item.end, item.level)
        for item in headings
        if item.text == heading and (heading_level is None or item.level == heading_level)
    ]
    try:
        content_start, level = _select_heading_match(
            matches, heading_occurrence, selector, typed_errors=typed_errors
        )
    except HeadingNotFoundError as exc:
        exc.suggestions = _heading_suggestions(lines, heading, heading_level)
        body = "".join(lines)
        exc.source_context = source_context if source_context is not None else body
        offset = (
            len(exc.source_context[: len(exc.source_context) - len(body)].splitlines())
            if exc.source_context.endswith(body)
            else None
        )
        for candidate in exc.suggestions:
            selected = [
                item
                for item in headings
                if item.text == candidate["heading"] and item.level == candidate["heading_level"]
            ][candidate["heading_occurrence"] - 1]
            exc.suggestion_spans.append(
                (selected.start + 1 + offset, selected.end + offset)
                if offset is not None
                else (1, len(exc.source_context.splitlines()))
            )
        raise
    content_end = next(
        (item.start for item in headings if item.start >= content_start and item.level <= level),
        len(lines),
    )
    return content_start, content_end


def _heading_suggestions(
    lines: list[str], heading: str, heading_level: int | None
) -> list[HeadingSuggestion]:
    """Rank bounded AST candidates; retain full text until boundary redaction."""
    query = heading.casefold()[:HEADING_SUGGESTION_MAX_CHARS]
    occurrences: dict[tuple[str, int], int] = {}
    ranked: list[tuple[float, int, HeadingSuggestion]] = []
    for item in markdown_headings(lines):
        key = (item.text, item.level)
        occurrences[key] = occurrences.get(key, 0) + 1
        if heading_level is not None and item.level != heading_level:
            continue
        comparable = item.text.casefold()[:HEADING_SUGGESTION_MAX_CHARS]
        similarity = SequenceMatcher(None, query, comparable, autojunk=False).ratio()
        if query and comparable.startswith(query):
            similarity = max(similarity, _HEADING_SUGGESTION_PREFIX_SIMILARITY)
        if similarity < _HEADING_SUGGESTION_MIN_SIMILARITY:
            continue
        ranked.append(
            (
                similarity,
                item.start,
                {
                    "heading": item.text,
                    "heading_level": item.level,
                    "heading_occurrence": occurrences[key],
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _, _, candidate in ranked[:_HEADING_SUGGESTION_LIMIT]]


def _select_heading_match(
    matches: list[tuple[int, int]],
    heading_occurrence: int | None,
    selector: SectionSelector = "source",
    *,
    typed_errors: bool = False,
) -> tuple[int, int]:
    subject = _SELECTOR_SUBJECTS[selector]
    level_parameter, occurrence_parameter = _SELECTOR_PARAMETERS[selector]

    def refuse(message: str, *, ambiguous: bool = False) -> ValueError:
        # Single-selector tools keep their plain ValueError contract; a move, which has
        # two selectors, asks for typed errors that name the selector that failed.
        if not typed_errors:
            return ValueError(message)
        error_type = AmbiguousHeadingError if ambiguous else SectionSelectorError
        return error_type(message, selector=selector)

    if heading_occurrence is None:
        if not matches:
            raise HeadingNotFoundError(
                f"{subject} not found; no section selected", selector=selector
            )
        if len(matches) > 1:
            raise refuse(
                f"{subject} is ambiguous ({len(matches)} matches); pass {level_parameter} "
                f"for inter-level matches, or pass {level_parameter}, {occurrence_parameter}, "
                "and expected_hash for same-level duplicates",
                ambiguous=True,
            )
        return matches[0]
    if isinstance(heading_occurrence, bool) or not isinstance(heading_occurrence, int):
        raise refuse(f"{occurrence_parameter} must be an integer")
    if heading_occurrence < 1:
        raise refuse(f"{occurrence_parameter} must be at least 1")
    if heading_occurrence > len(matches):
        raise refuse(
            f"{occurrence_parameter} {heading_occurrence} is out of range for "
            f"{len(matches)} matching headings"
        )
    return matches[heading_occurrence - 1]


def parse_heading_line(line: str) -> tuple[int, str] | None:
    """Return a Markdown ATX heading's level and text, or ``None``."""
    headings = markdown_headings([line])
    return (headings[0].level, headings[0].text) if headings else None


def patch_note_preamble(body: str, new_content: str) -> str:
    """Replace content strictly before the first recognized Markdown heading.

    Args:
        body: Markdown body without frontmatter.
        new_content: Replacement preamble. Whitespace-only content removes it.

    Returns:
        The body with a normalized preamble and the original heading suffix.

    Raises:
        ValueError: If no Markdown heading exists or the rendered body is unchanged.
    """
    lines = body.splitlines(keepends=True)
    headings = markdown_headings(lines)
    heading_index = headings[0].start if headings else None
    if heading_index is None:
        raise ValueError("no Markdown heading found; refusing to replace the entire note body")

    normalized = new_content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        normalized = ""
    suffix = "".join(lines[heading_index:])
    rendered = suffix if not normalized else f"{normalized}\n\n{suffix}"
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n")
    normalized_rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    if normalized_rendered == normalized_body:
        raise ValueError("preamble is unchanged; nothing to patch")
    return rendered


def rename_atx_heading_line(line: str, new_heading: str) -> str:
    """Replace only the text portion of one ATX heading line.

    Args:
        line: Existing ATX heading line, optionally including its line ending.
        new_heading: Validated replacement heading text.

    Returns:
        The heading line with its indentation, level, separator, and line ending preserved.

    Raises:
        ValueError: If ``line`` is not an addressable ATX heading.
    """
    match = _HEADING_HASH_PATTERN.match(line)
    if match is None:
        raise ValueError("line must be an ATX heading line")
    if line.endswith("\r\n"):
        line_ending = "\r\n"
    elif line.endswith(("\n", "\r")):
        line_ending = line[-1]
    else:
        line_ending = ""
    closing = re.search(r"[ \t]+#+[ \t]*$", line.rstrip("\r\n"))
    suffix = closing.group(0) if closing is not None else ""
    return f"{line[: match.end()]}{new_heading}{suffix}{line_ending}"


def section_replacement_block(new_content: str, *, prefix: str, suffix: str) -> str:
    """Render replacement section content with the existing boundary spacing."""
    leading = "\n\n" if prefix and not prefix.endswith("\n") else "\n"
    content_block = f"{new_content}\n"
    trailing = "" if not suffix or suffix.startswith("\n") else "\n"
    return f"{leading}{content_block}{trailing}"


def _find_heading_section(lines: list[str], heading: str) -> tuple[int, int, int] | None:
    matches = [item for item in markdown_headings(lines) if item.text == heading]
    if not matches:
        return None
    start, end = find_section_span(lines, heading, None)
    selected = matches[0]
    return selected.start, selected.level, _trim_trailing_blank_lines(lines, start, end)


def _trim_trailing_blank_lines(lines: list[str], start: int, end: int) -> int:
    insert_at = end
    while insert_at > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    return insert_at


def _entry_block(entry: str, *, prefix: str, suffix: str) -> str:
    leading = "" if not prefix else "\n\n" if not prefix.endswith("\n") else "\n"
    entry_block = entry if entry.endswith("\n") else f"{entry}\n"
    trailing = "" if not suffix or suffix.startswith("\n") else "\n"
    return f"{leading}{entry_block}{trailing}"


def move_note_section(
    body: str,
    heading: str,
    destination_heading: str,
    *,
    heading_level: int | None = None,
    heading_occurrence: int | None = None,
    destination_level: int | None = None,
    destination_occurrence: int | None = None,
    source_context: str | None = None,
) -> tuple[str, dict[str, int]]:
    """Move an exact heading subtree to an existing heading's final child position.

    Args:
        body: Exact Markdown body, without frontmatter.
        heading: Source heading text from the shared AST map.
        destination_heading: Existing destination heading text.
        heading_level: Optional source level filter.
        heading_occurrence: Optional one-based source occurrence.
        destination_level: Optional destination level filter.
        destination_occurrence: Optional one-based destination occurrence.
        source_context: Optional exact full note for contextual error redaction.

    Returns:
        The reordered body and content-free selected heading coordinates.

    Raises:
        ValueError: If selection, hierarchy or exact boundary preservation is unsafe.
    """
    lines = body.splitlines(keepends=True)
    move = _select_move(
        lines,
        heading,
        destination_heading,
        heading_level=heading_level,
        heading_occurrence=heading_occurrence,
        destination_level=destination_level,
        destination_occurrence=destination_occurrence,
        source_context=source_context,
    )
    reordered = _splice(lines, move)
    rendered = "".join(reordered)
    _verify_move(body, reordered, rendered, move)
    return rendered, {
        "source_level": move.source.level,
        "source_start_line": move.source.start + 1,
        "source_end_line": move.end,
        "destination_level": move.destination.level,
        "destination_start_line": move.destination.start + 1,
    }


@dataclass(frozen=True)
class _Move:
    """The selected source subtree and destination section of one move."""

    source: MarkdownHeading
    end: int
    destination: MarkdownHeading
    dest_end: int
    # The heading map of the body and the headings outside the moved subtree, computed
    # once at selection time and reused by the verification.
    headings: list[MarkdownHeading]
    remaining: list[MarkdownHeading]


def _select_move(
    lines: list[str],
    heading: str,
    destination_heading: str,
    *,
    heading_level: int | None,
    heading_occurrence: int | None,
    destination_level: int | None,
    destination_occurrence: int | None,
    source_context: str | None,
) -> _Move:
    """Select both sections and refuse every move the hierarchy rules forbid."""
    start, end = find_section_span(
        lines,
        heading,
        heading_level,
        heading_occurrence=heading_occurrence,
        source_context=source_context,
        selector="source",
        typed_errors=True,
    )
    dest_start, dest_end = find_section_span(
        lines,
        destination_heading,
        destination_level,
        heading_occurrence=destination_occurrence,
        source_context=source_context,
        selector="destination",
        typed_errors=True,
    )
    source = heading_before(lines, start)
    destination = heading_before(lines, dest_start)
    if source.level == 1:
        raise ValueError("move_note_section only supports source heading levels 2 through 6")
    if source.start == destination.start:
        raise ValueError("source and destination select the same section")
    if source.start < destination.start < end:
        raise ValueError("destination is a descendant of the source section")
    if source.level <= destination.level:
        raise ValueError("source level must be greater than destination level; no releveling")
    headings = markdown_headings(lines)
    remaining = [item for item in headings if not source.start <= item.start < end]
    parent = next(
        (
            item
            for item in reversed(remaining)
            if item.start < dest_end and item.level < source.level
        ),
        None,
    )
    if parent != destination:
        raise ValueError(
            "source cannot be appended as a final child without changing heading levels"
        )
    return _Move(
        source=source,
        end=end,
        destination=destination,
        dest_end=dest_end,
        headings=headings,
        remaining=remaining,
    )


def _splice(lines: list[str], move: _Move) -> list[str]:
    """Return the lines with the source subtree appended as the destination's last child."""
    moved = lines[move.source.start : move.end]
    retained = lines[: move.source.start] + lines[move.end :]
    removed_before = move.end - move.source.start if move.source.start < move.dest_end else 0
    insert_at = move.dest_end - removed_before
    return retained[:insert_at] + moved + retained[insert_at:]


def _verify_move(body: str, reordered: list[str], rendered: str, move: _Move) -> None:
    """Refuse a move that changes nothing, needs a newline or reinterprets a heading."""
    if rendered == body:
        raise ValueError("section placement is unchanged; nothing to move")
    if any(line and not line.endswith(("\n", "\r")) for line in reordered[:-1]):
        raise ValueError("move boundary requires an added newline; exact preservation refused")
    expected = _expected_headings(move)
    actual = markdown_headings(rendered.splitlines(keepends=True))
    if [(item.level, item.text) for item in actual] != [
        (item.level, item.text) for item in expected
    ]:
        raise ValueError("move changes Markdown heading interpretation; exact preservation refused")


def _expected_headings(move: _Move) -> list[MarkdownHeading]:
    """The heading sequence the move must produce: the subtree lands at its new place only."""
    expected = [item for item in move.remaining if item.start < move.dest_end]
    expected += [item for item in move.headings if move.source.start <= item.start < move.end]
    expected += [item for item in move.remaining if item.start >= move.dest_end]
    return expected
