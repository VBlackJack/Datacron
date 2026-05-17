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
from typing import Any, Final

import frontmatter

__all__ = ["extract_tags", "parse"]

_INLINE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\S)#([A-Za-z0-9_][A-Za-z0-9_\-/]*)")


def parse(raw: str) -> tuple[dict[str, Any], str]:
    """Split ``raw`` into a YAML frontmatter mapping and body.

    Returns a tuple ``(metadata, body)``. If the file has no frontmatter
    delimiters the metadata dict is empty and the body is the original text
    (byte-for-byte; trailing newlines are preserved).
    """
    if not raw.lstrip().startswith("---"):
        return {}, raw
    post = frontmatter.loads(raw)
    metadata: dict[str, Any] = dict(post.metadata)
    return metadata, post.content


def _coerce_tag_iterable(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


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
    candidates.extend(match.group(1) for match in _INLINE_TAG_PATTERN.finditer(body))

    seen: set[str] = set()
    ordered: list[str] = []
    for tag in candidates:
        normalized = tag.lstrip("#").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
