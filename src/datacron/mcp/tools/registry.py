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
"""FastMCP registration for the Datacron tool surface."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES
from datacron.mcp.tools.advisory import _contradiction_scan_impl
from datacron.mcp.tools.ops import _audit_query_impl, _get_health_impl, _get_note_history_impl
from datacron.mcp.tools.read import _get_note_impl, _list_notes_impl
from datacron.mcp.tools.search import _get_backlinks_impl, _search_regex_impl, _search_text_impl
from datacron.mcp.tools.write import (
    _append_journal_impl,
    _create_note_ai_impl,
    _patch_note_section_impl,
    _revert_note_impl,
    _set_frontmatter_impl,
)

GetNoteFormat = str  # "full" | "map" -- kept loose for FastMCP schema


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
            "chunk_id inputs return format='chunk' with the sandbox-wrapped chunk body; "
            "a parent-hash mismatch returns an explicit stale-chunk error. Chunk reads "
            "ignore offset/limit. For note inputs, format='full' returns the "
            "sandbox-wrapped body and offset/limit page large notes by character range; "
            "format='map' returns the heading outline only (cheap to scan before "
            "requesting full content)."
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
            "a note ULID or a wikilink alias (resolved via title -> filename -> "
            "aliases). Empty list if unresolved or no incoming links."
        ),
    )
    async def get_backlinks(target: str, limit: int = 20) -> dict[str, Any]:
        return await _get_backlinks_impl(app, target=target, limit=limit)

    @server.tool(
        name="contradiction_scan",
        title="Scan frozen contradiction candidates",
        description=(
            "Return a cache-only advisory report over the frozen contradiction pool. "
            "The report is not validated on real content (0/4), judge confidence is "
            "uncalibrated, and its candidates must never block writes, merges, health, or CI."
        ),
    )
    async def contradiction_scan() -> dict[str, Any]:
        return await _contradiction_scan_impl()

    @server.tool(
        name="get_health",
        title="Get operational health",
        description=(
            "Return truthful read-only health for index freshness, vault integrity, "
            "point-in-time checksum, durability capability, and invariant evidence."
        ),
    )
    async def get_health() -> dict[str, Any]:
        return await _get_health_impl(app)

    @server.tool(
        name="create_note_ai",
        title="Create memory note",
        description=(
            "Write a new typed _memory Markdown note. This is a write operation: "
            "it is confined to DATACRON_WRITE_PATHS, never overwrites existing "
            "files, writes a durable operation record, and "
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
        ctx: Context[Any, Any, Any],
        supersedes: list[str] | None = None,
        last_verified: str | None = None,
        expected_hash: str | None = None,
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
            expected_hash=expected_hash,
            actor=app.identity_provider.identify(ctx).actor,
        )

    @server.tool(
        name="append_journal",
        title="Append to memory note",
        description=(
            "Append a Markdown entry under a heading in an existing memory note. "
            "This is a write operation: it is confined to DATACRON_WRITE_PATHS, "
            "stores content-addressed history, writes atomically, and "
            "relies on the MCP client's tool approval for human-in-the-loop review."
        ),
    )
    async def append_journal(
        rel_path: str,
        heading: str,
        entry: str,
        ctx: Context[Any, Any, Any],
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        return await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading=heading,
            entry=entry,
            expected_hash=expected_hash,
            actor=app.identity_provider.identify(ctx).actor,
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
        ctx: Context[Any, Any, Any],
        confidence: str | None = None,
        last_verified: str | None = None,
        supersedes: list[str] | None = None,
        origin: str | None = None,
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        return await _set_frontmatter_impl(
            app,
            rel_path=rel_path,
            confidence=confidence,
            last_verified=last_verified,
            supersedes=supersedes,
            origin=origin,
            expected_hash=expected_hash,
            actor=app.identity_provider.identify(ctx).actor,
        )

    @server.tool(
        name="patch_note_section",
        title="Patch note section",
        description=(
            "Replace the content under one existing Markdown heading. "
            "Pass the note's current content_hash as expected_hash for CAS. "
            "The operation preserves the heading line "
            "and non-target sections, stores exact prior history, and writes atomically."
        ),
    )
    async def patch_note_section(
        rel_path: str,
        heading: str,
        new_content: str,
        ctx: Context[Any, Any, Any],
        expected_hash: str | None = None,
        heading_level: int | None = None,
    ) -> dict[str, Any]:
        return await _patch_note_section_impl(
            app,
            rel_path=rel_path,
            heading=heading,
            new_content=new_content,
            expected_hash=expected_hash,
            heading_level=heading_level,
            actor=app.identity_provider.identify(ctx).actor,
        )

    @server.tool(
        name="revert_note",
        title="Revert note to exact history",
        description=(
            "Restore a note to exact content-addressed history bytes. Pass the current "
            "content_hash as expected_hash for CAS. The revert is itself durable, "
            "reversible, indexed, and operation-logged."
        ),
    )
    async def revert_note(
        note: str,
        to_hash: str,
        ctx: Context[Any, Any, Any],
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        return await _revert_note_impl(
            app,
            note=note,
            to_hash=to_hash,
            expected_hash=expected_hash,
            actor=app.identity_provider.identify(ctx).actor,
        )

    @server.tool(
        name="get_note_history",
        title="Get note operation history",
        description=(
            "List committed operation metadata for one note without reading history "
            "content or modifying the journal."
        ),
    )
    async def get_note_history(note: str, limit: int = 100) -> dict[str, Any]:
        return await _get_note_history_impl(app, note=note, limit=limit)

    @server.tool(
        name="audit_query",
        title="Query operation audit log",
        description=(
            "Query committed operation metadata by time range, tool, or note. "
            "This read-only operation never changes the journal or vault."
        ),
    )
    async def audit_query(
        start: str | None = None,
        end: str | None = None,
        tool: str | None = None,
        note: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await _audit_query_impl(
            app,
            start=start,
            end=end,
            tool=tool,
            note=note,
            limit=limit,
        )

    if app.settings.read_only:
        for tool_name in MUTATING_TOOL_NAMES:
            server.remove_tool(tool_name)
