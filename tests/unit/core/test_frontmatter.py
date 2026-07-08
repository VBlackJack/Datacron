# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.frontmatter`."""

from __future__ import annotations

import pytest

from datacron.core.frontmatter import FrontmatterError, extract_tags, parse, serialize


class TestParse:
    def test_with_yaml(self) -> None:
        raw = "---\ntitle: Hello\ntags: [a, b]\n---\n\n# Body\n"
        meta, body = parse(raw)
        assert meta == {"title": "Hello", "tags": ["a", "b"]}
        assert body.lstrip().startswith("# Body")

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
