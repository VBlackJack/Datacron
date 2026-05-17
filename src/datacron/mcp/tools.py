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
from datacron.core.models import ChunkType, Note
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
