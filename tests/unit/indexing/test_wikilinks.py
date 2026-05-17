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
"""Tests for the regex wikilinks extractor."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from datacron.core.models import Chunk
from datacron.indexing.wikilinks import RegexWikilinksExtractor

ChunkFactory = Callable[..., Chunk]


def _extract(
    content: str,
    chunk_factory: ChunkFactory,
) -> list[tuple[str, str | None, str | None, str | None]]:
    chunk = chunk_factory(content=content)
    links = RegexWikilinksExtractor().extract(chunk)
    return [
        (
            link.target_alias,
            link.display_text,
            link.header_anchor,
            link.block_ref,
        )
        for link in links
    ]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("See [[Roadmap]].", ("Roadmap", None, None, None)),
        ("See [[Roadmap|Delivery plan]].", ("Roadmap", "Delivery plan", None, None)),
        ("See [[Roadmap#Milestones]].", ("Roadmap", None, "Milestones", None)),
        ("See [[Roadmap#^block-123]].", ("Roadmap", None, None, "block-123")),
    ],
)
def test_extracts_supported_syntax_variants(
    content: str,
    expected: tuple[str, str | None, str | None, str | None],
    chunk_factory: ChunkFactory,
) -> None:
    assert _extract(content, chunk_factory) == [expected]


def test_escaped_opening_brackets_are_ignored(chunk_factory: ChunkFactory) -> None:
    assert _extract(r"Escaped \[[Roadmap]] but [[Real Link]].", chunk_factory) == [
        ("Real Link", None, None, None)
    ]


def test_multiple_occurrences_are_returned_in_order(chunk_factory: ChunkFactory) -> None:
    assert _extract("[[Alpha]] then [[Beta|B]] then [[Alpha]].", chunk_factory) == [
        ("Alpha", None, None, None),
        ("Beta", "B", None, None),
        ("Alpha", None, None, None),
    ]


def test_multiline_wikilink_normalizes_internal_whitespace(chunk_factory: ChunkFactory) -> None:
    content = "[[Project\nCharter#Long\nHeader|Display\nText]]"

    assert _extract(content, chunk_factory) == [
        ("Project Charter", "Display Text", "Long Header", None)
    ]


def test_empty_chunk_returns_empty_list(chunk_factory: ChunkFactory) -> None:
    assert RegexWikilinksExtractor().extract(chunk_factory(content="")) == []


def test_source_chunk_id_is_preserved(chunk_factory: ChunkFactory) -> None:
    chunk = chunk_factory(content="[[Roadmap]]")
    links = RegexWikilinksExtractor().extract(chunk)

    assert links[0].source_chunk_id == chunk.chunk_id
    assert links[0].resolved_note_id is None
