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
"""Tests for :mod:`datacron.core.frontmatter`."""

from __future__ import annotations

from datetime import date

import pytest

from datacron.core.frontmatter import (
    FrontmatterError,
    extract_tags,
    has_ambiguous_leading_delimiter_block,
    matches_frontmatter_filter,
    parse,
    parse_preserving_bom_and_body_eols,
    serialize,
)


class TestParse:
    def test_with_yaml(self) -> None:
        raw = "---\ntitle: Hello\ntags: [a, b]\n---\n\n# Body\n"
        meta, body = parse(raw)
        assert meta == {"title": "Hello", "tags": ["a", "b"]}
        assert body.lstrip().startswith("# Body")

    def test_bom_prefixed_frontmatter_is_parsed(self) -> None:
        """A leading BOM must not hide a note's frontmatter.

        ``str.lstrip`` does not strip U+FEFF and the YAML parser does not skip
        it either, so the delimiter check used to miss it and every BOM-saved
        note was indexed with no title, no tags and no frontmatter id at all.
        """
        raw = "\ufeff---\ntitle: Hello\ntags: [a, b]\n---\n\n# Body\n"
        meta, body = parse(raw)
        assert meta == {"title": "Hello", "tags": ["a", "b"]}
        assert body.lstrip().startswith("# Body")
        assert "\ufeff" not in body

    def test_bom_without_yaml_keeps_body_byte_exact(self) -> None:
        raw = "\ufeff# Just a body\n"
        meta, body = parse(raw)
        assert meta == {}
        assert body == raw

    def test_without_yaml(self) -> None:
        raw = "# Just a body\n"
        meta, body = parse(raw)
        assert meta == {}
        assert body == raw

    def test_empty_string(self) -> None:
        meta, body = parse("")
        assert meta == {}
        assert body == ""

    def test_invalid_yaml_raises_typed_error(self) -> None:
        raw = "---\nid: [unclosed\n---\nbody\n"

        with pytest.raises(FrontmatterError, match="while parsing"):
            parse(raw)

    def test_an_impossible_date_raises_the_typed_error(self) -> None:
        """PyYAML reports ``2024-02-30`` with a plain ValueError, not a YAMLError.

        Uncaught, it escaped every reader that degrades malformed frontmatter to
        empty metadata, so the note could not be read at all.
        """
        raw = "---\ncreated: 2024-02-30\n---\nbody\n"

        with pytest.raises(FrontmatterError, match="day is out of range"):
            parse(raw)

    def test_the_write_path_block_check_does_not_leak_an_impossible_date(self) -> None:
        """The write tools' delimiter check loads the YAML itself.

        It caught only YAMLError, so the constructor's plain ValueError escaped a
        check whose contract is to answer, not to raise. A block that cannot be
        loaded is ambiguous, whatever the loader raises for it.
        """
        raw = "---\ncreated: 2024-02-30\n---\nbody\n"

        assert has_ambiguous_leading_delimiter_block(raw) is True

    def test_lifecycle_dates_are_parsed_as_iso_strings(self) -> None:
        raw = (
            "---\n"
            "valid_from: 2026-07-17\n"
            "invalid_at: 2026-07-17T08:30:00Z\n"
            "invalidated_by: 01HQXR7K9YZ8M2N3PQRSTV4WX5\n"
            "---\n"
            "Body\n"
        )

        metadata, _body = parse(raw)

        assert metadata["valid_from"] == "2026-07-17"
        assert metadata["invalid_at"] == "2026-07-17T08:30:00+00:00"
        assert metadata["invalidated_by"] == "01HQXR7K9YZ8M2N3PQRSTV4WX5"


class TestSerialize:
    def test_round_trips_metadata_and_body(self) -> None:
        metadata = {
            "id": "01HQXR7K9YZ8M2N3PQRSTV4WX5",
            "title": "Mémoire",
            "origin": "ai",
            "confidence": "L2",
            "tags": ["datacron", "mémoire"],
        }
        body = "# Mémoire\n\nTexte préservé.\n"

        parsed_metadata, parsed_body = parse(serialize(metadata, body))

        assert parsed_metadata == metadata
        assert parsed_body == body.rstrip("\n")

    def test_key_order_is_deterministic(self) -> None:
        rendered = serialize(
            {
                "z_extra": True,
                "tags": ["memory"],
                "title": "Title",
                "id": "01HQXR7K9YZ8M2N3PQRSTV4WX5",
                "confidence": "L2",
                "alpha_extra": "first",
            },
            "Body\n",
        )

        lines = rendered.splitlines()
        assert lines[:7] == [
            "---",
            "id: 01HQXR7K9YZ8M2N3PQRSTV4WX5",
            "title: Title",
            "confidence: L2",
            "tags:",
            "- memory",
            "alpha_extra: first",
        ]
        assert "z_extra: true" in lines

    def test_lifecycle_key_order_is_deterministic(self) -> None:
        rendered = serialize(
            {
                "id": "01HQXR7K9YZ8M2N3PQRSTV4WX5",
                "origin": "human",
                "invalidated_by": "01HQXR7K9YZ8M2N3PQRSTV4WX6",
                "invalid_at": "2026-07-17T08:30:00+00:00",
                "valid_from": "2026-07-01",
            },
            "Body\n",
        )

        assert rendered.splitlines()[:7] == [
            "---",
            "id: 01HQXR7K9YZ8M2N3PQRSTV4WX5",
            "valid_from: '2026-07-01'",
            "invalid_at: '2026-07-17T08:30:00+00:00'",
            "invalidated_by: 01HQXR7K9YZ8M2N3PQRSTV4WX6",
            "origin: human",
            "---",
        ]

    def test_rejected_round_trips_between_supersedes_and_tags(self) -> None:
        metadata = {
            "id": "01HQXR7K9YZ8M2N3PQRSTV4WX5",
            "supersedes": ["01HQXR7K9YZ8M2N3PQRSTV4WX4"],
            "rejected": ["vector embeddings -- BM25 is sufficient"],
            "tags": ["memory"],
        }

        rendered = serialize(metadata, "Body\n")
        parsed_metadata, parsed_body = parse(rendered)
        lines = rendered.splitlines()

        assert parsed_metadata == metadata
        assert parsed_body == "Body"
        assert lines.index("supersedes:") < lines.index("rejected:") < lines.index("tags:")


