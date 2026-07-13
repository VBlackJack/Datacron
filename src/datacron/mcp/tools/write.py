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
"""Approved vault write tool implementations."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ulid import ULID

from datacron.core.durability import DurabilityUnavailableError, ReadOnlyModeError
from datacron.core.frontmatter import FrontmatterError, serialize
from datacron.core.hashing import HASH_HEX_LENGTH
from datacron.core.logger import get_logger
from datacron.core.markdown_sections import (
    append_entry_to_heading,
    find_section_span,
    parse_heading_line,
    section_replacement_block,
)
from datacron.core.operation_log import (
    HistoryUnavailableError,
    OperationContext,
    OperationLogError,
)
from datacron.core.paths import PathConfinementError
from datacron.core.vault_writer import UlidCollisionError
from datacron.mcp.tools.payloads import _audit, _error_response
from datacron.mcp.tools.read import _resolve_note
from datacron.mcp.tools.search import (
    _invalidate_alias_cache_if_index_changed,
    _reconcile_serialized,
)
from datacron.mcp.tools.write_validation import (
    _clean_string_list,
    _parse_preserving_bom,
    _serialize_preserving_bom,
    _validate_append_journal_request,
    _validate_expected_hash,
    _validate_memory_frontmatter,
    _validate_patch_note_section_request,
    _validate_set_frontmatter_request,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

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


async def _create_note_ai_impl(
    app: DatacronApp,
    *,
    rel_path: str,
    title: str,
    body: str,
    origin: str,
    confidence: str,
    tags: list[str],
    supersedes: list[str] | None = None,
    last_verified: str | None = None,
    expected_hash: str | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        app.write_policy.ensure_writable()
        cleaned = _validate_memory_frontmatter(
            rel_path=rel_path,
            title=title,
            body=body,
            origin=origin,
            confidence=confidence,
            tags=tags,
        )
        cleaned_expected_hash = _validate_expected_hash(expected_hash)
        now = datetime.now(tz=UTC)
        for attempt in range(_ULID_CREATE_ATTEMPTS):
            note_id = str(ULID())
            frontmatter = {
                "id": note_id,
                "title": cleaned["title"],
                "created": now.isoformat(),
                "updated": now.isoformat(),
                "origin": cleaned["origin"],
                "confidence": cleaned["confidence"],
                "last_verified": (
                    last_verified.strip() if last_verified else now.date().isoformat()
                ),
                "supersedes": _clean_string_list(supersedes or []),
                "tags": cleaned["tags"],
            }
            content = serialize(frontmatter, body)
            try:
                content_hash = await app.vault_writer.write_note_atomic(
                    cleaned["rel_path"],
                    content,
                    overwrite=False,
                    expected_hash=cleaned_expected_hash,
                    note_id=note_id,
                    operation=OperationContext(
                        op="create",
                        tool="create_note_ai",
                        actor=actor,
                        parameters={
                            "title_chars": len(cleaned["title"]),
                            "body_chars": len(body),
                            "origin": cleaned["origin"],
                            "confidence": cleaned["confidence"],
                            "tag_count": len(cleaned["tags"]),
                            "supersedes_count": len(supersedes or []),
                        },
                    ),
                )
                break
            except UlidCollisionError:
                if attempt == _ULID_CREATE_ATTEMPTS - 1:
                    raise
        index_stats = await _reconcile_serialized(app)
        await _invalidate_alias_cache_if_index_changed(app, index_stats)
    except (DurabilityUnavailableError, ReadOnlyModeError) as exc:
        return _error_response("create_note_ai", exc, started, rel_path=rel_path)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled -- set DATACRON_WRITE_PATHS")
            if not app.settings.write_paths
            else exc
        )
        return _error_response(
            "create_note_ai",
            mapped_exc,
            started,
            rel_path=rel_path,
            title=title,
        )
    except FileExistsError:
        return _error_response(
            "create_note_ai",
            FileExistsError(
                f"note already exists at {rel_path}; use patch_note_section (v0.2 phase 2)"
            ),
            started,
            rel_path=rel_path,
            title=title,
        )
    except ValueError as exc:
        return _error_response(
            "create_note_ai",
            exc,
            started,
            rel_path=rel_path,
            title=title,
        )
    except Exception:
        _LOGGER.exception("create_note_ai failed (rel_path=%r title=%r)", rel_path, title)
        return _error_response(
            "create_note_ai",
            RuntimeError("internal error"),
            started,
            rel_path=rel_path,
            title=title,
        )

    payload: dict[str, Any] = {
        "created": {
            "id": note_id,
            "rel_path": cleaned["rel_path"],
            "title": cleaned["title"],
        },
        "content_hash": content_hash,
        "indexed": True,
    }
    _audit(
        "create_note_ai",
        started,
        note_id=note_id,
        rel_path=cleaned["rel_path"],
        title=cleaned["title"],
        reindexed_notes=index_stats["reindexed_notes"],
        deleted_notes=index_stats["deleted_notes"],
    )
    return payload


async def _append_journal_impl(
    app: DatacronApp,
    *,
    rel_path: str,
    heading: str,
    entry: str,
    expected_hash: str | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        app.write_policy.ensure_writable()
        cleaned_rel_path, cleaned_heading, cleaned_entry = _validate_append_journal_request(
            rel_path=rel_path,
            heading=heading,
            entry=entry,
        )
        cleaned_expected_hash = _validate_expected_hash(expected_hash)

        def mutation(raw: str) -> str:
            metadata, current_body, has_bom = _parse_preserving_bom(raw)
            new_body = append_entry_to_heading(
                current_body,
                cleaned_heading,
                cleaned_entry,
            )
            metadata["updated"] = datetime.now(tz=UTC).isoformat()
            return _serialize_preserving_bom(metadata, new_body, has_bom=has_bom)

        content_hash = await app.vault_writer.mutate_note_atomic(
            cleaned_rel_path,
            mutation,
            expected_hash=cleaned_expected_hash,
            operation=OperationContext(
                op="append",
                tool="append_journal",
                actor=actor,
                parameters={
                    "heading": cleaned_heading,
                    "entry_chars": len(cleaned_entry),
                },
            ),
        )
        index_stats = await _reconcile_serialized(app)
        await _invalidate_alias_cache_if_index_changed(app, index_stats)
    except (DurabilityUnavailableError, ReadOnlyModeError) as exc:
        return _error_response("append_journal", exc, started, rel_path=rel_path)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled -- set DATACRON_WRITE_PATHS")
            if not app.settings.write_paths
            else exc
        )
        return _error_response(
            "append_journal",
            mapped_exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except FileNotFoundError as exc:
        return _error_response(
            "append_journal",
            exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except (FrontmatterError, ValueError) as exc:
        return _error_response(
            "append_journal",
            exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except Exception:
        _LOGGER.exception("append_journal failed (rel_path=%r heading=%r)", rel_path, heading)
        return _error_response(
            "append_journal",
            RuntimeError("internal error"),
            started,
            rel_path=rel_path,
            heading=heading,
        )

    payload: dict[str, Any] = {
        "appended": {"rel_path": cleaned_rel_path, "heading": cleaned_heading},
        "content_hash": content_hash,
        "indexed": True,
    }
    _audit(
        "append_journal",
        started,
        rel_path=cleaned_rel_path,
        heading=cleaned_heading,
        reindexed_notes=index_stats["reindexed_notes"],
        deleted_notes=index_stats["deleted_notes"],
    )
    return payload


async def _set_frontmatter_impl(
    app: DatacronApp,
    *,
    rel_path: str,
    confidence: str | None = None,
    last_verified: str | None = None,
    supersedes: list[str] | None = None,
    origin: str | None = None,
    expected_hash: str | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        app.write_policy.ensure_writable()
        (
            cleaned_rel_path,
            cleaned_confidence,
            cleaned_last_verified,
            cleaned_supersedes,
            cleaned_origin,
        ) = _validate_set_frontmatter_request(
            rel_path=rel_path,
            confidence=confidence,
            last_verified=last_verified,
            supersedes=supersedes,
            origin=origin,
        )
        cleaned_expected_hash = _validate_expected_hash(expected_hash)
        changed_fields: list[str] = []
        requested_fields = sorted(
            field
            for field, value in {
                "confidence": cleaned_confidence,
                "last_verified": cleaned_last_verified,
                "supersedes": cleaned_supersedes,
                "origin": cleaned_origin,
            }.items()
            if value is not None
        )

        def mutation(raw: str) -> str:
            metadata, body, has_bom = _parse_preserving_bom(raw)
            if not metadata:
                raise ValueError("note has no frontmatter")
            if cleaned_confidence is not None:
                _set_changed_frontmatter_field(
                    metadata,
                    changed_fields,
                    "confidence",
                    cleaned_confidence,
                )
            if cleaned_last_verified is not None:
                _set_changed_frontmatter_field(
                    metadata,
                    changed_fields,
                    "last_verified",
                    cleaned_last_verified,
                )
            if cleaned_supersedes is not None:
                _set_changed_frontmatter_field(
                    metadata,
                    changed_fields,
                    "supersedes",
                    cleaned_supersedes,
                )
            if cleaned_origin is not None:
                _set_changed_frontmatter_field(
                    metadata,
                    changed_fields,
                    "origin",
                    cleaned_origin,
                )
            metadata["updated"] = datetime.now(tz=UTC).isoformat()
            return _serialize_preserving_bom(metadata, body, has_bom=has_bom)

        content_hash = await app.vault_writer.mutate_note_atomic(
            cleaned_rel_path,
            mutation,
            expected_hash=cleaned_expected_hash,
            operation=OperationContext(
                op="set_frontmatter",
                tool="set_frontmatter",
                actor=actor,
                parameters={"fields": ",".join(requested_fields)},
            ),
        )
        index_stats = await _reconcile_serialized(app)
        await _invalidate_alias_cache_if_index_changed(app, index_stats)
    except (DurabilityUnavailableError, ReadOnlyModeError) as exc:
        return _error_response("set_frontmatter", exc, started, rel_path=rel_path)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled -- set DATACRON_WRITE_PATHS")
            if not app.settings.write_paths
            else exc
        )
        return _error_response(
            "set_frontmatter",
            mapped_exc,
            started,
            rel_path=rel_path,
        )
    except FileNotFoundError as exc:
        return _error_response(
            "set_frontmatter",
            exc,
            started,
            rel_path=rel_path,
        )
    except (FrontmatterError, ValueError) as exc:
        return _error_response(
            "set_frontmatter",
            exc,
            started,
            rel_path=rel_path,
        )
    except Exception:
        _LOGGER.exception("set_frontmatter failed (rel_path=%r)", rel_path)
        return _error_response(
            "set_frontmatter",
            RuntimeError("internal error"),
            started,
            rel_path=rel_path,
        )

    payload: dict[str, Any] = {
        "updated": {"rel_path": cleaned_rel_path, "fields": changed_fields},
        "content_hash": content_hash,
        "indexed": True,
    }
    _audit(
        "set_frontmatter",
        started,
        rel_path=cleaned_rel_path,
        fields=changed_fields,
        reindexed_notes=index_stats["reindexed_notes"],
        deleted_notes=index_stats["deleted_notes"],
    )
    return payload


def _set_changed_frontmatter_field(
    metadata: dict[str, Any],
    changed_fields: list[str],
    field: str,
    value: Any,
) -> None:
    if metadata.get(field) != value:
        changed_fields.append(field)
    metadata[field] = value


async def _patch_note_section_impl(
    app: DatacronApp,
    *,
    rel_path: str,
    heading: str,
    new_content: str,
    expected_hash: str | None = None,
    heading_level: int | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        app.write_policy.ensure_writable()
        (
            cleaned_rel_path,
            cleaned_heading,
            cleaned_new_content,
            cleaned_expected_hash,
            cleaned_heading_level,
        ) = _validate_patch_note_section_request(
            rel_path=rel_path,
            heading=heading,
            new_content=new_content,
            expected_hash=expected_hash,
            heading_level=heading_level,
        )
        matched_level = 0
        matched_text = ""

        def mutation(raw: str) -> str:
            nonlocal matched_level, matched_text
            metadata, body, has_bom = _parse_preserving_bom(raw)
            lines = body.splitlines(keepends=True)
            content_start, content_end = find_section_span(
                lines,
                cleaned_heading,
                cleaned_heading_level,
            )
            matched_heading = parse_heading_line(lines[content_start - 1])
            if matched_heading is None:
                raise RuntimeError("section span does not follow a heading")
            matched_level, matched_text = matched_heading
            prefix = "".join(lines[:content_start])
            suffix = "".join(lines[content_end:])
            new_body = (
                f"{prefix}"
                f"{section_replacement_block(cleaned_new_content, prefix=prefix, suffix=suffix)}"
                f"{suffix}"
            )
            metadata["updated"] = datetime.now(tz=UTC).isoformat()
            return _serialize_preserving_bom(metadata, new_body, has_bom=has_bom)

        content_hash = await app.vault_writer.mutate_note_atomic(
            cleaned_rel_path,
            mutation,
            expected_hash=cleaned_expected_hash,
            operation=OperationContext(
                op="patch_section",
                tool="patch_note_section",
                actor=actor,
                parameters={
                    "heading": cleaned_heading,
                    "heading_level": cleaned_heading_level,
                    "new_content_chars": len(cleaned_new_content),
                },
            ),
        )
        index_stats = await _reconcile_serialized(app)
        await _invalidate_alias_cache_if_index_changed(app, index_stats)
    except (DurabilityUnavailableError, ReadOnlyModeError) as exc:
        return _error_response("patch_note_section", exc, started, rel_path=rel_path)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled -- set DATACRON_WRITE_PATHS")
            if not app.settings.write_paths
            else exc
        )
        return _error_response(
            "patch_note_section",
            mapped_exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except FileNotFoundError as exc:
        return _error_response(
            "patch_note_section",
            exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except (FrontmatterError, ValueError) as exc:
        return _error_response(
            "patch_note_section",
            exc,
            started,
            rel_path=rel_path,
            heading=heading,
        )
    except Exception:
        _LOGGER.exception("patch_note_section failed (rel_path=%r heading=%r)", rel_path, heading)
        return _error_response(
            "patch_note_section",
            RuntimeError("internal error"),
            started,
            rel_path=rel_path,
            heading=heading,
        )

    payload: dict[str, Any] = {
        "patched": {
            "rel_path": cleaned_rel_path,
            "heading": matched_text,
            "level": matched_level,
        },
        "content_hash": content_hash,
        "indexed": True,
    }
    _audit(
        "patch_note_section",
        started,
        rel_path=cleaned_rel_path,
        heading=cleaned_heading,
        heading_level=matched_level,
        reindexed_notes=index_stats["reindexed_notes"],
        deleted_notes=index_stats["deleted_notes"],
    )
    return payload


async def _revert_note_impl(
    app: DatacronApp,
    *,
    note: str,
    to_hash: str,
    expected_hash: str | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        app.write_policy.ensure_writable()
        cleaned_to_hash = _validate_expected_hash(to_hash)
        if cleaned_to_hash is None:
            raise ValueError("to_hash is required")
        cleaned_expected_hash = _validate_expected_hash(expected_hash)
        resolved = await _resolve_note(app, note)
        if resolved is None:
            raise FileNotFoundError(f"No note found for {note!r}")
        content_hash = await app.vault_writer.revert_note_atomic(
            resolved.rel_path,
            cleaned_to_hash,
            expected_hash=cleaned_expected_hash,
            operation=OperationContext(
                op="revert",
                tool="revert_note",
                actor=actor,
                parameters={"to_hash": cleaned_to_hash},
            ),
        )
        index_stats = await _reconcile_serialized(app)
        await _invalidate_alias_cache_if_index_changed(app, index_stats)
    except (
        FileNotFoundError,
        HistoryUnavailableError,
        OperationLogError,
        PathConfinementError,
        DurabilityUnavailableError,
        ReadOnlyModeError,
        ValueError,
    ) as exc:
        return _error_response(
            "revert_note",
            exc,
            started,
            note=note,
            to_hash=to_hash,
        )
    except Exception:
        _LOGGER.exception("revert_note failed (note=%r to_hash=%r)", note, to_hash)
        return _error_response(
            "revert_note",
            RuntimeError("internal error"),
            started,
            note=note,
            to_hash=to_hash,
        )

    payload: dict[str, Any] = {
        "reverted": {
            "id": resolved.id,
            "rel_path": resolved.rel_path,
            "to_hash": cleaned_to_hash,
        },
        "content_hash": content_hash,
        "indexed": True,
    }
    _audit(
        "revert_note",
        started,
        note_id=resolved.id,
        rel_path=resolved.rel_path,
        to_hash=cleaned_to_hash,
        reindexed_notes=index_stats["reindexed_notes"],
        deleted_notes=index_stats["deleted_notes"],
    )
    return payload
