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
"""Note listing and retrieval tool implementations."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Final

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.frontmatter import matches_frontmatter_filter
from datacron.core.hashing import FRESHNESS_CONTRACT_ID
from datacron.core.markdown_headings import MarkdownHeading, markdown_headings
from datacron.core.markdown_sections import heading_ancestry
from datacron.core.models import Chunk, ChunkType, Note
from datacron.core.paths import PathConfinementError, read_ulid_mappings, sidecar_dir
from datacron.core.scope import NoteAdmissionError
from datacron.core.vault import ULID_SIDECAR_FILENAME
from datacron.indexing.chunker import content_line_offset
from datacron.mcp.sandbox import (
    sanitize_payload_strings,
    wrap_vault_content,
)
from datacron.mcp.tools.payloads import (
    _audit,
    _bounded_count,
    _error_response,
    _estimate_tokens,
    _internal_error_response,
    _redact_retrieval_text,
    _sanitize_retrieval_metadata,
    _validate_frontmatter_filter,
)
from datacron.mcp.tools.retrieval import (
    OPAQUE_CHUNK_PATTERN,
    opaque_chunk_id,
    protect_chunk_metadata,
    protect_note_title,
)
from datacron.mcp.tools.search import _repair_index_on_read

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map", "chunk"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CHUNK_ID_SEPARATOR: Final[str] = "::"
_MAX_HEADING_PATH_DEPTH: Final[int] = 6


class StaleChunkError(ValueError):
    """Raised when a chunk belongs to an older byte version of its note."""


async def _list_notes_impl(
    app: DatacronApp,
    *,
    folder: str | None,
    tags: list[str] | None,
    limit: int,
    offset: int = 0,
    frontmatter: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    validation_error = _validate_list_notes_request(offset=offset, frontmatter=frontmatter)
    if validation_error is not None:
        exc, context = validation_error
        return _error_response(
            "list_notes",
            exc,
            started,
            folder=folder,
            frontmatter=frontmatter,
            **context,
        )
    try:
        indexed_page = await _list_notes_from_index(
            app,
            folder=folder,
            tags=tags,
            frontmatter=frontmatter,
            limit=bounded_limit,
            offset=offset,
        )
        if indexed_page is None:
            notes = await app.vault_reader.list_notes(folder=folder)
            filtered = [
                note
                for note in _filter_by_tags(notes, tags)
                if matches_frontmatter_filter(note.frontmatter, frontmatter)
            ]
            total = len(filtered)
            start = min(offset, total)
            returned = filtered[start : start + bounded_limit]
        else:
            returned, total = indexed_page
            start = min(offset, total)
    except (FileNotFoundError, ValueError, PathConfinementError) as exc:
        return _error_response(
            "list_notes",
            exc,
            started,
            folder=folder,
            tags=tags,
            frontmatter=frontmatter,
        )
    except Exception:
        # Defensive: per brief, any unexpected tool failure must log a
        # traceback and return an error rather than crashing the server.
        return _internal_error_response(
            "list_notes", started, folder=folder, tags=tags, frontmatter=frontmatter
        )

    end = min(start + bounded_limit, total)
    next_offset = end if end < total else None
    payload = {
        "notes": [_note_summary(app, note) for note in returned],
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
        frontmatter=frontmatter,
        limit=limit,
        offset=offset,
        bounded_limit=bounded_limit,
        total=total,
        returned=len(returned),
    )
    return payload


async def _list_notes_from_index(
    app: DatacronApp,
    *,
    folder: str | None,
    tags: list[str] | None,
    frontmatter: dict[str, str] | None,
    limit: int,
    offset: int,
) -> tuple[list[Note], int] | None:
    """Return an index-selected note page, or ``None`` for filesystem fallback."""
    authorized_folder = app.scope.authorize_rel_path(folder or "", "read")
    relative_folder = authorized_folder.relative_to(app.vault_root)
    normalized_folder = None if relative_folder.name == "" else relative_folder.as_posix()
    try:
        repair = await _repair_index_on_read(app)
        _, indexed_total = await app.store.list_note_paths(
            folder=normalized_folder,
            tags=tags or [],
            frontmatter=frontmatter,
            limit=1,
            offset=0,
        )
        rel_paths: list[str] = []
        if indexed_total:
            rel_paths, _ = await app.store.list_note_paths(
                folder=normalized_folder,
                tags=tags or [],
                frontmatter=frontmatter,
                limit=indexed_total,
                offset=0,
            )
    except RuntimeError:
        return None

    indexed_after = (
        repair["indexed_notes_before"] + repair["reindexed_notes"] - repair["deleted_notes"]
    )
    if repair["checked_notes"] and indexed_after <= 0:
        return None
    admitted_paths = [
        rel_path for rel_path in rel_paths if app.scope.allows_note_rel_path(rel_path)
    ]
    total = len(admitted_paths)
    start = min(offset, total)
    page_paths = admitted_paths[start : start + limit]

    notes: list[Note] = []
    try:
        for rel_path in page_paths:
            notes.append(await _read_note_by_rel_path(app, rel_path))
    except (FileNotFoundError, NoteAdmissionError):
        return None
    return notes, total


async def _get_note_impl(
    app: DatacronApp,
    *,
    id_or_path: str,
    fmt: str,
    offset: int = 0,
    limit: int | None = None,
    heading_path: list[str] | None = None,
    heading_occurrence: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    validation_error = _validate_get_note_request(fmt=fmt, offset=offset, limit=limit)
    if validation_error is None:
        section_error = _validate_section_request(id_or_path, fmt, heading_path, heading_occurrence)
        if section_error is not None:
            validation_error = (section_error, {})
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
        chunk_payload = await _resolve_chunk_payload(app, id_or_path)
        if chunk_payload is not None:
            _audit(
                "get_note",
                started,
                id_or_path=id_or_path,
                fmt="chunk",
                note_id=chunk_payload["note_id"],
                note_rel_path=chunk_payload["rel_path"],
                chunk_id=chunk_payload["chunk_id"],
                truncated=False,
            )
            return chunk_payload

        if fmt == "chunk":
            raise ValueError("format='chunk' requires an indexed chunk_id")

        note = await _resolve_note(app, id_or_path)
        if note is None:
            raise FileNotFoundError(f"No note found for {id_or_path!r}")
    except (NoteAdmissionError, FileNotFoundError, ValueError, PathConfinementError) as exc:
        return _error_response("get_note", exc, started, id_or_path=id_or_path, fmt=fmt)
    except Exception:
        return _internal_error_response("get_note", started, id_or_path=id_or_path, fmt=fmt)

    try:
        payload = (
            _build_section_payload(
                app, note, heading_path, heading_occurrence, offset=offset, limit=limit
            )
            if heading_path is not None
            else _build_map_payload(app, note)
            if fmt == "map"
            else _build_full_payload(app, note, offset=offset, limit=limit)
        )
    except ValueError as exc:
        return _error_response("get_note", exc, started, id_or_path=id_or_path, fmt=fmt)

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


def _validate_section_request(
    id_or_path: str,
    fmt: str,
    heading_path: list[str] | None,
    heading_occurrence: int | None,
) -> ValueError | None:
    if heading_path is None:
        if heading_occurrence is not None:
            return ValueError("heading_occurrence requires heading_path")
        return None
    if fmt != "full" or _CHUNK_ID_SEPARATOR in id_or_path:
        return ValueError("heading_path requires a note input with format='full'")
    if (
        not isinstance(heading_path, list)
        or not heading_path
        or len(heading_path) > _MAX_HEADING_PATH_DEPTH
        or any(not isinstance(part, str) or not part.strip() for part in heading_path)
    ):
        return ValueError("heading_path must contain 1 to 6 nonempty heading strings")
    if heading_occurrence is not None and (
        type(heading_occurrence) is not int or heading_occurrence < 1
    ):
        return ValueError("heading_occurrence must be a positive 1-based integer")
    return None


def _build_section_payload(
    app: DatacronApp,
    note: Note,
    heading_path: list[str],
    heading_occurrence: int | None,
    *,
    offset: int,
    limit: int | None,
) -> dict[str, Any]:
    lines = note.content.splitlines(keepends=True)
    headings = markdown_headings(lines)
    matches: list[tuple[int, list[MarkdownHeading]]] = [
        (index, ancestors)
        for index, ancestors in enumerate(heading_ancestry(headings))
        if [ancestor.text for ancestor in ancestors] == heading_path
    ]
    if not matches:
        raise ValueError("heading_path does not match any heading ancestry")
    if len(matches) > 1 and heading_occurrence is None:
        raise ValueError("heading_path is ambiguous; supply heading_occurrence")
    occurrence = heading_occurrence if heading_occurrence is not None else 1
    if occurrence > len(matches):
        raise ValueError("heading_occurrence exceeds the number of matching headings")
    selected_index, selected_ancestors = matches[occurrence - 1]
    selected = headings[selected_index]
    end = next(
        (item.start for item in headings[selected_index + 1 :] if item.level <= selected.level),
        len(lines),
    )
    content = "".join(lines[selected.start : end])
    line_offset = content_line_offset(note)
    line_start, line_end = line_offset + selected.start + 1, line_offset + end
    returned_path = list(heading_path)
    if app.secret_redactor.retrieval_enabled(app.settings):
        content = app.secret_redactor.redact_fragment(
            content, note.raw_content, line_start, line_end
        )
        returned_path = [
            app.secret_redactor.redact_fragment(
                ancestor.text,
                note.raw_content,
                line_offset + ancestor.start + 1,
                line_offset + ancestor.end,
            )
            for ancestor in selected_ancestors
        ]
    payload = _build_full_payload(app, note, offset=offset, limit=limit, retrieval_content=content)
    payload["section"] = {
        "heading_path": [_sanitize_retrieval_metadata(app, part) for part in returned_path],
        "heading_occurrence": occurrence,
        "matching_headings": len(matches),
        "line_start": line_start,
        "line_end": line_end,
    }
    return payload


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


def _validate_list_notes_request(
    *,
    offset: int,
    frontmatter: dict[str, str] | None,
) -> tuple[BaseException, dict[str, object]] | None:
    if offset < 0:
        return ValueError("offset must be >= 0"), {"offset": offset}
    return _validate_frontmatter_filter(frontmatter)


def _filter_by_tags(notes: list[Note], tags: list[str] | None) -> list[Note]:
    if not tags:
        return notes
    required = {t.strip().lower() for t in tags if t.strip()}
    if not required:
        return notes
    return [note for note in notes if required.issubset(set(note.tags))]


def _note_summary(app: DatacronApp, note: Note) -> dict[str, Any]:
    return {
        "id": note.id,
        "rel_path": _redact_retrieval_text(app, note.rel_path),
        **_sanitized_note_metadata(app, note),
        "created": note.created.isoformat(),
        "updated": note.updated.isoformat(),
    }


def _sanitized_note_metadata(app: DatacronApp, note: Note) -> dict[str, Any]:
    metadata = {
        "title": protect_note_title(app, note),
        "tags": list(note.tags),
        "aliases": list(note.aliases),
        "frontmatter": dict(note.frontmatter),
    }
    if app.secret_redactor.retrieval_enabled(app.settings):
        metadata = app.secret_redactor.redact_value(metadata)
        if metadata["frontmatter"].get("title") == note.title:
            metadata["frontmatter"]["title"] = metadata["title"]
    return sanitize_payload_strings(metadata)


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
    # Fast path: resolve the indexed identity without loading the note catalog.
    try:
        indexed_rel_path = await app.store.get_note_rel_path(note_id)
    except RuntimeError:
        indexed_rel_path = None
    if indexed_rel_path is not None:
        indexed_note = await _try_read_note_by_rel_path(app, indexed_rel_path)
        if indexed_note is not None and indexed_note.id == note_id:
            return indexed_note

    # A sidecar hit is verified against current bytes. Path coverage alone
    # cannot prove absence: a frontmatter ID may have changed since indexing.
    sidecar_note = await _resolve_note_from_sidecar(app, note_id)
    if sidecar_note is not None:
        return sidecar_note

    # Fallback: fresh notes can exist on disk before the next reindex.
    return await _resolve_note_from_unindexed_notes(app, note_id)


async def _resolve_note_from_unindexed_notes(app: DatacronApp, note_id: str) -> Note | None:
    """Look for ``note_id`` in the notes the index has not seen in their current state.

    ``get_note`` does not repair the index first, so a note written since the last
    pass can carry an identity the index has never recorded. Finding it used to mean
    reading, decoding, hashing and YAML-parsing every note in the vault, on the event
    loop, and the identity that triggers it is the one that resolves nowhere: a
    plausible but nonexistent ULID, which a caller can repeat, stalls the server for
    every other tool call each time and then answers that there is no such note.

    A note whose stored mtime still matches the file cannot carry an identity the
    index missed, because the index read that exact file. Only the notes that are new
    or have moved since are examined, which on a vault nobody has edited outside
    Datacron is none of them. That is the gate ``reconcile`` already applies, and like
    ``reconcile`` it trusts the mtime only to decide whether to look, never to decide
    what the content is.
    """
    try:
        indexed = await app.store.list_indexed_notes_with_mtime()
    except RuntimeError:
        indexed = {}
    for rel_path, (path, fs_mtime_ns) in (await app.vault_reader.stat_notes()).items():
        entry = indexed.get(rel_path)
        if entry is not None and entry[2] is not None and entry[2] == fs_mtime_ns:
            continue
        try:
            note = await app.vault_reader.read_note(path)
        except (OSError, ValueError, NoteAdmissionError, PathConfinementError):
            continue
        if note.id == note_id:
            return note
    return None


async def _resolve_note_from_sidecar(
    app: DatacronApp,
    note_id: str,
) -> Note | None:
    """Return a sidecar hit only after checking the live note identity."""
    sidecar_path = sidecar_dir(app.vault_root) / ULID_SIDECAR_FILENAME
    if not sidecar_path.is_file():
        return None
    try:
        mappings = await asyncio.to_thread(
            read_ulid_mappings,
            sidecar_path,
            require_string_pairs=True,
        )
    except (OSError, UnicodeError, ValueError):
        return None

    matching_paths = [rel_path for rel_path, mapped_id in mappings.items() if mapped_id == note_id]
    if len(matching_paths) == 1:
        note = await _try_read_note_by_rel_path(app, matching_paths[0])
        return note if note is not None and note.id == note_id else None
    return None


async def _try_read_note_by_rel_path(app: DatacronApp, rel_path: str) -> Note | None:
    try:
        return await _read_note_by_rel_path(app, rel_path)
    except (FileNotFoundError, PathConfinementError, ValueError):
        return None


async def _read_note_by_rel_path(app: DatacronApp, rel_path: str) -> Note:
    resolved = app.scope.authorize_note_rel_path(rel_path)
    return await app.vault_reader.read_note(resolved)


async def _resolve_chunk_payload(app: DatacronApp, id_or_path: str) -> dict[str, Any] | None:
    if _CHUNK_ID_SEPARATOR not in id_or_path:
        return None
    try:
        chunk = await app.store.get_chunk(id_or_path)
        if chunk is None and (opaque_match := OPAQUE_CHUNK_PATTERN.fullmatch(id_or_path)):
            candidates = await app.store.list_chunks_for_note(opaque_match[1])
            chunk = next((item for item in candidates if opaque_chunk_id(item) == id_or_path), None)
    except RuntimeError as exc:
        raise ValueError("chunk_id cannot be resolved while the index is unavailable") from exc
    if chunk is None:
        raise ValueError(
            "chunk_id does not exist in the index; read format=map and use an indexed "
            "chunk_id returned by the server"
        )

    note = await _read_note_by_rel_path(app, chunk.note_rel_path)
    indexed_notes = await app.store.list_indexed_notes()
    indexed_note = indexed_notes.get(chunk.note_rel_path)
    if (
        indexed_note is None
        or indexed_note[0] != chunk.note_id
        or indexed_note[1] != note.content_hash
    ):
        raise StaleChunkError(
            f"chunk_id is stale for {chunk.note_rel_path}; "
            "indexed content_hash does not match current note bytes; reindex and retry"
        )
    chunks = await app.store.list_chunks_for_note(chunk.note_id)
    safe_chunks = protect_chunk_metadata(app, note, chunks)
    safe_chunk = next(
        safe
        for original, safe in zip(chunks, safe_chunks, strict=True)
        if original.chunk_id == chunk.chunk_id
    )
    prev_chunk_id, next_chunk_id = _chunk_neighbor_ids(safe_chunks, safe_chunk.chunk_id)
    return _build_chunk_payload(
        app,
        note,
        safe_chunk,
        prev_chunk_id=prev_chunk_id,
        next_chunk_id=next_chunk_id,
    )


def _chunk_neighbor_ids(chunks: list[Chunk], chunk_id: str) -> tuple[str | None, str | None]:
    for index, chunk in enumerate(chunks):
        if chunk.chunk_id != chunk_id:
            continue
        prev_chunk_id = chunks[index - 1].chunk_id if index > 0 else None
        next_chunk_id = chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
        return prev_chunk_id, next_chunk_id
    return None, None


def _build_full_payload(
    app: DatacronApp,
    note: Note,
    *,
    offset: int,
    limit: int | None,
    retrieval_content: str | None = None,
) -> dict[str, Any]:
    max_tokens = app.settings.get_note_max_tokens
    max_chars = max_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    if retrieval_content is None:
        retrieval_content = _redact_retrieval_text(app, note.content)
    total_chars = len(retrieval_content)
    start = min(offset, total_chars)
    requested_limit = limit if limit is not None else max_chars
    limit_applied = min(requested_limit, max_chars)
    end = min(start + limit_applied, total_chars)
    content = retrieval_content[start:end]
    truncated = start > 0 or end < total_chars
    next_offset = end if end < total_chars else None

    returned_rel_path = _redact_retrieval_text(app, note.rel_path)
    wrapped = wrap_vault_content(returned_rel_path, content)
    return {
        "id": note.id,
        "rel_path": returned_rel_path,
        **_sanitized_note_metadata(app, note),
        "created": note.created.isoformat(),
        "updated": note.updated.isoformat(),
        "content_hash": note.content_hash,
        "note_content_hash": note.content_hash,
        "content_hash_contract": FRESHNESS_CONTRACT_ID,
        "format": "full",
        "content": wrapped,
        "estimated_tokens": _estimate_tokens(retrieval_content),
        "returned_estimated_tokens": _estimate_tokens(content),
        "offset": start,
        "limit_applied": limit_applied,
        "total_chars": total_chars,
        "returned_chars": len(content),
        "next_offset": next_offset,
        "truncated": truncated,
    }


def _build_chunk_payload(
    app: DatacronApp,
    note: Note,
    chunk: Chunk,
    *,
    prev_chunk_id: str | None,
    next_chunk_id: str | None,
) -> dict[str, Any]:
    return {
        "format": "chunk",
        "chunk_id": _redact_retrieval_text(app, chunk.chunk_id),
        "note_id": chunk.note_id,
        "rel_path": _redact_retrieval_text(app, chunk.note_rel_path),
        "title": _sanitize_retrieval_metadata(app, protect_note_title(app, note)),
        "header_path": _sanitize_retrieval_metadata(app, chunk.header_path),
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "content": wrap_vault_content(
            _redact_retrieval_text(app, chunk.note_rel_path),
            app.secret_redactor.redact_fragment(
                chunk.content, note.raw_content, chunk.line_start, chunk.line_end
            )
            if app.secret_redactor.retrieval_enabled(app.settings)
            else chunk.content,
        ),
        "content_hash": note.content_hash,
        "note_content_hash": note.content_hash,
        "chunk_content_hash": chunk.content_hash,
        "content_hash_contract": FRESHNESS_CONTRACT_ID,
        "estimated_tokens": chunk.token_count,
        "prev_chunk_id": (
            _redact_retrieval_text(app, prev_chunk_id) if prev_chunk_id is not None else None
        ),
        "next_chunk_id": (
            _redact_retrieval_text(app, next_chunk_id) if next_chunk_id is not None else None
        ),
    }


def _build_map_payload(app: DatacronApp, note: Note) -> dict[str, Any]:
    # One heading parse serves the map, the chunk protection and the title protection.
    selected_headings = markdown_headings(note.content.splitlines(keepends=True))
    chunks = protect_chunk_metadata(app, note, app.chunker.chunk(note), headings=selected_headings)
    headings: list[dict[str, Any]] = []
    line_offset = content_line_offset(note)
    for chunk in chunks:
        if chunk.chunk_type is not ChunkType.HEADING:
            continue
        selected = next(
            item
            for item in reversed(selected_headings)
            if item.start < chunk.line_start - line_offset
        )
        level = selected.level
        headings.append(
            {
                "level": level,
                "text": _sanitize_retrieval_metadata(
                    app,
                    chunk.section_title or chunk.content.lstrip("# ").strip(),
                ),
                "path": _sanitize_retrieval_metadata(app, chunk.header_path),
                "chunk_id": _redact_retrieval_text(app, chunk.chunk_id),
            }
        )
    return {
        "id": note.id,
        "rel_path": _redact_retrieval_text(app, note.rel_path),
        "title": _sanitize_retrieval_metadata(
            app, protect_note_title(app, note, headings=selected_headings)
        ),
        "content_hash": note.content_hash,
        "note_content_hash": note.content_hash,
        "content_hash_contract": FRESHNESS_CONTRACT_ID,
        "format": "map",
        "headings": headings,
        "chunk_count": len(chunks),
    }