class TestMatchesFrontmatterFilter:
    def test_scalar_and_key_matching_are_case_insensitive(self) -> None:
        assert matches_frontmatter_filter({"Origin": "AI"}, {"origin": "ai"}) is True

    def test_list_matches_any_element(self) -> None:
        assert (
            matches_frontmatter_filter(
                {"supersedes": ["01AAA", "01BBB"]},
                {"SUPERSEDES": "01bbb"},
            )
            is True
        )

    def test_all_pairs_are_required(self) -> None:
        assert (
            matches_frontmatter_filter(
                {"origin": "ai", "confidence": "high"},
                {"origin": "AI", "confidence": "low"},
            )
            is False
        )

    def test_missing_key_and_empty_frontmatter_do_not_match(self) -> None:
        assert matches_frontmatter_filter({"origin": "ai"}, {"type": "decision"}) is False
        assert matches_frontmatter_filter({}, {"origin": "ai"}) is False

    def test_non_string_values_use_string_representation(self) -> None:
        metadata = {"priority": 7, "important": True, "reviewed": date(2026, 7, 20)}
        assert (
            matches_frontmatter_filter(
                metadata,
                {"priority": "7", "important": "true", "reviewed": "2026-07-20"},
            )
            is True
        )

    def test_omitted_or_empty_filter_matches_any_frontmatter(self) -> None:
        assert matches_frontmatter_filter({}, None) is True
        assert matches_frontmatter_filter({}, {}) is True


class TestExtractTags:
    def test_list_form(self) -> None:
        tags = extract_tags({"tags": ["Alpha", "Beta"]}, "no inline tags here")
        assert tags == ["alpha", "beta"]

    def test_comma_separated_string(self) -> None:
        tags = extract_tags({"tags": "Alpha, Beta;Gamma"}, "")
        assert tags == ["alpha", "beta", "gamma"]

    def test_inline_tags(self) -> None:
        body = "Some text #datacron and #project/sub here\nAnd `#code-in-backtick`"
        tags = extract_tags({}, body)
        assert "datacron" in tags
        assert "project/sub" in tags

    def test_fenced_code_tags_are_skipped(self) -> None:
        body = """Before
```c
#region
#pragma once
#include <stdio.h>
```
After #project/datacron
"""

        assert extract_tags({}, body) == ["project/datacron"]

    def test_tilde_fenced_code_tags_are_skipped(self) -> None:
        body = """Before
~~~python
#region
~~~
After #datacron
"""

        assert extract_tags({}, body) == ["datacron"]

    def test_fenced_code_hex_colors_are_skipped(self) -> None:
        body = """```css
.theme { background: #bd93f9; color: #ffb86c; }
```
See #project/themeforge
"""

        assert extract_tags({}, body) == ["project/themeforge"]

    def test_inline_code_spans_are_skipped(self) -> None:
        body = "Run `#region #pragma #include` before #project/datacron."

        assert extract_tags({}, body) == ["project/datacron"]

    def test_inline_hex_color_shape_in_prose_skipped(self) -> None:
        body = "Accent #bd93f9 partout, keep #project/themeforge."

        assert extract_tags({}, body) == ["project/themeforge"]

    def test_three_digit_hex_boundary_keeps_real_word_tag(self) -> None:
        body = "Drop #fff but keep #uid."

        assert extract_tags({}, body) == ["uid"]

    def test_frontmatter_hex_tags_are_kept(self) -> None:
        body = "Drop inline #bd93f9 and #fff, keep #uid."

        assert extract_tags({"tags": ["bd93f9", "fff"]}, body) == ["bd93f9", "fff", "uid"]

    def test_inline_numeric_refs_and_digit_prefixed_ids_skipped(self) -> None:
        body = "Issue #47 and color #1e8f4f are refs, but #project/sub is a tag"
        tags = extract_tags({}, body)
        assert tags == ["project/sub"]
        assert extract_tags({"tags": ["47"]}, body) == ["47", "project/sub"]

    def test_dedup_and_ordering(self) -> None:
        body = "first #alpha then #beta then #alpha again"
        tags = extract_tags({"tags": ["beta", "Gamma"]}, body)
        assert tags == ["beta", "gamma", "alpha"]

    def test_inline_in_url_skipped(self) -> None:
        body = "see https://example.com/page#section for details"
        tags = extract_tags({}, body)
        assert "section" not in tags

    def test_no_tags(self) -> None:
        assert extract_tags({}, "nothing here") == []


