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
from datetime import UTC, date, datetime, timedelta
from pathlib import PureWindowsPath
from typing import Any, Final

import yaml
from ulid import ULID

from datacron.core.frontmatter import (
    FRONTMATTER_BOUNDARY_PATTERN,
    parse,
    parse_preserving_bom_and_body_eols,
    serialize_preserving_bom,
)
from datacron.core.hashing import HASH_HEX_LENGTH
from datacron.core.paths import PathConfinementError

# The exact-bytes helpers live in core.frontmatter; the write tools of this package
# keep importing them under the names they have always used.
_parse_preserving_bom_and_body_eols = parse_preserving_bom_and_body_eols
_serialize_preserving_bom = serialize_preserving_bom

_MEMORY_ORIGINS: Final[frozenset[str]] = frozenset({"ai", "human", "merged"})
_MEMORY_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low", "needs_verification"}
)
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
_BACKLOG_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"BL-[0-9]{4,}")
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ATX_CLOSING_SEQUENCE: Final[re.Pattern[str]] = re.compile(r"[ \t]#+$")
_ATX_LEVEL_MARKER: Final[re.Pattern[str]] = re.compile(r"#{1,6}(?:[ \t]|$)")
_MARKDOWN_SUFFIX: Final[str] = ".md"
_WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WRITES_DISABLED_MESSAGE: Final[str] = "writes disabled -- set DATACRON_WRITE_PATHS"
# Markdown ATX headings run from one to six hash marks; every heading selector shares it.
MAX_HEADING_LEVEL: Final[int] = 6
HEADING_LEVELS: Final[range] = range(1, MAX_HEADING_LEVEL + 1)
_REJECTED_ENTRY_SEPARATOR: Final[str] = " -- "
_MAX_REJECTED_ENTRIES: Final[int] = 16
_MAX_REJECTED_ENTRY_CHARS: Final[int] = 300
_RENAME_H1_REFUSAL_MESSAGE: Final[str] = (
    "rename_note_section only supports ATX heading levels 2 through 6; "
    "level 1 is refused because frontmatter title synchronization is outside this tool"
)


def _map_write_path_error(
    exc: PathConfinementError,
    *,
    writes_configured: bool,
) -> PathConfinementError:
    """Map an empty write allowlist to the stable public error message."""
    if writes_configured:
        return exc
    return PathConfinementError(_WRITES_DISABLED_MESSAGE)


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

    _assert_markdown_rel_path(cleaned_rel_path)
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
    _assert_markdown_rel_path(cleaned_rel_path)
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if "\n" in cleaned_heading or "\r" in cleaned_heading:
        # This tool is the one that synthesizes a heading line, and an ATX heading
        # cannot carry a newline. A multi-line value never matched the section it
        # had just created, so every call took the create branch again and the note
        # grew one more duplicate heading, without bound, until patching any of
        # them became ambiguous. rename_note_section already refuses this.
        raise ValueError("heading must be a single line")
    if _ATX_LEVEL_MARKER.match(cleaned_heading):
        # Only a real ATX marker ("## Log") is refused. "#1 Priorities" and a
        # leading Obsidian tag are ordinary heading text, and refusing every
        # leading "#" made those existing sections unreachable by this tool.
        raise ValueError("heading must not start with '#'; the tool supplies the level")
    if not entry.strip():
        raise ValueError("entry must not be empty")
    return cleaned_rel_path, cleaned_heading, entry


