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
"""Validation and serialization helpers for write tools."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Final

from datacron.core.frontmatter import parse, serialize
from datacron.core.hashing import HASH_HEX_LENGTH
from datacron.core.logger import get_logger

_LOGGER = get_logger(__name__)

GetNoteFormat = str  # "full" | "map" -- kept loose for FastMCP schema
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CHUNK_ID_SEPARATOR: Final[str] = "::"
_MEMORY_ORIGINS: Final[frozenset[str]] = frozenset({"ai", "human", "merged"})
_MEMORY_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low", "needs_verification"}
)
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
_ULID_CREATE_ATTEMPTS: Final[int] = 5


def _validate_memory_frontmatter(
    *,
    rel_path: str,
    title: str,
    body: str,
    origin: str,
    confidence: str,
    tags: list[str],
) -> dict[str, Any]:
    cleaned_rel_path = rel_path.strip()
    cleaned_title = title.strip()
    cleaned_origin = _validate_memory_origin(origin)
    cleaned_confidence = _validate_memory_confidence(confidence)
    cleaned_tags = _clean_string_list(tags)

    if not cleaned_rel_path.endswith(".md"):
        raise ValueError("rel_path must end with .md")
    if not cleaned_title:
        raise ValueError("title must not be empty")
    if not body.strip():
        raise ValueError("body must not be empty")
    if not cleaned_tags:
        raise ValueError("tags must not be empty")

    return {
        "rel_path": cleaned_rel_path,
        "title": cleaned_title,
        "origin": cleaned_origin,
        "confidence": cleaned_confidence,
        "tags": cleaned_tags,
    }


def _validate_memory_origin(origin: str) -> str:
    cleaned_origin = origin.strip().lower()
    if cleaned_origin not in _MEMORY_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(_MEMORY_ORIGINS)}")
    return cleaned_origin


def _validate_memory_confidence(confidence: str) -> str:
    cleaned_confidence = confidence.strip().lower()
    if cleaned_confidence not in _MEMORY_CONFIDENCE_LEVELS:
        raise ValueError(f"confidence must be one of {sorted(_MEMORY_CONFIDENCE_LEVELS)}")
    return cleaned_confidence


def _validate_append_journal_request(
    *,
    rel_path: str,
    heading: str,
    entry: str,
) -> tuple[str, str, str]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    if not cleaned_rel_path.endswith(".md"):
        raise ValueError("rel_path must end with .md")
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if not entry.strip():
        raise ValueError("entry must not be empty")
    return cleaned_rel_path, cleaned_heading, entry


def _validate_set_frontmatter_request(
    *,
    rel_path: str,
    confidence: str | None,
    last_verified: str | None,
    supersedes: list[str] | None,
    origin: str | None,
) -> tuple[str, str | None, str | None, list[str] | None, str | None]:
    cleaned_rel_path = rel_path.strip()
    if confidence is None and last_verified is None and supersedes is None and origin is None:
        raise ValueError("nothing to update")
    if not cleaned_rel_path.endswith(".md"):
        raise ValueError("rel_path must end with .md")

    cleaned_confidence = _validate_memory_confidence(confidence) if confidence is not None else None
    cleaned_last_verified = (
        _validate_last_verified_date(last_verified) if last_verified is not None else None
    )
    cleaned_supersedes = _clean_string_list(supersedes) if supersedes is not None else None
    cleaned_origin = _validate_memory_origin(origin) if origin is not None else None

    return (
        cleaned_rel_path,
        cleaned_confidence,
        cleaned_last_verified,
        cleaned_supersedes,
        cleaned_origin,
    )


def _validate_last_verified_date(value: str) -> str:
    cleaned = value.strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("last_verified must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != cleaned:
        raise ValueError("last_verified must be a YYYY-MM-DD date")
    return cleaned


def _validate_patch_note_section_request(
    *,
    rel_path: str,
    heading: str,
    new_content: str,
    expected_hash: str | None,
    heading_level: int | None,
) -> tuple[str, str, str, str | None, int | None]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    cleaned_expected_hash = _validate_expected_hash(expected_hash)

    if not cleaned_rel_path.endswith(".md"):
        raise ValueError("rel_path must end with .md")
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if not new_content.strip():
        raise ValueError("new_content must not be empty")
    if heading_level is not None and heading_level not in range(1, 7):
        raise ValueError("heading_level must be between 1 and 6")

    normalized_content = new_content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return (
        cleaned_rel_path,
        cleaned_heading,
        normalized_content,
        cleaned_expected_hash,
        heading_level,
    )


def _validate_expected_hash(expected_hash: str | None) -> str | None:
    if expected_hash is None:
        return None
    cleaned = expected_hash.strip()
    if not _CONTENT_HASH_PATTERN.fullmatch(cleaned):
        raise ValueError(f"expected_hash must be a lowercase {HASH_HEX_LENGTH}-character SHA-256")
    return cleaned


def _parse_preserving_bom(raw: str) -> tuple[dict[str, Any], str, bool]:
    has_bom = raw.startswith("\ufeff")
    parseable = raw[1:] if has_bom else raw
    metadata, body = parse(parseable)
    return metadata, body, has_bom


def _serialize_preserving_bom(
    metadata: dict[str, Any],
    body: str,
    *,
    has_bom: bool,
) -> str:
    prefix = "\ufeff" if has_bom else ""
    return f"{prefix}{serialize(metadata, body)}"


def _clean_string_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value).strip()
        if not value or value in seen:
            continue
        cleaned.append(value)
        seen.add(value)
    return cleaned
