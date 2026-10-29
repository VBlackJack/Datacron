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
"""Pure editing of a pipe table row inside one Markdown section.

A row is identified by its first cell, compared casefolded with whitespace
collapsed. Only the own content of the section is read, so a table under a
subsection is never touched. Every edit is checked like a section splice: the
result must keep every heading of the note readable.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from datacron.core.markdown_headings import split_markdown_lines
from datacron.core.markdown_sections import (
    append_entry_to_heading,
    own_section_bounds,
    verify_spliced_headings,
)

__all__ = ["TableUpsert", "render_table_row", "section_own_text", "upsert_table_row"]

TableUpsert = Literal["created", "appended", "replaced", "unchanged"]

_CELL_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL: Final[re.Pattern[str]] = re.compile(r"^\s*:?-{3,}:?\s*$")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TABLE_PREFIX: Final[str] = "|"
_DEFAULT_EOL: Final[str] = "\n"
_MIN_TABLE_LINES: Final[int] = 2


def render_table_row(cells: Sequence[str]) -> str:
    """Render cells as one pipe table line, without its line end."""
    return "| " + " | ".join(cells) + " |"


def _cells(line: str) -> list[str]:
    text = line.strip()
    parts = _CELL_SPLIT.split(text)
    if text.startswith(_TABLE_PREFIX):
        parts = parts[1:]
    if text.endswith(_TABLE_PREFIX) and not text.endswith("\\|"):
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _key(cell: str) -> str:
    return _WHITESPACE.sub(" ", cell).strip().casefold()


def _eol(line: str) -> str:
    stripped = line.rstrip("\r\n")
    return line[len(stripped) :] or _DEFAULT_EOL


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(_SEPARATOR_CELL.match(cell) for cell in cells)


def _tables(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Return (first data row, end) for each pipe table in ``lines[start:end]``."""
    found: list[tuple[int, int]] = []
    index = start
    while index < end:
        if not lines[index].lstrip().startswith(_TABLE_PREFIX):
            index += 1
            continue
        run_start = index
        while index < end and lines[index].lstrip().startswith(_TABLE_PREFIX):
            index += 1
        if index - run_start >= _MIN_TABLE_LINES and _is_separator(lines[run_start + 1]):
            found.append((run_start + _MIN_TABLE_LINES, index))
    return found


def section_own_text(body: str, heading: str) -> str | None:
    """Return the own content of ``heading``, or ``None`` when the note has no such section."""
    lines = split_markdown_lines(body)
    bounds = own_section_bounds(lines, heading)
    if bounds is None:
        return None
    start, end = bounds
    return "".join(lines[start:end])


def upsert_table_row(
    body: str, heading: str, row: Sequence[str], header: Sequence[str]
) -> tuple[str, TableUpsert]:
    """Replace the row whose first cell matches ``row[0]``, or add it.

    A missing section is created with a table of ``header``; a section without a
    table receives one at the end of its own content; a table without the row
    receives it after its last row. The first table holding the key wins.
    """
    if not row or len(row) != len(header):
        raise ValueError("a table row needs as many cells as its header, and at least one")
    rendered_row = render_table_row(row)
    table_block = "\n".join(
        (render_table_row(header), render_table_row(["---"] * len(header)), rendered_row)
    )
    lines = split_markdown_lines(body)
    bounds = own_section_bounds(lines, heading)
    if bounds is None:
        return append_entry_to_heading(body, heading, table_block), "created"
    start, end = bounds
    tables = _tables(lines, start, end)
    if not tables:
        return append_entry_to_heading(body, heading, table_block), "appended"
    wanted = _key(row[0])
    for first, last in tables:
        for index in range(first, last):
            cells = _cells(lines[index])
            if not cells or _key(cells[0]) != wanted:
                continue
            replacement = rendered_row + _eol(lines[index])
            if replacement == lines[index]:
                return body, "unchanged"
            return _splice(lines, index, index + 1, replacement), "replaced"
    _first, last = tables[0]
    previous = lines[last - 1]
    if previous.endswith(("\n", "\r")):
        inserted = rendered_row + _eol(previous)
        return _splice(lines, last, last, inserted), "appended"
    # The table ends the note without a final line end: close its last row first.
    eol = _DEFAULT_EOL
    return _splice(lines, last - 1, last, previous + eol + rendered_row + eol), "appended"


def _splice(lines: list[str], start: int, end: int, inserted: str) -> str:
    rendered = "".join(lines[:start]) + inserted + "".join(lines[end:])
    verify_spliced_headings(lines, start, end, inserted, rendered)
    return rendered
