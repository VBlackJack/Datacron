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
"""Regex-based wikilink extraction for Datacron chunks."""

from __future__ import annotations

import re
from typing import Final, final

from datacron.core.models import Chunk, Wikilink

__all__ = ["RegexWikilinksExtractor"]

_WIKILINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\[\["
    r"(?P<target>[^\]|#^]+?)"
    r"(?:#\^(?P<block>[^\]|]+?))?"
    r"(?:#(?P<header>[^\]|]+?))?"
    r"(?:\|(?P<display>[^\]]+?))?"
    r"\]\]",
    re.MULTILINE,
)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


@final
class RegexWikilinksExtractor:
    """Extract unresolved wikilink records from chunk content."""

    def extract(self, chunk: Chunk) -> list[Wikilink]:
        """Return one unresolved :class:`Wikilink` per occurrence in ``chunk``."""
        return [
            Wikilink(
                source_chunk_id=chunk.chunk_id,
                target_alias=_normalize_part(match.group("target")),
                resolved_note_id=None,
                display_text=_normalize_optional_part(match.group("display")),
                header_anchor=_normalize_optional_part(match.group("header")),
                block_ref=_normalize_optional_part(match.group("block")),
            )
            for match in _WIKILINK_PATTERN.finditer(chunk.content)
        ]


def _normalize_optional_part(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_part(value)
    return normalized or None


def _normalize_part(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


# Structural conformance check for mypy.
from datacron.core.protocols import WikilinksExtractor as _WikilinksExtractorProtocol  # noqa: E402


def _conformance_check(_: _WikilinksExtractorProtocol) -> None:
    """Mypy structural conformance: RegexWikilinksExtractor must satisfy the Protocol."""


_conformance_check(RegexWikilinksExtractor())
