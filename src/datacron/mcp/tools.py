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
"""MCP read-only tools: ``list_notes`` and ``get_note``.

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
from typing import TYPE_CHECKING, Any, Final

from mcp.server.fastmcp import FastMCP

from datacron.core.logger import get_logger
from datacron.core.models import Chunk, ChunkType, Note, SearchResult
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.mcp.sandbox import wrap_vault_content

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["GetNoteFormat", "register_tools"]

_LOGGER = get_logger(__name__)

GetNoteFormat = str  # "full" | "map" — kept loose for FastMCP schema
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")


def register_tools(server: FastMCP[Any], app: Any) -> None:
    """Attach the Sem-2 tools to ``server``.

    ``app`` is the :class:`DatacronApp` bundle; typed loosely to avoid a
    circular import with :mod:`datacron.mcp.server`.
    """

    @server.tool(
        name="list_notes",
        title="List notes",
        description=(
            "Return a paginated list of notes in the vault, optionally scoped to a "
            "subfolder and/or filtered by tags. Each entry includes the stable ULID, "
            "title, tags, aliases, and timestamps."
        ),
    )
    async def list_notes(
        folder: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await _list_notes_impl(app, folder=folder, tags=tags, limit=limit)

    @server.tool(
        name="get_note",
        title="Get a note",
        description=(
            "Fetch a single note by its ULID or vault-relative path. format='full' "
            "returns the sandbox-wrapped body; format='map' returns the heading "
            "outline only (cheap to scan before requesting full content)."
        ),
    )
    async def get_note(
        id_or_path: str,
        format: GetNoteFormat = "full",
    ) -> dict[str, Any]:
        return await _get_note_impl(app, id_or_path=id_or_path, fmt=format)

    @server.tool(
        name="search_text",
        title="Search text (BM25)",
        description=(
            "Full-text BM25 search over the FTS5 index. Returns ranked sandbox-"
            "wrapped snippets with **term** highlighting. Requires `datacron index` "
            "to have been run first."
        ),
    )
    async def search_text(query: str, limit: int = 20) -> dict[str, Any]:
        return await _search_text_impl(app, query=query, limit=limit)

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
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
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
    returned = filtered[:bounded_limit]
    payload = {
        "notes": [_note_summary(note) for note in returned],
        "total": total,
        "returned": len(returned),
        "truncated": total > len(returned),
        "limit_applied": bounded_limit,
    }
    _audit(
        "list_notes",
        started,
        folder=folder,
        tags=tags,
        limit=limit,
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
) -> dict[str, Any]:
    started = time.perf_counter()
    if fmt not in _VALID_FORMATS:
        return _error_response(
            "get_note",
            ValueError(f"format must be one of {sorted(_VALID_FORMATS)}"),
            started,
            id_or_path=id_or_path,
            fmt=fmt,
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

    payload = _build_map_payload(app, note) if fmt == "map" else _build_full_payload(app, note)

    _audit(
        "get_note",
        started,
        id_or_path=id_or_path,
        fmt=fmt,
        note_id=note.id,
        note_rel_path=note.rel_path,
        truncated=bool(payload.get("truncated", False)),
    )
    return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bounded_count(requested: int, ceiling: int) -> int:
    if requested <= 0:
        return ceiling
    return min(requested, ceiling)


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
    if _ULID_PATTERN.match(id_or_path):
        for note in await app.vault_reader.list_notes():
            if note.id == id_or_path:
                return note
        return None
    candidate = (app.vault_root / id_or_path).expanduser()
    resolved = assert_within_paths(candidate, [app.vault_root], kind="read")
    return await app.vault_reader.read_note(resolved)


def _build_full_payload(app: DatacronApp, note: Note) -> dict[str, Any]:
    max_tokens = app.settings.max_result_tokens
    estimated_tokens = max(1, len(note.content) // 4)
    content = note.content
    truncated = False
    if estimated_tokens > max_tokens:
        # Truncate at the character boundary corresponding to max_tokens (* 4
        # is the same heuristic used for token_count). The model still sees a
        # complete envelope but the content field carries truncated=True.
        char_budget = max_tokens * 4
        content = note.content[:char_budget]
        truncated = True

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
        "estimated_tokens": min(estimated_tokens, max_tokens),
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
        raw_results = await app.store.search(cleaned, limit=bounded_limit)
    except Exception:
        _LOGGER.exception("search_text failed (query=%r)", query)
        return _error_response("search_text", RuntimeError("internal error"), started, query=query)

    results, truncated_for_tokens = _apply_token_budget(
        raw_results, max_tokens=app.settings.max_result_tokens
    )
    payload = {
        "query": cleaned,
        "results": [_search_result_summary(r) for r in results],
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    _audit(
        "search_text",
        started,
        query=cleaned,
        limit=limit,
        bounded_limit=bounded_limit,
        returned=len(results),
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
        raw_results = await app.ripgrep.search(
            pattern=pattern,
            vault_root=app.vault_root,
            glob=glob,
            limit=bounded_limit,
            store=app.store,
        )
    except FileNotFoundError as exc:
        return _error_response("search_regex", exc, started, pattern=pattern, glob=glob)
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
    payload = {
        "pattern": pattern,
        "glob": glob,
        "results": [_search_result_summary(r) for r in results],
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    _audit(
        "search_regex",
        started,
        pattern=pattern,
        glob=glob,
        limit=limit,
        bounded_limit=bounded_limit,
        returned=len(results),
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

    payload = {
        "target": cleaned,
        "resolved_note_id": resolved_id,
        "results": sources,
        "returned": len(sources),
        "limit_applied": bounded_limit,
    }
    _audit(
        "get_backlinks",
        started,
        target=cleaned,
        resolved_note_id=resolved_id,
        returned=len(sources),
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
    """Walk every note's chunks, extract wikilinks, return source chunks pointing at the target.

    A chunk is considered a backlink source if any of its extracted wikilinks
    resolves (via :meth:`VaultReader.resolve_alias`) to ``target_note_id``.
    A small alias-resolution cache amortizes the per-link cost when multiple
    chunks reference the same target string.
    """
    notes = await app.vault_reader.list_notes()
    target_alias_lower = target_alias.strip().lower()
    alias_cache: dict[str, str | None] = {target_alias_lower: target_note_id}
    seen_chunk_ids: set[str] = set()
    sources: list[dict[str, Any]] = []

    for note in notes:
        if note.id == target_note_id:
            continue
        chunks: list[Chunk] = await app.store.list_chunks_for_note(note.id)
        for chunk in chunks:
            if chunk.chunk_id in seen_chunk_ids:
                continue
            wikilinks = app.wikilinks.extract(chunk)
            if not wikilinks:
                continue
            if not await _chunk_links_to(app, wikilinks, target_note_id, alias_cache):
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
    wikilinks: list[Any],
    target_note_id: str,
    alias_cache: dict[str, str | None],
) -> bool:
    """Return True if any wikilink in ``wikilinks`` resolves to ``target_note_id``.

    Wikilinks always carry ``resolved_note_id=None`` per the extractor's
    contract; resolution happens here via :meth:`VaultReader.resolve_alias`,
    cached across the scan.
    """
    for link in wikilinks:
        key = link.target_alias.strip().lower()
        if not key:
            continue
        if key not in alias_cache:
            alias_cache[key] = await app.vault_reader.resolve_alias(link.target_alias)
        if alias_cache[key] == target_note_id:
            return True
    return False
