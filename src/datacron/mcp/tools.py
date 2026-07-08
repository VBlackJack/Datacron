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
"""MCP tools for vault reads, search, and approved memory writes.

Each tool follows the seven Sem-2 rules from
``docs/agent-briefs/02-brief-claude-code.md``:

1. Typed parameters (FastMCP's Pydantic integration).
2. Path inputs are confined via :func:`datacron.core.paths.assert_within_paths`.
3. Delegate work to ``core`` (and, for ``get_note(format='map')``, the
   ``ASTChunker`` Protocol).
4. Vault content goes through
   :func:`datacron.mcp.sandbox.wrap_vault_content` before leaving the
   server.
5. Result size is bounded by ``DATACRON_MAX_RESULT_COUNT`` (count) and
   ``DATACRON_MAX_RESULT_TOKENS`` (token budget on ``get_note(full)``).
6. Every call emits a single audit log line at INFO level.
7. The return value is JSON-serializable.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from mcp.server.fastmcp import FastMCP
from ulid import ULID

from datacron.core.config import TEMPORAL_OVERFETCH_FACTOR
from datacron.core.frontmatter import FrontmatterError, parse, serialize
from datacron.core.hashing import HASH_HEX_LENGTH, hash_text
from datacron.core.logger import get_logger
from datacron.core.models import ChunkType, Note, SearchResult
from datacron.core.paths import PathConfinementError, assert_within_paths, assert_within_write_paths
from datacron.core.temporal import rerank_temporal
from datacron.indexing.reconcile import ReconcileStats, reconcile
from datacron.indexing.ripgrep import RipgrepError
from datacron.mcp.sandbox import wrap_vault_content

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["GetNoteFormat", "register_tools"]

_LOGGER = get_logger(__name__)

GetNoteFormat = str  # "full" | "map" — kept loose for FastMCP schema
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CHUNK_ID_SEPARATOR: Final[str] = "::"
_TOKEN_ESTIMATE_DIVISOR: Final[int] = 4
_MEMORY_ORIGINS: Final[frozenset[str]] = frozenset({"ai", "human", "merged"})
_MEMORY_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low", "needs_verification"}
)
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")


def register_tools(server: FastMCP[Any], app: Any) -> None:
    """Attach the Sem-2 tools to ``server``.

    ``app`` is the :class:`DatacronApp` bundle; typed loosely to avoid a
    circular import with :mod:`datacron.mcp.server`.
    """

    @server.tool(
        name="list_notes",
        title="List notes",
        description=(
            "Return an offset/limit paginated list of notes in the vault, optionally "
            "scoped to a subfolder and/or filtered by tags. Each entry includes the "
            "stable ULID, title, tags, aliases, and timestamps."
        ),
    )
    async def list_notes(
        folder: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await _list_notes_impl(app, folder=folder, tags=tags, limit=limit, offset=offset)

    @server.tool(
        name="get_note",
        title="Get a note",
        description=(
            "Fetch a single note by its ULID, indexed chunk_id, or vault-relative path. "
            "format='full' returns the sandbox-wrapped body; offset/limit page large "
            "notes by character range. format='map' returns the heading outline only "
            "(cheap to scan before requesting full content)."
        ),
    )
    async def get_note(
        id_or_path: str,
        format: GetNoteFormat = "full",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await _get_note_impl(
            app,
            id_or_path=id_or_path,
            fmt=format,
            offset=offset,
            limit=limit,
        )

    @server.tool(
        name="search_text",
        title="Search text (BM25)",
        description=(
            "Full-text BM25 search over the FTS5 index. Returns ranked sandbox-"
            "wrapped snippets with **term** highlighting. Requires `datacron index` "
            "to have been run first. By default, explicitly superseded notes are "
            "demoted; set include_superseded=true to inspect historical notes."
        ),
    )
    async def search_text(
        query: str,
        limit: int = 20,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        return await _search_text_impl(
            app,
            query=query,
            limit=limit,
            include_superseded=include_superseded,
        )

    @server.tool(
        name="search_regex",
        title="Search regex (ripgrep)",
        description=(
            "Regex search via ripgrep. Returns ranked sandbox-wrapped match lines "
            "with **term** highlighting, resolved to indexed chunks. Restrict file "
            "scope with `glob` (e.g. '*.md'). Requires `rg` on PATH and "
            "`datacron index` for chunk resolution."
        ),
    )
    async def search_regex(
        pattern: str,
        glob: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await _search_regex_impl(app, pattern=pattern, glob=glob, limit=limit)

    @server.tool(
        name="get_backlinks",
        title="Get backlinks",
        description=(
            "Return chunks whose wikilinks point at the given target. Target may be "
            "a note ULID or a wikilink alias (resolved via title → filename → "
            "aliases). Empty list if unresolved or no incoming links."
        ),
    )
    async def get_backlinks(target: str, limit: int = 20) -> dict[str, Any]:
        return await _get_backlinks_impl(app, target=target, limit=limit)

    @server.tool(
        name="create_note_ai",
        title="Create memory note",
        description=(
            "Write a new typed _memory Markdown note. This is a write operation: "
            "it is confined to DATACRON_WRITE_PATHS, never overwrites existing "
            "files, snapshots destructive writes in lower-level primitives, and "
            "relies on the MCP client's tool approval for human-in-the-loop review."
        ),
    )
    async def create_note_ai(
        rel_path: str,
        title: str,
        body: str,
        origin: str,
        confidence: str,
        tags: list[str],
        supersedes: list[str] | None = None,
        last_verified: str | None = None,
    ) -> dict[str, Any]:
        return await _create_note_ai_impl(
            app,
            rel_path=rel_path,
            title=title,
            body=body,
            origin=origin,
            confidence=confidence,
            tags=tags,
            supersedes=supersedes,
            last_verified=last_verified,
        )

    @server.tool(
        name="append_journal",
        title="Append to memory note",
        description=(
            "Append a Markdown entry under a heading in an existing memory note. "
            "This is a write operation: it is confined to DATACRON_WRITE_PATHS, "
            "uses reversible backups before overwrite, writes atomically, and "
            "relies on the MCP client's tool approval for human-in-the-loop review."
        ),
    )
    async def append_journal(rel_path: str, heading: str, entry: str) -> dict[str, Any]:
        return await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading=heading,
            entry=entry,
        )

    @server.tool(
        name="set_frontmatter",
        title="Set lifecycle frontmatter",
        description=(
            "Update lifecycle frontmatter fields on an existing memory note. "
            "This write operation only changes origin, confidence, "
            "last_verified, supersedes, and the automatic updated timestamp; "
            "the Markdown body is preserved."
        ),
    )
    async def set_frontmatter(
        rel_path: str,
        confidence: str | None = None,
        last_verified: str | None = None,
        supersedes: list[str] | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        return await _set_frontmatter_impl(
            app,
            rel_path=rel_path,
            confidence=confidence,
            last_verified=last_verified,
            supersedes=supersedes,
            origin=origin,
        )

    @server.tool(
        name="patch_note_section",
        title="Patch note section",
        description=(
            "Replace the content under one existing Markdown heading. "
            "This write operation requires the caller to pass the note's "
            "current content_hash as expected_hash, preserves the heading line "
            "and non-target sections, snapshots the prior file, and writes atomically."
        ),
    )
    async def patch_note_section(
        rel_path: str,
        heading: str,
        new_content: str,
        expected_hash: str,
        heading_level: int | None = None,
    ) -> dict[str, Any]:
        return await _patch_note_section_impl(
            app,
            rel_path=rel_path,
            heading=heading,
            new_content=new_content,
            expected_hash=expected_hash,
            heading_level=heading_level,
        )


# ---------------------------------------------------------------------------
# Implementations (kept as module-level coroutines so they're easy to unit-test
# without spinning a FastMCP instance).
# ---------------------------------------------------------------------------


async def _list_notes_impl(
    app: DatacronApp,
    *,
    folder: str | None,
    tags: list[str] | None,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    validation_error = _validate_list_notes_request(offset=offset)
    if validation_error is not None:
        exc, context = validation_error
        return _error_response("list_notes", exc, started, folder=folder, **context)
    try:
        notes = await app.vault_reader.list_notes(folder=folder)
    except (FileNotFoundError, ValueError, PathConfinementError) as exc:
        return _error_response("list_notes", exc, started, folder=folder)
    except Exception:
        # Defensive: per brief, any unexpected tool failure must log a
        # traceback and return an error rather than crashing the server.
        _LOGGER.exception("list_notes failed (folder=%r)", folder)
        return _error_response("list_notes", RuntimeError("internal error"), started, folder=folder)

    filtered = _filter_by_tags(notes, tags)
    total = len(filtered)
    start = min(offset, total)
    end = min(start + bounded_limit, total)
    returned = filtered[start:end]
    next_offset = end if end < total else None
    payload = {
        "notes": [_note_summary(note) for note in returned],
        "total": total,
        "returned": len(returned),
        "offset": start,
        "next_offset": next_offset,
        "truncated": start > 0 or next_offset is not None,
        "limit_applied": bounded_limit,
    }
    _audit(
        "list_notes",
        started,
        folder=folder,
        tags=tags,
        limit=limit,
        offset=offset,
        bounded_limit=bounded_limit,
        total=total,
        returned=len(returned),
    )
    return payload


async def _get_note_impl(
    app: DatacronApp,
    *,
    id_or_path: str,
    fmt: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    validation_error = _validate_get_note_request(fmt=fmt, offset=offset, limit=limit)
    if validation_error is not None:
        exc, fields = validation_error
        return _error_response(
            "get_note",
            exc,
            started,
            id_or_path=id_or_path,
            fmt=fmt,
            **fields,
        )

    try:
        note = await _resolve_note(app, id_or_path)
    except (FileNotFoundError, ValueError, PathConfinementError) as exc:
        return _error_response("get_note", exc, started, id_or_path=id_or_path, fmt=fmt)
    except Exception:
        _LOGGER.exception("get_note failed (id_or_path=%r)", id_or_path)
        return _error_response(
            "get_note", RuntimeError("internal error"), started, id_or_path=id_or_path, fmt=fmt
        )

    if note is None:
        return _error_response(
            "get_note",
            FileNotFoundError(f"No note found for {id_or_path!r}"),
            started,
            id_or_path=id_or_path,
            fmt=fmt,
        )

    payload = (
        _build_map_payload(app, note)
        if fmt == "map"
        else _build_full_payload(app, note, offset=offset, limit=limit)
    )

    _audit(
        "get_note",
        started,
        id_or_path=id_or_path,
        fmt=fmt,
        offset=offset if fmt == "full" else None,
        limit=limit if fmt == "full" else None,
        note_id=note.id,
        note_rel_path=note.rel_path,
        truncated=bool(payload.get("truncated", False)),
    )
    return payload


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
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        cleaned = _validate_memory_frontmatter(
            rel_path=rel_path,
            title=title,
            body=body,
            origin=origin,
            confidence=confidence,
            tags=tags,
        )
        note_id = str(ULID())
        now = datetime.now(tz=UTC)
        frontmatter = {
            "id": note_id,
            "title": cleaned["title"],
            "created": now.isoformat(),
            "updated": now.isoformat(),
            "origin": cleaned["origin"],
            "confidence": cleaned["confidence"],
            "last_verified": (last_verified.strip() if last_verified else now.date().isoformat()),
            "supersedes": _clean_string_list(supersedes or []),
            "tags": cleaned["tags"],
        }
        content = serialize(frontmatter, body)
        await app.vault_writer.write_note_atomic(cleaned["rel_path"], content, overwrite=False)
        index_stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled — set DATACRON_WRITE_PATHS")
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
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        cleaned_rel_path, cleaned_heading, cleaned_entry = _validate_append_journal_request(
            rel_path=rel_path,
            heading=heading,
            entry=entry,
        )
        resolved = assert_within_write_paths(app.vault_root / cleaned_rel_path, app.settings)
        resolved = assert_within_paths(resolved, [app.vault_root], kind="write")
        if not resolved.exists():
            raise FileNotFoundError(f"note not found at {cleaned_rel_path}; use create_note_ai")
        raw = resolved.read_text(encoding="utf-8")
        metadata, body = parse(raw)
        new_body = _append_entry_to_heading(body, cleaned_heading, cleaned_entry)
        metadata["updated"] = datetime.now(tz=UTC).isoformat()
        content = serialize(metadata, new_body)
        await app.vault_writer.write_note_atomic(cleaned_rel_path, content, overwrite=True)
        index_stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled — set DATACRON_WRITE_PATHS")
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
    except ValueError as exc:
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
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
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
        resolved = assert_within_write_paths(app.vault_root / cleaned_rel_path, app.settings)
        resolved = assert_within_paths(resolved, [app.vault_root], kind="write")
        if not resolved.exists():
            raise FileNotFoundError(f"note not found at {cleaned_rel_path}; use create_note_ai")

        raw = resolved.read_text(encoding="utf-8")
        metadata, body = parse(raw)
        if not metadata:
            raise ValueError("note has no frontmatter")

        changed_fields: list[str] = []
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
            _set_changed_frontmatter_field(metadata, changed_fields, "origin", cleaned_origin)

        metadata["updated"] = datetime.now(tz=UTC).isoformat()
        content = serialize(metadata, body)
        await app.vault_writer.write_note_atomic(cleaned_rel_path, content, overwrite=True)
        index_stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled — set DATACRON_WRITE_PATHS")
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
    expected_hash: str,
    heading_level: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
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
        resolved = assert_within_write_paths(app.vault_root / cleaned_rel_path, app.settings)
        resolved = assert_within_paths(resolved, [app.vault_root], kind="write")
        if not resolved.exists():
            raise FileNotFoundError(f"note not found at {cleaned_rel_path}; use create_note_ai")

        raw = resolved.read_text(encoding="utf-8")
        if hash_text(raw) != cleaned_expected_hash:
            raise ValueError("note changed since read (hash mismatch); re-read and retry")

        metadata, body = parse(raw)
        lines = body.splitlines(keepends=True)
        content_start, content_end = _find_section_span(
            lines,
            cleaned_heading,
            cleaned_heading_level,
        )
        matched_heading = _parse_heading_line(lines[content_start - 1])
        if matched_heading is None:
            raise RuntimeError("section span does not follow a heading")
        matched_level, matched_text = matched_heading
        prefix = "".join(lines[:content_start])
        suffix = "".join(lines[content_end:])
        new_body = (
            f"{prefix}"
            f"{_section_replacement_block(cleaned_new_content, prefix=prefix, suffix=suffix)}"
            f"{suffix}"
        )
        metadata["updated"] = datetime.now(tz=UTC).isoformat()
        content = serialize(metadata, new_body)
        await app.vault_writer.write_note_atomic(cleaned_rel_path, content, overwrite=True)
        index_stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
    except PathConfinementError as exc:
        mapped_exc = (
            PathConfinementError("writes disabled — set DATACRON_WRITE_PATHS")
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bounded_count(requested: int, ceiling: int) -> int:
    if requested <= 0:
        return ceiling
    return min(requested, ceiling)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _TOKEN_ESTIMATE_DIVISOR)


def _validate_get_note_request(
    *,
    fmt: str,
    offset: int,
    limit: int | None,
) -> tuple[BaseException, dict[str, int | None]] | None:
    if fmt not in _VALID_FORMATS:
        return ValueError(f"format must be one of {sorted(_VALID_FORMATS)}"), {}
    if offset < 0:
        return ValueError("offset must be >= 0"), {"offset": offset}
    if limit is not None and limit <= 0:
        return ValueError("limit must be > 0"), {"limit": limit}
    return None


def _validate_list_notes_request(*, offset: int) -> tuple[BaseException, dict[str, int]] | None:
    if offset < 0:
        return ValueError("offset must be >= 0"), {"offset": offset}
    return None


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
    expected_hash: str,
    heading_level: int | None,
) -> tuple[str, str, str, str, int | None]:
    cleaned_rel_path = rel_path.strip()
    cleaned_heading = heading.strip()
    cleaned_expected_hash = expected_hash.strip()

    if not cleaned_rel_path.endswith(".md"):
        raise ValueError("rel_path must end with .md")
    if not cleaned_heading:
        raise ValueError("heading must not be empty")
    if not new_content.strip():
        raise ValueError("new_content must not be empty")
    if not _CONTENT_HASH_PATTERN.fullmatch(cleaned_expected_hash):
        raise ValueError(f"expected_hash must be a lowercase {HASH_HEX_LENGTH}-character SHA-256")
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


def _append_entry_to_heading(body: str, heading: str, entry: str) -> str:
    lines = body.splitlines(keepends=True)
    section = _find_heading_section(lines, heading)
    if section is None:
        suffix = "" if not body else "\n\n"
        entry_block = entry if entry.endswith("\n") else f"{entry}\n"
        return f"{body}{suffix}## {heading}\n\n{entry_block}"

    _heading_index, _level, insert_at = section
    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])
    block = _entry_block(entry, prefix=prefix, suffix=suffix)
    return f"{prefix}{block}{suffix}"


def _find_heading_section(lines: list[str], heading: str) -> tuple[int, int, int] | None:
    for index, line in enumerate(lines):
        parsed = _parse_heading_line(line)
        if parsed is None:
            continue
        level, text = parsed
        if text != heading:
            continue
        insert_at = len(lines)
        for next_index in range(index + 1, len(lines)):
            next_heading = _parse_heading_line(lines[next_index])
            if next_heading is not None and next_heading[0] <= level:
                insert_at = next_index
                break
        insert_at = _trim_trailing_blank_lines(lines, index + 1, insert_at)
        return index, level, insert_at
    return None


def _find_section_span(
    lines: list[str],
    heading: str,
    heading_level: int | None,
) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        parsed = _parse_heading_line(line)
        if parsed is None:
            continue
        level, text = parsed
        if text != heading:
            continue
        if heading_level is not None and level != heading_level:
            continue
        matches.append((index, level))

    if not matches:
        raise ValueError("heading not found; nothing to patch")
    if len(matches) > 1:
        raise ValueError(f"heading is ambiguous ({len(matches)} matches); pass heading_level")

    heading_index, level = matches[0]
    content_start = heading_index + 1
    content_end = len(lines)
    for next_index in range(content_start, len(lines)):
        next_heading = _parse_heading_line(lines[next_index])
        if next_heading is not None and next_heading[0] <= level:
            content_end = next_index
            break
    return content_start, content_end


def _trim_trailing_blank_lines(lines: list[str], start: int, end: int) -> int:
    insert_at = end
    while insert_at > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    return insert_at


def _parse_heading_line(line: str) -> tuple[int, str] | None:
    match = _HEADING_HASH_PATTERN.match(line)
    if match is None:
        return None
    level = len(match.group(1))
    text = line[match.end() :].strip()
    return level, text


def _entry_block(entry: str, *, prefix: str, suffix: str) -> str:
    leading = "" if not prefix else "\n\n" if not prefix.endswith("\n") else "\n"
    entry_block = entry if entry.endswith("\n") else f"{entry}\n"
    trailing = "" if not suffix or suffix.startswith("\n") else "\n"
    return f"{leading}{entry_block}{trailing}"


def _section_replacement_block(new_content: str, *, prefix: str, suffix: str) -> str:
    leading = "\n\n" if prefix and not prefix.endswith("\n") else "\n"
    content_block = f"{new_content}\n"
    trailing = "" if not suffix or suffix.startswith("\n") else "\n"
    return f"{leading}{content_block}{trailing}"


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


def _filter_by_tags(notes: list[Note], tags: list[str] | None) -> list[Note]:
    if not tags:
        return notes
    required = {t.strip().lower() for t in tags if t.strip()}
    if not required:
        return notes
    return [note for note in notes if required.issubset(set(note.tags))]


def _note_summary(note: Note) -> dict[str, Any]:
    return {
        "id": note.id,
        "rel_path": note.rel_path,
        "title": note.title,
        "tags": list(note.tags),
        "aliases": list(note.aliases),
        "frontmatter": dict(note.frontmatter),
        "created": note.created.isoformat(),
        "updated": note.updated.isoformat(),
    }


async def _resolve_note(app: DatacronApp, id_or_path: str) -> Note | None:
    """Return the Note for either a ULID or a vault-relative path."""
    if _CHUNK_ID_SEPARATOR in id_or_path:
        chunk = None
        try:
            chunk = await app.store.get_chunk(id_or_path)
        except RuntimeError:
            chunk = None
        if chunk is not None:
            return await _read_note_by_rel_path(app, chunk.note_rel_path)

        note_id = id_or_path.split(_CHUNK_ID_SEPARATOR, 1)[0]
        if _ULID_PATTERN.match(note_id):
            return await _resolve_note(app, note_id)
        return None

    if _ULID_PATTERN.match(id_or_path):
        return await _resolve_note_by_ulid(app, id_or_path)
    return await _read_note_by_rel_path(app, id_or_path)


async def _resolve_note_by_ulid(app: DatacronApp, note_id: str) -> Note | None:
    # Fast path: the index maps rel_path -> note_id without reading notes.
    indexed = await app.store.list_indexed_notes_with_mtime()
    for rel_path, (indexed_note_id, _hash, _mtime) in indexed.items():
        if indexed_note_id == note_id:
            try:
                return await _read_note_by_rel_path(app, rel_path)
            except FileNotFoundError:
                break

    # Fallback: fresh notes can exist on disk before the next reindex.
    for note in await app.vault_reader.list_notes():
        if note.id == note_id:
            return note
    return None


async def _read_note_by_rel_path(app: DatacronApp, rel_path: str) -> Note:
    candidate = (app.vault_root / rel_path).expanduser()
    resolved = assert_within_paths(candidate, [app.vault_root], kind="read")
    return await app.vault_reader.read_note(resolved)


def _build_full_payload(
    app: DatacronApp,
    note: Note,
    *,
    offset: int,
    limit: int | None,
) -> dict[str, Any]:
    max_tokens = app.settings.get_note_max_tokens
    max_chars = max_tokens * _TOKEN_ESTIMATE_DIVISOR
    total_chars = len(note.content)
    start = min(offset, total_chars)
    requested_limit = limit if limit is not None else max_chars
    limit_applied = min(requested_limit, max_chars)
    end = min(start + limit_applied, total_chars)
    content = note.content[start:end]
    truncated = start > 0 or end < total_chars
    next_offset = end if end < total_chars else None

    wrapped = wrap_vault_content(note.rel_path, content)
    return {
        "id": note.id,
        "rel_path": note.rel_path,
        "title": note.title,
        "tags": list(note.tags),
        "aliases": list(note.aliases),
        "frontmatter": dict(note.frontmatter),
        "created": note.created.isoformat(),
        "updated": note.updated.isoformat(),
        "content_hash": note.content_hash,
        "format": "full",
        "content": wrapped,
        "estimated_tokens": _estimate_tokens(note.content),
        "returned_estimated_tokens": _estimate_tokens(content),
        "offset": start,
        "limit_applied": limit_applied,
        "total_chars": total_chars,
        "returned_chars": len(content),
        "next_offset": next_offset,
        "truncated": truncated,
    }


def _build_map_payload(app: DatacronApp, note: Note) -> dict[str, Any]:
    chunks = app.chunker.chunk(note)
    headings: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.chunk_type is not ChunkType.HEADING:
            continue
        match = _HEADING_HASH_PATTERN.match(chunk.content)
        level = len(match.group(1)) if match else 1
        headings.append(
            {
                "level": level,
                "text": chunk.section_title or chunk.content.lstrip("# ").strip(),
                "path": chunk.header_path,
                "chunk_id": chunk.chunk_id,
            }
        )
    return {
        "id": note.id,
        "rel_path": note.rel_path,
        "title": note.title,
        "format": "map",
        "headings": headings,
        "chunk_count": len(chunks),
    }


def _error_response(tool: str, exc: BaseException, started: float, **fields: Any) -> dict[str, Any]:
    _audit(tool, started, error=type(exc).__name__, error_message=str(exc), **fields)
    return {
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    }


def _audit(tool: str, started: float, **fields: Any) -> None:
    duration_ms = (time.perf_counter() - started) * 1000.0
    rendered = " ".join(f"{key}={value!r}" for key, value in fields.items() if value is not None)
    _LOGGER.info("AUDIT tool=%s duration_ms=%.2f %s", tool, duration_ms, rendered)


# ---------------------------------------------------------------------------
# Sem 3 — search_text / search_regex / get_backlinks
# ---------------------------------------------------------------------------


async def _search_text_impl(
    app: DatacronApp,
    *,
    query: str,
    limit: int,
    include_superseded: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    cleaned = query.strip()
    if not cleaned:
        return _error_response(
            "search_text",
            ValueError("query must not be empty"),
            started,
            query=query,
        )
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    try:
        repair = await _repair_index_on_read(app)
        raw_results = await app.store.search(
            cleaned,
            limit=bounded_limit * TEMPORAL_OVERFETCH_FACTOR,
        )
        temporal_meta = await app.store.list_temporal_metadata()
        raw_results = rerank_temporal(
            raw_results,
            temporal_meta,
            include_superseded=include_superseded,
        )[:bounded_limit]
    except Exception:
        _LOGGER.exception("search_text failed (query=%r)", query)
        return _error_response("search_text", RuntimeError("internal error"), started, query=query)

    results, truncated_for_tokens = _apply_token_budget(
        raw_results, max_tokens=app.settings.max_result_tokens
    )
    payload: dict[str, Any] = {
        "query": cleaned,
        "results": [_search_result_summary(r) for r in results],
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    if repair["reindexed_notes"] or repair["deleted_notes"]:
        payload["index_repair"] = repair
    _audit(
        "search_text",
        started,
        query=cleaned,
        limit=limit,
        bounded_limit=bounded_limit,
        returned=len(results),
        include_superseded=include_superseded,
        reindexed_notes=repair["reindexed_notes"],
        deleted_notes=repair["deleted_notes"],
        truncated_for_tokens=truncated_for_tokens,
    )
    return payload


async def _search_regex_impl(
    app: DatacronApp,
    *,
    pattern: str,
    glob: str | None,
    limit: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    if not pattern:
        return _error_response(
            "search_regex",
            ValueError("pattern must not be empty"),
            started,
            pattern=pattern,
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        return _error_response(
            "search_regex",
            ValueError(f"invalid regex: {exc}"),
            started,
            pattern=pattern,
        )
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    try:
        repair = await _repair_index_on_read(app)
        raw_results = await app.ripgrep.search(
            pattern=pattern,
            vault_root=app.vault_root,
            glob=glob,
            limit=bounded_limit,
            store=app.store,
            rg_path=app.settings.ripgrep_path,
        )
    except FileNotFoundError as exc:
        return _error_response("search_regex", exc, started, pattern=pattern, glob=glob)
    except RipgrepError as exc:
        message = exc.stderr.strip() or str(exc)
        return _error_response(
            "search_regex",
            ValueError(f"pattern rejected by ripgrep: {message}"),
            started,
            pattern=pattern,
            glob=glob,
        )
    except Exception:
        _LOGGER.exception("search_regex failed (pattern=%r glob=%r)", pattern, glob)
        return _error_response(
            "search_regex",
            RuntimeError("internal error"),
            started,
            pattern=pattern,
            glob=glob,
        )

    results, truncated_for_tokens = _apply_token_budget(
        raw_results, max_tokens=app.settings.max_result_tokens
    )
    payload: dict[str, Any] = {
        "pattern": pattern,
        "glob": glob,
        "results": [_search_result_summary(r) for r in results],
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    if repair["reindexed_notes"] or repair["deleted_notes"]:
        payload["index_repair"] = repair
    _audit(
        "search_regex",
        started,
        pattern=pattern,
        glob=glob,
        limit=limit,
        bounded_limit=bounded_limit,
        returned=len(results),
        reindexed_notes=repair["reindexed_notes"],
        deleted_notes=repair["deleted_notes"],
        truncated_for_tokens=truncated_for_tokens,
    )
    return payload


async def _get_backlinks_impl(
    app: DatacronApp,
    *,
    target: str,
    limit: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    cleaned = target.strip()
    if not cleaned:
        return _error_response(
            "get_backlinks",
            ValueError("target must not be empty"),
            started,
            target=target,
        )

    try:
        resolved_id = await _resolve_backlink_target(app, cleaned)
    except Exception:
        _LOGGER.exception("get_backlinks resolution failed (target=%r)", target)
        return _error_response(
            "get_backlinks", RuntimeError("internal error"), started, target=target
        )

    if resolved_id is None:
        payload_unresolved: dict[str, Any] = {
            "target": cleaned,
            "resolved_note_id": None,
            "results": [],
            "returned": 0,
            "limit_applied": bounded_limit,
        }
        _audit(
            "get_backlinks",
            started,
            target=cleaned,
            resolved_note_id=None,
            returned=0,
        )
        return payload_unresolved

    try:
        repair = await _repair_index_on_read(app)
        sources = await _find_backlink_sources(app, resolved_id, cleaned, bounded_limit)
    except Exception:
        _LOGGER.exception("get_backlinks scan failed (target=%r id=%r)", target, resolved_id)
        return _error_response(
            "get_backlinks",
            RuntimeError("internal error"),
            started,
            target=target,
            resolved_note_id=resolved_id,
        )

    payload: dict[str, Any] = {
        "target": cleaned,
        "resolved_note_id": resolved_id,
        "results": sources,
        "returned": len(sources),
        "limit_applied": bounded_limit,
    }
    if repair["reindexed_notes"] or repair["deleted_notes"]:
        payload["index_repair"] = repair
    _audit(
        "get_backlinks",
        started,
        target=cleaned,
        resolved_note_id=resolved_id,
        returned=len(sources),
        reindexed_notes=repair["reindexed_notes"],
        deleted_notes=repair["deleted_notes"],
    )
    return payload


# ---------------------------------------------------------------------------
# Helpers (Sem 3)
# ---------------------------------------------------------------------------


def _apply_token_budget(
    results: list[SearchResult],
    *,
    max_tokens: int,
) -> tuple[list[SearchResult], bool]:
    """Keep results in order until their cumulative token_count exceeds ``max_tokens``."""
    kept: list[SearchResult] = []
    running_total = 0
    for result in results:
        running_total += max(1, result.chunk.token_count)
        if kept and running_total > max_tokens:
            return kept, True
        kept.append(result)
    return kept, False


def _search_result_summary(result: SearchResult) -> dict[str, Any]:
    chunk = result.chunk
    wrapped_snippet = wrap_vault_content(chunk.note_rel_path, result.snippet)
    return {
        "chunk_id": chunk.chunk_id,
        "note_id": chunk.note_id,
        "note_rel_path": chunk.note_rel_path,
        "header_path": chunk.header_path,
        "section_title": chunk.section_title,
        "chunk_type": chunk.chunk_type.value,
        "score": result.score,
        "snippet": wrapped_snippet,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "token_count": chunk.token_count,
    }


async def _repair_index_on_read(app: DatacronApp) -> ReconcileStats:
    """Synchronize the FTS index with the live vault before index-backed reads.

    Delegates to the shared incremental :func:`reconcile` with the mtime gate
    enabled, so an unchanged vault costs one ``stat`` sweep rather than a full
    re-read+hash of every note. ``content_hash`` remains the authority on any
    note whose mtime moved.
    """
    return await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)


async def _resolve_backlink_target(app: DatacronApp, target: str) -> str | None:
    """Return a note_id from a ULID or an alias, or None if unresolved."""
    if _ULID_PATTERN.match(target):
        return target
    return await app.vault_reader.resolve_alias(target)


async def _find_backlink_sources(
    app: DatacronApp,
    target_note_id: str,
    target_alias: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Scan indexed wikilink metadata and return source chunks pointing at the target.

    A chunk is considered a backlink source if any of its indexed wikilinks
    resolves (via :meth:`VaultReader.resolve_alias`) to ``target_note_id``.
    A small alias-resolution cache amortizes the per-link cost when multiple
    chunks reference the same target string.
    """
    target_alias_lower = target_alias.strip().lower()
    alias_cache: dict[str, str | None] = {target_alias_lower: target_note_id}
    seen_chunk_ids: set[str] = set()
    sources: list[dict[str, Any]] = []

    for chunk in await app.store.list_chunks_with_wikilinks():
        if chunk.note_id == target_note_id:
            continue
        if chunk.chunk_id in seen_chunk_ids:
            continue
        if not await _chunk_links_to(app, chunk.wikilinks_out, target_note_id, alias_cache):
            continue
        seen_chunk_ids.add(chunk.chunk_id)
        sources.append(
            {
                "source_chunk_id": chunk.chunk_id,
                "source_note_id": chunk.note_id,
                "source_note_rel_path": chunk.note_rel_path,
                "header_path": chunk.header_path,
                "section_title": chunk.section_title,
            }
        )
        if len(sources) >= limit:
            return sources
    return sources


async def _chunk_links_to(
    app: DatacronApp,
    wikilinks: list[str],
    target_note_id: str,
    alias_cache: dict[str, str | None],
) -> bool:
    """Return True if any wikilink in ``wikilinks`` resolves to ``target_note_id``.

    Indexed wikilinks are raw target aliases; resolution happens here via
    :meth:`VaultReader.resolve_alias`, cached across the scan.
    """
    for target_alias in wikilinks:
        key = target_alias.strip().lower()
        if not key:
            continue
        if key not in alias_cache:
            alias_cache[key] = await app.vault_reader.resolve_alias(target_alias)
        if alias_cache[key] == target_note_id:
            return True
    return False
