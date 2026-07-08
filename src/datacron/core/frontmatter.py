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
"""Thin wrapper around ``python-frontmatter`` for the Datacron core.

The vault reader uses :func:`parse` to split a Markdown file into its YAML
header (as a ``dict``) and body. Tag handling is centralized in
:func:`extract_tags` so the vault reader and any future indexer share the
same normalization rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

import frontmatter
import yaml

__all__ = ["FrontmatterError", "extract_tags", "parse", "serialize"]

_FENCED_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*(?=\n|$)"
)
_INLINE_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"`[^`\n]*`")
_INLINE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\S)#([A-Za-z_][A-Za-z0-9_\-/]*)")
_HEX_COLOR_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)^(?:[0-9a-f]{3}|[0-9a-f]{6})$")
_FRONTMATTER_KEY_ORDER: Final[tuple[str, ...]] = (
    "id",
    "title",
    "created",
    "updated",
    "origin",
    "confidence",
    "last_verified",
    "supersedes",
    "tags",
)


class FrontmatterError(Exception):
    """Raised when a note's YAML frontmatter cannot be parsed."""


def parse(raw: str) -> tuple[dict[str, Any], str]:
    """Split ``raw`` into a YAML frontmatter mapping and body.

    Returns a tuple ``(metadata, body)``. If the file has no frontmatter
    delimiters the metadata dict is empty and the body is the original text
    (byte-for-byte; trailing newlines are preserved).
    """
    if not raw.lstrip().startswith("---"):
        return {}, raw
    try:
        post = frontmatter.loads(raw)
    except yaml.YAMLError as exc:
        raise FrontmatterError(str(exc)) from exc
    metadata: dict[str, Any] = dict(post.metadata)
    return metadata, post.content


def serialize(metadata: Mapping[str, Any], body: str) -> str:
    """Return Markdown text with deterministic YAML frontmatter."""
    ordered: dict[str, Any] = {}
    for key in _FRONTMATTER_KEY_ORDER:
        if key in metadata:
            ordered[key] = metadata[key]
    for key in sorted(key for key in metadata if key not in ordered):
        ordered[key] = metadata[key]

    dumped = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n{body}"


def _coerce_tag_iterable(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _strip_code_spans(body: str) -> str:
    """Blank Markdown code regions before heuristic inline-tag scanning.

    This preserves line breaks and character positions enough for the
    whitespace lookbehind in ``_INLINE_TAG_PATTERN`` while avoiding a full
    Markdown parser.
    """
    without_fences = _FENCED_CODE_PATTERN.sub(_blank_match, body)
    return _INLINE_CODE_PATTERN.sub(_blank_match, without_fences)


def _blank_match(match: re.Match[str]) -> str:
    return "".join("\n" if char == "\n" else " " for char in match.group(0))


def extract_tags(metadata: dict[str, Any], body: str) -> list[str]:
    """Return the deduplicated, lowercase tag set for a note.

    Sources, in order:
      1. The ``tags`` frontmatter key (list, comma-separated string, or scalar).
      2. Inline ``#tag`` occurrences in ``body``.

    Tags keep their original ``foo/bar`` slash separators, are lowercased, and
    preserve first-seen order.
    """
    candidates: list[str] = []
    candidates.extend(_coerce_tag_iterable(metadata.get("tags")))
    scrubbed_body = _strip_code_spans(body)
    candidates.extend(
        match.group(1)
        for match in _INLINE_TAG_PATTERN.finditer(scrubbed_body)
        # Prose tags exactly shaped like CSS hex colors are dropped; frontmatter
        # tags remain authoritative and are never filtered.
        if not _HEX_COLOR_TAG_PATTERN.fullmatch(match.group(1))
    )

    seen: set[str] = set()
    ordered: list[str] = []
    for tag in candidates:
        normalized = tag.lstrip("#").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
