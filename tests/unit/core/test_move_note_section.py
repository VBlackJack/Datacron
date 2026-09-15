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
"""Exact section moves and fail-closed hierarchy selection."""

import pytest

from datacron.core.markdown_sections import (
    AmbiguousHeadingError,
    HeadingNotFoundError,
    move_note_section,
)


def test_move_preserves_subtree_and_every_byte() -> None:
    source = "#### BL-0123 ###\n\nDone.\n\n##### History\n\nkept\n\n"
    body = "# Root\n\n## Active\n\n" + source + "## Archive\n\n### Prior\nold\n"
    # A H4 after the H3 child would belong to Prior, not Archive.
    with pytest.raises(ValueError, match="final child"):
        move_note_section(body, "BL-0123", "Archive")
    body = body.replace("### Prior", "#### Prior")
    result, selected = move_note_section(body, "BL-0123", "Archive")
    assert result == body.replace(source, "") + source
    assert selected["source_level"] == 4
    assert selected["destination_level"] == 2


def test_setext_destination_and_fence_are_preserved() -> None:
    body = "# Root\n\n### Item ##\n\n```\n## Archive\n```\n\nArchive\n---\n\n"
    result, _ = move_note_section(body, "Item", "Archive")
    assert result == "# Root\n\nArchive\n---\n\n### Item ##\n\n```\n## Archive\n```\n\n"


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        ("Root", "Archive", "levels 2"),
        ("Item", "Item", "same section"),
        ("Item", "History", "descendant"),
        ("Archive", "Item", "level"),
        ("Missing", "Archive", "not found"),
    ],
)
def test_invalid_moves_are_refused(source: str, destination: str, message: str) -> None:
    body = "# Root\n\n## Active\n\n### Item\n\n#### History\n\n## Archive\n"
    with pytest.raises(ValueError, match=message):
        move_note_section(body, source, destination)


def test_duplicate_selection_and_unchanged_move() -> None:
    body = "# Root\n\n## Active\n\n### Item\nfirst\n\n### Item\nsecond\n\n## Archive\n"
    with pytest.raises(ValueError, match="ambiguous"):
        move_note_section(body, "Item", "Archive")
    result, _ = move_note_section(body, "Item", "Archive", heading_level=3, heading_occurrence=2)
    assert result.endswith("## Archive\n### Item\nsecond\n\n")
    with pytest.raises(ValueError, match="unchanged"):
        move_note_section(result, "Item", "Archive", heading_level=3, heading_occurrence=2)


def test_setext_source_moves_before_its_previous_position() -> None:
    body = "# Archive\n\n# Active\n\nItem\n---\n\nkept\n"
    result, _ = move_note_section(body, "Item", "Archive")
    assert result == "# Archive\n\nItem\n---\n\nkept\n# Active\n\n"


def test_duplicate_destination_requires_occurrence() -> None:
    body = "# Root\n\n### Item\n\n## Archive\nfirst\n\n## Archive\nsecond\n"
    with pytest.raises(ValueError, match="ambiguous"):
        move_note_section(body, "Item", "Archive")
    result, _ = move_note_section(
        body, "Item", "Archive", destination_level=2, destination_occurrence=1
    )
    assert result == "# Root\n\n## Archive\nfirst\n\n### Item\n\n## Archive\nsecond\n"


def test_move_within_destination_to_last_child() -> None:
    body = "# Root\n\n## Archive\n\n### Item\n\n### Prior\n"
    result, _ = move_note_section(body, "Item", "Archive")
    assert result == "# Root\n\n## Archive\n\n### Prior\n### Item\n\n"


def test_destination_errors_name_the_destination_selector_and_parameters() -> None:
    body = "# Root\n\n### Item\n\n## Archive\nfirst\n\n## Archive\nsecond\n"
    with pytest.raises(HeadingNotFoundError) as missing:
        move_note_section(body, "Item", "Missing")
    assert missing.value.selector == "destination"
    assert str(missing.value).startswith("destination heading not found")

    with pytest.raises(AmbiguousHeadingError) as ambiguous:
        move_note_section(body, "Item", "Archive")
    assert ambiguous.value.selector == "destination"
    assert "destination_level" in str(ambiguous.value)
    assert "destination_occurrence" in str(ambiguous.value)
    assert "heading_occurrence" not in str(ambiguous.value)


def test_source_errors_keep_the_source_selector_and_parameters() -> None:
    body = "# Root\n\n### Item\nfirst\n\n### Item\nsecond\n\n## Archive\n"
    with pytest.raises(HeadingNotFoundError) as missing:
        move_note_section(body, "Missing", "Archive")
    assert missing.value.selector == "source"
    assert str(missing.value).startswith("heading not found")

    with pytest.raises(AmbiguousHeadingError) as ambiguous:
        move_note_section(body, "Item", "Archive")
    assert ambiguous.value.selector == "source"
    assert "heading_level" in str(ambiguous.value)
    assert "heading_occurrence" in str(ambiguous.value)
    assert "destination_level" not in str(ambiguous.value)
