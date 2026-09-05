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
from typing import Final

from datacron.core.markdown_headings import markdown_headings

__all__ = [
    "append_entry_to_heading",
    "find_section_span",
    "parse_heading_line",
    "patch_note_preamble",
    "rename_atx_heading_line",
    "section_replacement_block",
]

_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")


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
) -> tuple[int, int]:
    """Return the content span for one unambiguous matching heading."""
    headings = markdown_headings(lines)
    matches = [
        (item.end, item.level)
        for item in headings
        if item.text == heading and (heading_level is None or item.level == heading_level)
    ]
    content_start, level = _select_heading_match(matches, heading_occurrence)
    content_end = next(
        (item.start for item in headings if item.start >= content_start and item.level <= level),
        len(lines),
    )
    return content_start, content_end


def _select_heading_match(
    matches: list[tuple[int, int]],
    heading_occurrence: int | None,
) -> tuple[int, int]:
    if heading_occurrence is None:
        if not matches:
            raise ValueError("heading not found; nothing to patch")
        if len(matches) > 1:
            raise ValueError(
                f"heading is ambiguous ({len(matches)} matches); pass heading_level for "
                "inter-level matches, or pass heading_level, heading_occurrence, and "
                "expected_hash for same-level duplicates"
            )
        return matches[0]
    if isinstance(heading_occurrence, bool) or not isinstance(heading_occurrence, int):
        raise ValueError("heading_occurrence must be an integer")
    if heading_occurrence < 1:
        raise ValueError("heading_occurrence must be at least 1")
    if heading_occurrence > len(matches):
        raise ValueError(
            f"heading_occurrence {heading_occurrence} is out of range for "
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