class TestLeadingDelimiterBlockThatIsNotFrontmatter:
    """A leading --- block whose YAML is not a mapping must not cut off the body.

    python-frontmatter reports empty metadata for such a block, while the exact
    body span was computed from the delimiter lines alone. The two disagreed
    about where the note started, and every mutation tool wrote back only the
    tail, deleting everything above the second delimiter.
    """

    _THEMATIC_BREAK_NOTE = (
        "---\n\n# Project notes\n\nImportant content.\n\n---\n\nRest of the note.\n"
    )

    def test_a_thematic_break_keeps_the_whole_document_as_body(self) -> None:
        metadata, body, has_bom = parse_preserving_bom_and_body_eols(self._THEMATIC_BREAK_NOTE)

        assert metadata == {}
        assert not has_bom
        assert body == self._THEMATIC_BREAK_NOTE
        assert "# Project notes" in body
        assert "Important content." in body

    def test_a_yaml_sequence_block_keeps_the_whole_document_as_body(self) -> None:
        raw = "---\n- alpha\n- beta\n---\n\n# Title\n\nbody\n"

        metadata, body, _ = parse_preserving_bom_and_body_eols(raw)

        assert metadata == {}
        assert body == raw

    def test_a_real_mapping_still_has_its_block_removed(self) -> None:
        raw = "---\nid: 01J5S0C0000000000000000001\n---\n\n# Title\n\nbody\n"

        metadata, body, _ = parse_preserving_bom_and_body_eols(raw)

        assert metadata == {"id": "01J5S0C0000000000000000001"}
        assert body == "\n# Title\n\nbody\n"

    def test_an_empty_mapping_block_still_has_its_block_removed(self) -> None:
        raw = "---\n---\n\n# Title\n"

        metadata, body, _ = parse_preserving_bom_and_body_eols(raw)

        assert metadata == {}
        assert body == "\n# Title\n"

    @pytest.mark.parametrize(
        "raw",
        ["---\nIntro para\n\n## A\n\nb\n  \n", "\ufeff---\r\n\r\n# N\r\n\r\nbody\r\n"],
    )
    def test_an_unclosed_opening_delimiter_keeps_the_exact_text_as_body(self, raw: str) -> None:
        """The stripped python-frontmatter text lost the leading and trailing whitespace."""
        metadata, body, has_bom = parse_preserving_bom_and_body_eols(raw)

        assert metadata == {}
        assert body == raw.removeprefix("\ufeff")
        assert has_bom is raw.startswith("\ufeff")


class TestHasAmbiguousLeadingDelimiterBlock:
    """Tell a real frontmatter block from a leading --- that only looks like one."""

    @pytest.mark.parametrize(
        "raw",
        [
            "---\n\n# Project notes\n\nImportant content.\n\n---\n\nRest of the note.\n",
            "---\n- alpha\n- beta\n---\n\n# Title\n",
            "---\njust a scalar\n---\n\n# Title\n",
        ],
    )
    def test_reports_a_block_whose_yaml_is_not_a_mapping(self, raw: str) -> None:
        assert has_ambiguous_leading_delimiter_block(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "---\nid: 01J5S0C0000000000000000001\n---\n\n# Title\n",
            "---\n---\n\n# Title\n",
            "# Title\n\nbody\n",
            "",
            "\ufeff---\nid: 01J5S0C0000000000000000001\n---\n\n# Title\n",
        ],
    )
    def test_accepts_a_real_block_an_empty_block_and_no_block(self, raw: str) -> None:
        assert has_ambiguous_leading_delimiter_block(raw) is False

    @pytest.mark.parametrize(
        "raw",
        [
            "---\rid: 01J5S0C0000000000000000001\r---\r# Title\r",
            "---\nid: 01J5S0C0000000000000000001\r---\r# Title\r",
        ],
        ids=["bare-cr", "bare-cr-closing"],
    )
    def test_reports_a_block_whose_delimiters_the_parser_cannot_see(self, raw: str) -> None:
        """python-frontmatter needs a newline after each delimiter; the span finder does not."""
        assert has_ambiguous_leading_delimiter_block(raw) is True

    def test_accepts_crlf_delimiters(self) -> None:
        raw = "---\r\nid: 01J5S0C0000000000000000001\r\n---\r\n# Title\r\n"
        assert has_ambiguous_leading_delimiter_block(raw) is False
