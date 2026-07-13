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
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypeVar

import frontmatter
import yaml

__all__ = [
    "FrontmatterError",
    "build_tiered_alias_index",
    "coerce_string_list",
    "extract_tags",
    "parse",
    "resolve_note_title",
    "serialize",
]

_ItemT = TypeVar("_ItemT")
_IdentityT = TypeVar("_IdentityT")

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


def coerce_string_list(
    value: object,
    *,
    split_delimited: bool = True,
    keep_empty_scalar: bool = False,
) -> list[str]:
    """Coerce a scalar or collection to stripped strings.

    Comma and semicolon splitting can be disabled for consumers whose stored
    scalar format is intentionally opaque. ``keep_empty_scalar`` preserves the
    legacy distinction between an empty scalar and an empty collection.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if split_delimited:
            parts = [part.strip() for part in value.replace(";", ",").split(",")]
            return [part for part in parts if part]
        stripped = value.strip()
        return [stripped] if stripped or keep_empty_scalar else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    stripped = str(value).strip()
    return [stripped] if stripped or keep_empty_scalar else []


def resolve_note_title(
    metadata: Mapping[str, object],
    body: str,
    path: Path,
    *,
    h1_pattern: re.Pattern[str],
    empty_h1_falls_back: bool = False,
) -> str:
    """Resolve a note title from frontmatter, first H1, then filename stem."""
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = h1_pattern.search(body)
    if heading:
        resolved = heading.group(1).strip()
        if resolved or not empty_h1_falls_back:
            return resolved
    return path.stem


def build_tiered_alias_index(
    items: Sequence[_ItemT],
    *,
    identity: Callable[[_ItemT], _IdentityT],
    title: Callable[[_ItemT], Iterable[str]],
    stem: Callable[[_ItemT], Iterable[str]],
    aliases: Callable[[_ItemT], Iterable[str]],
    normalize: Callable[[str], str],
) -> dict[str, _IdentityT | None]:
    """Build a pure title-to-stem-to-alias index with tiered precedence."""
    index: dict[str, _IdentityT | None] = {}
    tiers: tuple[Callable[[_ItemT], Iterable[str]], ...] = (title, stem, aliases)
    for extract in tiers:
        tier: dict[str, _IdentityT | None] = {}
        for item in items:
            item_identity = identity(item)
            for raw in extract(item):
                key = normalize(raw)
                if not key or key in index:
                    continue
                if key in tier and tier[key] != item_identity:
                    tier[key] = None
                else:
                    tier[key] = item_identity
        index.update(tier)
    return index


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
    candidates.extend(coerce_string_list(metadata.get("tags"), keep_empty_scalar=True))
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
