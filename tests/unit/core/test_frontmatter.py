# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.frontmatter`."""

from __future__ import annotations

from datacron.core.frontmatter import extract_tags, parse


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
