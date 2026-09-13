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
"""Deterministic AST-only suggestions without changing exact selection."""

import pytest

from datacron.core.markdown_sections import HeadingNotFoundError, find_section_span


def test_missing_markup_heading_suggests_exact_rendered_identity() -> None:
    lines = "## command `apply`\n\nBody\n".splitlines(True)
    with pytest.raises(HeadingNotFoundError) as caught:
        find_section_span(lines, "command `apply`", 2)
    assert caught.value.code == "heading_not_found"
    assert caught.value.suggestions == [
        {"heading": "command apply", "heading_level": 2, "heading_occurrence": 1}
    ]
    assert "command apply" not in str(caught.value)
    assert find_section_span(lines, "command apply", 2) == (1, 3)


def test_suggestions_filter_ast_level_and_count_occurrences() -> None:
    lines = (
        "```\n## Section\n```\n\n> ## Section\n\n### Section\n\nSection\n---\n\n## Section\n"
    ).splitlines(True)
    with pytest.raises(HeadingNotFoundError) as caught:
        find_section_span(lines, "Sectoin", 2)
    assert caught.value.suggestions == [
        {"heading": "Section", "heading_level": 2, "heading_occurrence": 1},
        {"heading": "Section", "heading_level": 2, "heading_occurrence": 2},
    ]


def test_similarity_ranking_is_stable_and_bounded() -> None:
    lines = "".join(f"## Section {number}\n\n" for number in range(12)).splitlines(True)
    with pytest.raises(HeadingNotFoundError) as caught:
        find_section_span(lines, "Section 0x", 2)
    suggestions = caught.value.suggestions
    assert len(suggestions) == 5
    assert suggestions[0]["heading"] == "Section 0"
    with pytest.raises(HeadingNotFoundError) as repeated:
        find_section_span(lines, "Section 0x", 2)
    assert repeated.value.suggestions == suggestions


def test_unrelated_heading_does_not_produce_arbitrary_suggestion() -> None:
    with pytest.raises(HeadingNotFoundError) as caught:
        find_section_span(["## ABCD\n"], "zzzz", 2)
    assert caught.value.suggestions == []
