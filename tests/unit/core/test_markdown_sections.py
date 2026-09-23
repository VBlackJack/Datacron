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

from datacron.core.markdown_headings import MarkdownHeading, markdown_headings
from datacron.core.markdown_sections import (
    find_section_span,
    heading_ancestry,
    parse_heading_line,
    rename_atx_heading_line,
)
from datacron.mcp.tools.write_validation import _validate_rename_note_section_request


def test_patch_note_preamble_replaces_and_normalizes_before_preserved_suffix() -> None:
    from datacron.core.markdown_sections import patch_note_preamble

    suffix = "# Root\r\n\r\nBody.\r\n"
    body = f"Old preamble.\r\n\r\n{suffix}"

    assert patch_note_preamble(body, "\r\nNew line.\r\nSecond line.\r\n") == (
        f"New line.\nSecond line.\n\n{suffix}"
    )


def test_patch_note_preamble_inserts_before_first_h2_and_deletes_with_whitespace() -> None:
    from datacron.core.markdown_sections import patch_note_preamble

    suffix = "## First\n\nBody.\n"

    assert patch_note_preamble(suffix, "Added.") == f"Added.\n\n{suffix}"
    assert patch_note_preamble(f"Old.\n\n{suffix}", " \t\r\n ") == suffix


def test_patch_note_preamble_refuses_body_without_recognized_atx_heading() -> None:
    from datacron.core.markdown_sections import patch_note_preamble

    with pytest.raises(
        ValueError,
        match=r"^no Markdown heading found; refusing to replace the entire note body$",
    ):
        patch_note_preamble("Preamble only.\n", "Replacement.")


def test_patch_note_preamble_refuses_identical_rendering() -> None:
    from datacron.core.markdown_sections import patch_note_preamble

    with pytest.raises(
        ValueError,
        match=r"^preamble is unchanged; nothing to patch$",
    ):
        patch_note_preamble("Same.\n\n# Root\n", "\nSame.\n")

    with pytest.raises(
        ValueError,
        match=r"^preamble is unchanged; nothing to patch$",
    ):
        patch_note_preamble("Same.\r\n\r\n# Root\r\n", "Same.")


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


def _heading(level: int, text: str) -> MarkdownHeading:
    return MarkdownHeading(start=0, end=1, level=level, text=text)


def test_heading_ancestry_keeps_actual_ancestors_only() -> None:
    """Levels that rise, fall, skip one, and repeat at the same depth."""
    headings = [
        _heading(1, "Root"),
        _heading(2, "Child"),
        _heading(3, "Grandchild"),
        _heading(2, "Sibling"),
        _heading(2, "Sibling again"),
        _heading(4, "Skipped a level"),
        _heading(1, "Second root"),
    ]

    trails = heading_ancestry(headings)

    assert [[item.text for item in trail] for trail in trails] == [
        ["Root"],
        ["Root", "Child"],
        ["Root", "Child", "Grandchild"],
        ["Root", "Sibling"],
        ["Root", "Sibling again"],
        ["Root", "Sibling again", "Skipped a level"],
        ["Second root"],
    ]


def test_heading_ancestry_without_a_root_and_with_independent_trails() -> None:
    trails = heading_ancestry(
        [_heading(3, "Deep first"), _heading(2, "Shallower"), _heading(2, "Peer")]
    )

    assert [[item.text for item in trail] for trail in trails] == [
        ["Deep first"],
        ["Shallower"],
        ["Peer"],
    ]
    trails[0].append(_heading(4, "mutated"))
    assert len(trails[1]) == 1
    assert heading_ancestry([]) == []


