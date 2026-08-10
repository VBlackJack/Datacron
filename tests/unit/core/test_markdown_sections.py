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
"""Tests for pure Markdown section helpers."""

from __future__ import annotations

import pytest

from datacron.core.markdown_sections import find_section_span


def test_rename_atx_heading_line_preserves_prefix_level_separator_and_eol() -> None:
    from datacron.core.markdown_sections import rename_atx_heading_line

    assert rename_atx_heading_line("   ###\t  Old title\r\n", "New title") == (
        "   ###\t  New title\r\n"
    )


def test_rename_atx_heading_line_refuses_non_heading() -> None:
    from datacron.core.markdown_sections import rename_atx_heading_line

    with pytest.raises(ValueError, match="ATX heading line"):
        rename_atx_heading_line("plain text\n", "New title")


def test_heading_occurrence_selects_first_and_second_same_level_sections() -> None:
    lines = [
        "# Root\n",
        "## Same\n",
        "First.\n",
        "### Child\n",
        "Child.\n",
        "## Same\n",
        "Second.\n",
        "# Next\n",
    ]

    assert find_section_span(lines, "Same", 2, heading_occurrence=1) == (2, 5)
    assert find_section_span(lines, "Same", 2, heading_occurrence=2) == (6, 7)


def test_heading_occurrence_absence_preserves_unique_and_ambiguous_behavior() -> None:
    unique_lines = ["# Root\n", "## Unique\n", "Body.\n"]
    duplicate_lines = ["# Root\n", "## Same\n", "First.\n", "## Same\n", "Second.\n"]

    assert find_section_span(unique_lines, "Unique", 2) == (2, 3)
    with pytest.raises(
        ValueError,
        match=(
            r"^heading is ambiguous \(2 matches\); pass heading_level for inter-level "
            r"matches, or pass heading_level, heading_occurrence, and expected_hash for "
            r"same-level duplicates$"
        ),
    ):
        find_section_span(duplicate_lines, "Same", 2)


def test_heading_occurrence_applies_after_heading_level_filter() -> None:
    lines = [
        "# Root\n",
        "## Same\n",
        "Outer.\n",
        "### Same\n",
        "First inner.\n",
        "### Same\n",
        "Second inner.\n",
    ]

    assert find_section_span(lines, "Same", 3, heading_occurrence=2) == (6, 7)


@pytest.mark.parametrize("heading_occurrence", [0, -1])
def test_heading_occurrence_rejects_non_positive_values(heading_occurrence: int) -> None:
    with pytest.raises(ValueError, match=r"^heading_occurrence must be at least 1$"):
        find_section_span(
            ["## Same\n", "Body.\n"],
            "Same",
            2,
            heading_occurrence=heading_occurrence,
        )


@pytest.mark.parametrize("heading_occurrence", [True, 1.5, "1"])
def test_heading_occurrence_rejects_bool_and_non_integer_values(
    heading_occurrence: object,
) -> None:
    with pytest.raises(ValueError, match=r"^heading_occurrence must be an integer$"):
        find_section_span(
            ["## Same\n", "Body.\n"],
            "Same",
            2,
            heading_occurrence=heading_occurrence,  # type: ignore[arg-type]
        )


def test_heading_occurrence_reports_out_of_range_after_filtering() -> None:
    lines = ["## Same\n", "First.\n", "## Same\n", "Second.\n"]

    with pytest.raises(
        ValueError,
        match=r"^heading_occurrence 3 is out of range for 2 matching headings$",
    ):
        find_section_span(lines, "Same", 2, heading_occurrence=3)


def test_heading_occurrence_reports_out_of_range_when_no_heading_matches() -> None:
    with pytest.raises(
        ValueError,
        match=r"^heading_occurrence 1 is out of range for 0 matching headings$",
    ):
        find_section_span(["## Present\n", "Body.\n"], "Absent", 2, heading_occurrence=1)
