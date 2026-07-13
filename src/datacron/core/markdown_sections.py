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

__all__ = [
    "append_entry_to_heading",
    "find_section_span",
    "parse_heading_line",
    "section_replacement_block",
]

_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")


def append_entry_to_heading(body: str, heading: str, entry: str) -> str:
    """Append ``entry`` under ``heading``, creating a level-two section if absent."""
    lines = body.splitlines(keepends=True)
    section = _find_heading_section(lines, heading)
    if section is None:
        suffix = "" if not body else "\n\n"
        entry_block = entry if entry.endswith("\n") else f"{entry}\n"
        return f"{body}{suffix}## {heading}\n\n{entry_block}"

    _heading_index, _level, insert_at = section
    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])
    block = _entry_block(entry, prefix=prefix, suffix=suffix)
    return f"{prefix}{block}{suffix}"


def find_section_span(
    lines: list[str],
    heading: str,
    heading_level: int | None,
) -> tuple[int, int]:
    """Return the content span for one unambiguous matching heading."""
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        parsed = parse_heading_line(line)
        if parsed is None:
            continue
        level, text = parsed
        if text != heading:
            continue
        if heading_level is not None and level != heading_level:
            continue
        matches.append((index, level))

    if not matches:
        raise ValueError("heading not found; nothing to patch")
    if len(matches) > 1:
        raise ValueError(f"heading is ambiguous ({len(matches)} matches); pass heading_level")

    heading_index, level = matches[0]
    content_start = heading_index + 1
    content_end = len(lines)
    for next_index in range(content_start, len(lines)):
        next_heading = parse_heading_line(lines[next_index])
        if next_heading is not None and next_heading[0] <= level:
            content_end = next_index
            break
    return content_start, content_end


def parse_heading_line(line: str) -> tuple[int, str] | None:
    """Return a Markdown ATX heading's level and text, or ``None``."""
    match = _HEADING_HASH_PATTERN.match(line)
    if match is None:
        return None
    level = len(match.group(1))
    text = line[match.end() :].strip()
    return level, text


def section_replacement_block(new_content: str, *, prefix: str, suffix: str) -> str:
    """Render replacement section content with the existing boundary spacing."""
    leading = "\n\n" if prefix and not prefix.endswith("\n") else "\n"
    content_block = f"{new_content}\n"
    trailing = "" if not suffix or suffix.startswith("\n") else "\n"
    return f"{leading}{content_block}{trailing}"


def _find_heading_section(lines: list[str], heading: str) -> tuple[int, int, int] | None:
    for index, line in enumerate(lines):
        parsed = parse_heading_line(line)
        if parsed is None:
            continue
        level, text = parsed
        if text != heading:
            continue
        insert_at = len(lines)
        for next_index in range(index + 1, len(lines)):
            next_heading = parse_heading_line(lines[next_index])
            if next_heading is not None and next_heading[0] <= level:
                insert_at = next_index
                break
        insert_at = _trim_trailing_blank_lines(lines, index + 1, insert_at)
        return index, level, insert_at
    return None


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