class TestHeadingsInsideHtmlComments:
    """A heading inside <!-- --> is not a heading, and editing must not move it.

    mistletoe carries no HTML block token, so a commented-out draft section used
    to be reported as live. A move then carried the closing marker away with the
    section and buried everything that followed it, while the move verifier saw
    an unchanged heading sequence and reported that all bytes were preserved.
    """

    def test_a_commented_out_section_is_not_reported(self) -> None:
        body = "## Kept\n\ntext\n\n<!--\n## Draft\n\nnot ready\n-->\n\n## Next\n"

        headings = markdown_headings(body.splitlines(keepends=True))

        assert [item.text for item in headings] == ["Kept", "Next"]

    def test_a_single_line_comment_hides_its_heading(self) -> None:
        body = "## A\n\n<!-- ## Hidden -->\n\n## B\n"

        headings = markdown_headings(body.splitlines(keepends=True))

        assert [item.text for item in headings] == ["A", "B"]

    def test_an_unterminated_comment_hides_nothing(self) -> None:
        """An opener with no closing marker is a typo, and hiding cost more than it saved.

        This asserted the opposite until a property test showed the cost. With every
        heading below the orphan opener hidden, the section above it reached the end of
        the note, so patching that section replaced everything under it: exactly the
        silent content loss this class exists to prevent.

        Nothing is given up by not hiding. The damage a closed comment can suffer is a
        marker relocated or orphaned by an edit, and an unterminated comment has no
        closing marker to relocate; deleting the section holding the stray opener
        repairs the note rather than breaking it.
        """
        body = "## A\n\n<!--\n## Still A Heading\n"

        headings = markdown_headings(body.splitlines(keepends=True))

        assert [item.text for item in headings] == ["A", "Still A Heading"]

    def test_a_comment_closed_after_a_heading_still_hides_it(self) -> None:
        """The span that does close is still masked, which is the case that matters."""
        body = "## A\n\n<!--\n## Hidden\n-->\n\n## B\n"

        headings = markdown_headings(body.splitlines(keepends=True))

        assert [item.text for item in headings] == ["A", "B"]

    def test_a_comment_marker_inside_a_fence_does_not_open_a_comment(self) -> None:
        body = "## A\n\n```\n<!--\n```\n\n## B\n"

        headings = markdown_headings(body.splitlines(keepends=True))

        assert [item.text for item in headings] == ["A", "B"]


def test_a_new_heading_ending_in_hashes_is_refused() -> None:
    """A trailing closing sequence is dropped by the parser, silently.

    rename_atx_heading_line stored "## Section #" verbatim and mistletoe read
    it back as "Section": the tool reported the requested title, so the client
    believed the note held it, the next rename by that title failed with
    heading_not_found, and any stored selector built from it was dead. The
    reverse case is safe, because a line that already carried closing hashes
    has them restored.
    """
    for title in ("Section #", "Sprint ##", "Trailing\t#"):
        with pytest.raises(ValueError, match="closing sequence"):
            _validate_rename_note_section_request(
                rel_path="note.md",
                heading="Old",
                new_heading=title,
                expected_hash=None,
                heading_level=None,
                heading_occurrence=None,
            )


def test_a_hash_that_is_part_of_the_words_is_left_alone() -> None:
    """The refusal must not reach a language name or a numbered tag."""
    for title in ("C# et F#", "Tag#1", "Plain"):
        cleaned = _validate_rename_note_section_request(
            rel_path="note.md",
            heading="Old",
            new_heading=title,
            expected_hash=None,
            heading_level=None,
            heading_occurrence=None,
        )
        line = rename_atx_heading_line("## Old\n", cleaned[2])
        assert parse_heading_line(line) == (2, title)


class TestHeadingsWrittenWithInlineMarkup:
    """A caller may pass a heading as written in the note, markup included.

    Selectors compare parsed text, which drops inline markup, so the raw form
    matched nothing: append_journal created the section again on every call
    until the heading became ambiguous, and a rename to such a title was refused.
    """

    @pytest.mark.parametrize("requested", ["Releases of `datacron`", "Releases of datacron"])
    def test_append_reuses_the_section_whichever_form_is_passed(self, requested: str) -> None:
        from datacron.core.markdown_sections import append_entry_to_heading

        body = "# Log\n\n## Releases of `datacron`\n\n- first\n"

        once = append_entry_to_heading(body, requested, "- second")
        twice = append_entry_to_heading(once, requested, "- third")

        assert twice.count("## Releases of") == 1
        assert twice.endswith("- first\n\n- second\n\n- third\n")

    def test_a_created_section_is_found_again_by_the_same_string(self) -> None:
        from datacron.core.markdown_sections import append_entry_to_heading

        once = append_entry_to_heading("# Log\n", "Use **rg** flags", "- a")
        twice = append_entry_to_heading(once, "Use **rg** flags", "- b")

        assert twice.count("## Use **rg** flags") == 1