def _validate_set_frontmatter_request(
    *,
    rel_path: str,
    confidence: str | None,
    last_verified: str | None,
    supersedes: list[str] | None,
    rejected: list[str] | None,
    origin: str | None,
    valid_from: str | None,
    invalid_at: str | None,
    invalidated_by: str | None,
    last_id: str | None = None,
    archived: bool | None = None,
) -> tuple[
    str,
    str | None,
    str | None,
    list[str] | None,
    list[str] | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    cleaned_rel_path = rel_path.strip()
    if all(
        value is None
        for value in (
            confidence,
            last_verified,
            supersedes,
            rejected,
            origin,
            valid_from,
            invalid_at,
            invalidated_by,
            last_id,
            archived,
        )
    ):
        raise ValueError("nothing to update")
    _assert_markdown_rel_path(cleaned_rel_path)

    cleaned_confidence = _validate_memory_confidence(confidence) if confidence is not None else None
    cleaned_last_verified = (
        _validate_last_verified_date(last_verified) if last_verified is not None else None
    )
    cleaned_supersedes = _clean_string_list(supersedes) if supersedes is not None else None
    cleaned_rejected = _validate_rejected_entries(rejected) if rejected is not None else None
    cleaned_origin = _validate_memory_origin(origin) if origin is not None else None
    cleaned_valid_from = _validate_valid_from_date(valid_from) if valid_from is not None else None
    cleaned_invalid_at = (
        _validate_invalid_at_datetime(invalid_at) if invalid_at is not None else None
    )
    cleaned_invalidated_by = (
        _validate_canonical_ulid(invalidated_by, field="invalidated_by")
        if invalidated_by is not None
        else None
    )

    return (
        cleaned_rel_path,
        cleaned_confidence,
        cleaned_last_verified,
        cleaned_supersedes,
        cleaned_rejected,
        cleaned_origin,
        cleaned_valid_from,
        cleaned_invalid_at,
        cleaned_invalidated_by,
    )


def _validate_rejected_entries(values: list[str]) -> list[str]:
    """Validate and normalize structured rejected-option entries."""
    if len(values) > _MAX_REJECTED_ENTRIES:
        raise ValueError(f"rejected must contain at most {_MAX_REJECTED_ENTRIES} entries")

    cleaned: list[str] = []
    for index, raw_value in enumerate(values, start=1):
        if len(raw_value) > _MAX_REJECTED_ENTRY_CHARS:
            raise ValueError(
                f"rejected entry {index} must be at most {_MAX_REJECTED_ENTRY_CHARS} characters"
            )
        option, separator, reason = raw_value.partition(_REJECTED_ENTRY_SEPARATOR)
        if not separator:
            raise ValueError(f"rejected entry {index} must use the 'option -- reason' format")
        cleaned_option = option.strip()
        cleaned_reason = reason.strip()
        if not cleaned_option:
            raise ValueError(f"rejected entry {index} option must not be empty")
        if not cleaned_reason:
            raise ValueError(f"rejected entry {index} reason must not be empty")
        cleaned.append(f"{cleaned_option}{_REJECTED_ENTRY_SEPARATOR}{cleaned_reason}")
    return cleaned


def _validate_last_verified_date(value: str) -> str:
    cleaned = value.strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("last_verified must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != cleaned:
        raise ValueError("last_verified must be a YYYY-MM-DD date")
    return cleaned


def _validate_valid_from_date(value: str) -> str:
    cleaned = value.strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("valid_from must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != cleaned:
        raise ValueError("valid_from must be a YYYY-MM-DD date")
    return cleaned


def _validate_invalid_at_datetime(value: str) -> str:
    cleaned = value.strip()
    parseable = f"{cleaned[:-1]}+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        parsed = datetime.fromisoformat(parseable)
    except ValueError as exc:
        raise ValueError("invalid_at must be an ISO 8601 UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("invalid_at must be an ISO 8601 UTC datetime")
    return parsed.astimezone(UTC).isoformat()


def _validate_canonical_ulid(value: str, *, field: str) -> str:
    cleaned = value.strip()
    try:
        parsed = ULID.from_str(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical 26-character ULID") from exc
    if str(parsed) != cleaned:
        raise ValueError(f"{field} must be a canonical 26-character ULID")
    return cleaned


def _validate_patch_note_section_request(
    *,
    rel_path: str,
    heading: str,
    new_content: str,
    expected_hash: str | None,
    heading_level: int | None,
    heading_occurrence: int | None,
) -> tuple[str, str, str, str | None, int | None, int | None]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    cleaned_expected_hash = _validate_expected_hash(expected_hash)

    _assert_markdown_rel_path(cleaned_rel_path)
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if not new_content.strip():
        raise ValueError("new_content must not be empty")
    if heading_level is not None and heading_level not in HEADING_LEVELS:
        raise ValueError("heading_level must be between 1 and 6")
    cleaned_heading_occurrence = _validate_heading_occurrence(
        heading_occurrence,
        heading_level=heading_level,
        expected_hash=cleaned_expected_hash,
    )

    normalized_content = new_content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return (
        cleaned_rel_path,
        cleaned_heading,
        normalized_content,
        cleaned_expected_hash,
        heading_level,
        cleaned_heading_occurrence,
    )


def _validate_patch_note_preamble_request(
    *,
    rel_path: str,
    new_content: str,
    expected_hash: str | None,
) -> tuple[str, str, str]:
    cleaned_rel_path = rel_path.strip()
    cleaned_expected_hash = _validate_expected_hash(expected_hash)

    _assert_markdown_rel_path(cleaned_rel_path)
    if cleaned_expected_hash is None:
        raise ValueError("expected_hash is required")

    normalized_content = new_content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized_content.strip():
        normalized_content = ""
    return cleaned_rel_path, normalized_content, cleaned_expected_hash


def _validate_delete_note_section_request(
    *,
    rel_path: str,
    heading: str,
    expected_hash: str | None,
    heading_level: int | None,
    heading_occurrence: int | None,
) -> tuple[str, str, str | None, int | None, int | None]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    cleaned_expected_hash = _validate_expected_hash(expected_hash)

    _assert_markdown_rel_path(cleaned_rel_path)
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if heading_level is not None and heading_level not in HEADING_LEVELS:
        raise ValueError("heading_level must be between 1 and 6")
    if heading_level == 1:
        raise ValueError(
            "delete_note_section only supports heading levels 2 through 6; level 1 is refused"
        )
    cleaned_heading_occurrence = _validate_heading_occurrence(
        heading_occurrence,
        heading_level=heading_level,
        expected_hash=cleaned_expected_hash,
    )
    return (
        cleaned_rel_path,
        cleaned_heading,
        cleaned_expected_hash,
        heading_level,
        cleaned_heading_occurrence,
    )


def _validate_rename_note_section_request(
    *,
    rel_path: str,
    heading: str,
    new_heading: str,
    expected_hash: str | None,
    heading_level: int | None,
    heading_occurrence: int | None,
) -> tuple[str, str, str, str | None, int | None, int | None]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    cleaned_new_heading = new_heading.strip()
    cleaned_expected_hash = _validate_expected_hash(expected_hash)

    _assert_markdown_rel_path(cleaned_rel_path)
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if not cleaned_new_heading:
        raise ValueError("new_heading must not be empty")
    if "\r" in new_heading or "\n" in new_heading:
        raise ValueError("new_heading must be a single line")
    if cleaned_new_heading.startswith("#"):
        raise ValueError("new_heading must contain text only, without Markdown heading markers")
    if _ATX_CLOSING_SEQUENCE.search(cleaned_new_heading):
        # A space then hashes at the end is an ATX closing sequence, which the
        # parser removes: "Section #" was stored verbatim and read back as
        # "Section". The tool reported the requested title, so the client
        # believed the note held it, the next rename by that title failed with
        # heading_not_found, and any stored selector built from it was dead.
        raise ValueError(
            "new_heading must not end with a closing sequence of '#'; Markdown drops it "
            "and the stored heading would differ from the requested title"
        )
    if heading_level is not None and heading_level not in HEADING_LEVELS:
        raise ValueError("heading_level must be between 1 and 6")
    if heading_level == 1:
        raise ValueError(_RENAME_H1_REFUSAL_MESSAGE)
    cleaned_heading_occurrence = _validate_heading_occurrence(
        heading_occurrence,
        heading_level=heading_level,
        expected_hash=cleaned_expected_hash,
    )
    return (
        cleaned_rel_path,
        cleaned_heading,
        cleaned_new_heading,
        cleaned_expected_hash,
        heading_level,
        cleaned_heading_occurrence,
    )


def _validate_heading_occurrence(
    heading_occurrence: int | None,
    *,
    heading_level: int | None,
    expected_hash: str | None,
) -> int | None:
    if heading_occurrence is None:
        return None
    if isinstance(heading_occurrence, bool) or not isinstance(heading_occurrence, int):
        raise ValueError("heading_occurrence must be an integer")
    if heading_occurrence < 1:
        raise ValueError("heading_occurrence must be at least 1")
    if heading_level is None:
        raise ValueError("heading_occurrence requires heading_level")
    if expected_hash is None:
        raise ValueError("heading_occurrence requires expected_hash")
    return heading_occurrence


def _assert_markdown_rel_path(cleaned_rel_path: str) -> None:
    """Refuse a path that is not a Markdown note.

    The test and its message were written out at each of the seven call sites,
    so the suffix this server accepts was stated in eight places across two
    modules and could drift in any one of them.
    """
    if not cleaned_rel_path.endswith(_MARKDOWN_SUFFIX):
        raise ValueError(f"rel_path must end with {_MARKDOWN_SUFFIX}")
    if PureWindowsPath(cleaned_rel_path).drive:
        # An absolute or drive-qualified path is the confinement check's to refuse,
        # with its own error type.
        return
    for part in cleaned_rel_path.replace("\\", "/").split("/"):
        if part in {"", ".", ".."}:
            continue
        # The reader never admits a hidden folder, so a note written under
        # .datacron or .obsidian was committed and then refused by every read,
        # patch and revert, with a recovery hint telling the client to re-read it.
        if part.startswith("."):
            raise ValueError("rel_path must not enter a hidden folder")
        # A colon names an NTFS alternate data stream, and a reserved device name
        # is not a file on Windows: both failed after a stray temp file was made.
        if ":" in part:
            raise ValueError("rel_path must not contain ':'")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"rel_path must not use the reserved device name {part!r}")


def _validate_expected_hash(expected_hash: str | None) -> str | None:
    if expected_hash is None:
        return None
    cleaned = expected_hash.strip()
    if not _CONTENT_HASH_PATTERN.fullmatch(cleaned):
        raise ValueError(f"expected_hash must be a lowercase {HASH_HEX_LENGTH}-character SHA-256")
    return cleaned


def is_canonical_ulid(value: str) -> bool:
    """Report whether ``value`` is a canonical 26-character Crockford ULID.

    Identity repair needs this in its own right: adopting a frontmatter ID that
    only looks like a ULID would propagate the malformed value to the index and
    the sidecar, which is how an unrepairable divergence is created rather than
    fixed.
    """
    return _ULID_PATTERN.fullmatch(value) is not None


def replace_frontmatter_id(raw: str, note_id: str) -> str:
    """Return ``raw`` with its frontmatter ``id`` and ``updated`` replaced.

    The body survives byte for byte, BOM and line endings included: the
    exact-body parser is deliberate, since the plain one strips the trailing
    newline and would turn an identity repair into a silent rewrite of the note.

    Only the ``id`` and ``updated`` values are edited in the frontmatter text;
    the rest of the block, comments included, is kept as written unless that
    edit cannot be verified, in which case the block is re-serialized.
    """
    metadata, body, has_bom = _parse_preserving_bom_and_body_eols(raw)
    if not metadata:
        raise ValueError("note has no frontmatter")
    metadata["id"] = note_id
    metadata["updated"] = datetime.now(tz=UTC).isoformat()
    return _serialize_preserving_frontmatter(raw, metadata, body, has_bom=has_bom)


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


def _validate_backlog_last_id(value: object) -> str:
    """Require a canonical backlog identifier without coercing scalar types."""
    if not isinstance(value, str) or _BACKLOG_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("last_id must be BL- followed by at least four ASCII decimal digits")
    return value


def _backlog_counter_key(value: str) -> tuple[int, str]:
    """Compare arbitrary-width decimal counters without integer conversion limits."""
    digits = value.removeprefix("BL-").lstrip("0") or "0"
    return len(digits), digits


def _patch_frontmatter_fields(raw: str, metadata: dict[str, Any], fields: list[str]) -> str:
    """Replace selected YAML values while preserving unrelated header text and body."""
    start, end, eol = _frontmatter_header_span(raw)
    header = raw[start:end]
    nodes = _frontmatter_mapping_nodes(header)
    header, additions = _apply_field_edits(header, nodes, metadata, fields, eol)
    result = raw[:start] + header + additions + raw[end:]
    parsed, _ = parse(result)
    if parsed != metadata:
        raise ValueError("last_id metadata preservation validation failed")
    return result


def _serialize_preserving_frontmatter(
    raw: str,
    metadata: dict[str, Any],
    body: str,
    *,
    has_bom: bool,
) -> str:
    """Write ``body`` under the note's own frontmatter text, edited key by key.

    Re-dumping the whole block through PyYAML is lossy on hand-written notes:
    every comment went, key order and flow style changed, and YAML 1.1 turned
    values back into other values (``14:30`` into ``870``, ``1.10`` into ``1.1``,
    ``01234`` into ``668``, ``NO`` into ``false``) that Obsidian had displayed as
    written. Only the keys whose value changed are replaced or appended, through
    the same verified span edit as ``last_id``; every other byte of the block is
    kept. When that edit cannot be trusted (a removed key, a changed block list,
    anchors, a flow mapping) the whole block is re-serialized as before.
    """
    original, original_body, _had_bom = _parse_preserving_bom_and_body_eols(raw)
    if original and raw.endswith(original_body) and all(key in metadata for key in original):
        head = raw[: len(raw) - len(original_body)]
        changed = [key for key in metadata if key not in original or original[key] != metadata[key]]
        try:
            return _patch_frontmatter_fields(head, metadata, changed) + body
        except (ValueError, yaml.YAMLError):
            pass
    return _serialize_preserving_bom(metadata, body, has_bom=has_bom)


def _frontmatter_header_span(raw: str) -> tuple[int, int, str]:
    """Locate the YAML header between its boundaries; return its span and line ending."""
    offset = 1 if raw.startswith("\ufeff") else 0
    lines = raw[offset:].splitlines(keepends=True)
    opening = next((i for i, line in enumerate(lines) if line.strip()), None)
    if opening is None or FRONTMATTER_BOUNDARY_PATTERN.fullmatch(lines[opening]) is None:
        raise ValueError("last_id requires an explicit YAML frontmatter mapping")
    closing = next(
        (
            i
            for i in range(opening + 1, len(lines))
            if FRONTMATTER_BOUNDARY_PATTERN.fullmatch(lines[i]) is not None
        ),
        None,
    )
    if closing is None:
        raise ValueError("last_id requires closed YAML frontmatter")
    start = offset + sum(map(len, lines[: opening + 1]))
    end = offset + sum(map(len, lines[:closing]))
    eol = "\r\n" if lines[opening].endswith("\r\n") else "\n"
    return start, end, eol


def _frontmatter_mapping_nodes(header: str) -> dict[str, yaml.Node]:
    """Map each top-level key to its YAML node; refuse anything a span edit cannot trust."""
    node = yaml.compose(header, Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise ValueError("last_id requires a block YAML mapping")
    nodes: dict[str, yaml.Node] = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.value in nodes or key.value == "<<":
            raise ValueError("last_id refuses duplicate, complex, or merged YAML keys")
        nodes[key.value] = value
    # Anchors can alias another field's source span, so reject them fail-closed.
    if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(header)):
        raise ValueError("last_id refuses YAML anchors and aliases")
    return nodes


def _apply_field_edits(
    header: str,
    nodes: dict[str, yaml.Node],
    metadata: dict[str, Any],
    fields: list[str],
    eol: str,
) -> tuple[str, str]:
    """Replace the selected values in place and render the fields to append."""
    edits: list[tuple[int, int, str]] = []
    additions = ""
    for field in dict.fromkeys([*fields, "updated"]):
        if field not in metadata:
            raise ValueError("last_id cannot be combined with field removal")
        rendered = yaml.safe_dump(metadata[field], default_flow_style=True, allow_unicode=True)
        rendered = rendered.removesuffix("...\n").rstrip("\n")
        existing = nodes.get(field)
        if existing is None:
            additions += f"{field}: {rendered}{eol}"
            continue
        if isinstance(existing, yaml.CollectionNode) and not existing.flow_style:
            # A block collection's span ends after its terminating newline, while
            # the flow value rendered above carries none, so splicing one over the
            # other ran the next key onto the same line and the header stopped
            # parsing. Every such call failed, and what the caller saw was a YAML
            # parser message about a file it had never written. The refusal says
            # what the tool cannot do instead.
            raise ValueError(
                f"last_id cannot be combined with an edit to {field!r}, which this note "
                "stores as a block list; set that field in a separate call"
            )
        edits.append((existing.start_mark.index, existing.end_mark.index, rendered))
    for begin, finish, replacement in sorted(edits, reverse=True):
        header = header[:begin] + replacement + header[finish:]
    return header, additions
