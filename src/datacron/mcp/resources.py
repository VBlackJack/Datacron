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
"""MCP resources: ``datacron://vault/map``, ``vault/info``, ``policy/active``.

Resources are pull-only references the client may load into context.
They are intentionally lightweight (the vault map targets ~2k tokens -
truncated if the vault is large enough to exceed
``DATACRON_MAX_RESULT_TOKENS``) so adding Datacron to a client never
explodes the context window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp.server.fastmcp import FastMCP

from datacron import __version__
from datacron.core.config import (
    INDEX_DB_FILENAME,
    TOKEN_ESTIMATE_CHARS_PER_TOKEN,
    VAULT_CONFIG_FILENAME,
)
from datacron.core.logger import get_logger
from datacron.core.models import Note
from datacron.core.paths import sidecar_dir, sidecar_index_dir
from datacron.mcp.sandbox import sanitize_metadata_value

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["register_resources"]

_LOGGER = get_logger(__name__)

URI_VAULT_MAP: Final[str] = "datacron://vault/map"
URI_VAULT_INFO: Final[str] = "datacron://vault/info"
URI_POLICY_ACTIVE: Final[str] = "datacron://policy/active"
_TRUNCATION_MARKER: Final[str] = "[...vault map truncated to fit token budget...]"
_VAULT_MAP_TAG_LIMIT: Final[int] = 5


def register_resources(server: FastMCP[DatacronApp], app: DatacronApp) -> None:
    """Attach the three Phase-0 resources to ``server``."""

    @server.resource(
        URI_VAULT_MAP,
        name="vault-map",
        title="Vault map",
        description="Folder/file tree with titles and tags. Lightweight (~2k tokens).",
        mime_type="text/markdown",
    )
    async def vault_map() -> str:
        return await _build_vault_map(app)

    @server.resource(
        URI_VAULT_INFO,
        name="vault-info",
        title="Vault info",
        description="Stats about the vault: path, note count, index freshness.",
        mime_type="application/json",
    )
    async def vault_info() -> str:
        return await _build_vault_info(app)

    @server.resource(
        URI_POLICY_ACTIVE,
        name="policy-active",
        title="Active policy",
        description=(
            "Currently active write/trust policy. Empty in Phase 0 (read-only); "
            "v0.2 will populate this once write tools land."
        ),
        mime_type="application/json",
    )
    async def policy_active() -> str:
        return _build_policy_active()


# ---------------------------------------------------------------------------
# Builders (module-level so they're testable without spinning a server).
# ---------------------------------------------------------------------------


async def _build_vault_map(app: DatacronApp) -> str:
    """Render the vault as a Markdown outline, truncating to the token budget."""
    try:
        notes = await app.vault_reader.list_notes()
    except (FileNotFoundError, ValueError):
        _LOGGER.exception("vault map: list_notes failed")
        return f"# {app.vault_root}\n\n_(vault unavailable)_\n"

    lines: list[str] = [f"# {app.vault_root.name or app.vault_root}"]
    grouped = _group_by_folder(notes)
    for folder in sorted(grouped):
        if folder:
            lines.append(f"\n## {_sanitize_retrieval_metadata(app, folder)}/")
        for note in grouped[folder]:
            lines.append(_format_note_line(app, note))

    rendered = "\n".join(lines)
    return _truncate_to_token_budget(rendered, app.settings.max_result_tokens)


async def _build_vault_info(app: DatacronApp) -> str:
    note_count, list_error = await _safe_note_count(app)
    sidecar = sidecar_dir(app.vault_root)
    index_db = sidecar_index_dir(app.vault_root) / INDEX_DB_FILENAME
    vault_config = sidecar / VAULT_CONFIG_FILENAME
    indexed_notes, indexed_chunks, last_indexed_at, stats_error = await _safe_store_stats(app)

    info: dict[str, Any] = {
        "datacron_version": __version__,
        "vault_root": str(app.vault_root),
        "vault_initialized": vault_config.is_file(),
        "vault_config": str(vault_config) if vault_config.is_file() else None,
        "note_count": note_count,
        "index": {
            # "built" reflects whether the FTS5 store holds indexed notes -
            # not just whether the file exists. Opening the store at server
            # startup creates an empty database; that's not yet "built".
            "built": indexed_notes > 0,
            "path": str(index_db),
            "size_bytes": index_db.stat().st_size if index_db.is_file() else 0,
            "indexed_notes": indexed_notes,
            "indexed_chunks": indexed_chunks,
            "last_indexed_at": last_indexed_at,
        },
        "limits": {
            "max_result_count": app.settings.max_result_count,
            "max_result_tokens": app.settings.max_result_tokens,
        },
    }
    if list_error is not None:
        info["list_error"] = list_error
    if stats_error is not None:
        info["index"]["stats_error"] = stats_error
    return json.dumps(info, indent=2, sort_keys=True)


def _build_policy_active() -> str:
    """Return the (currently empty) policy descriptor.

    See ADR-006 / decisions-tranchees-v2.1.md section 4.3: the L0-L5 trust
    engine is dormant in Phase 0 because no write tools exist yet. The
    descriptor still ships so MCP clients can render the placeholder UX.
    """
    policy: dict[str, Any] = {
        "version": "phase0",
        "mode": "read-only",
        "write_tools_enabled": False,
        "trust_categories": {
            "auto-create": [],
            "review-patch": [],
            "dangerous": [],
        },
        "write_paths": [],
        "active_policies": [],
        "notes": (
            "Phase 0 ships read-only tools only. Write tools and the L0-L5 "
            "trust engine arrive in v0.2; this resource will populate then."
        ),
    }
    return json.dumps(policy, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _safe_note_count(app: DatacronApp) -> tuple[int, str | None]:
    try:
        notes = await app.vault_reader.list_notes()
    except (FileNotFoundError, ValueError) as exc:
        _LOGGER.warning("vault info: list_notes failed: %s", exc)
        return 0, str(exc)
    return len(notes), None


async def _safe_store_stats(
    app: DatacronApp,
) -> tuple[int, int, str | None, str | None]:
    """Return ``(indexed_notes, indexed_chunks, last_indexed_at_iso, error)``.

    Any FTS5 error is captured and surfaced via the ``stats_error`` field on
    the resource payload - vault/info must remain queryable even when the
    index is broken so users can diagnose the problem.
    """
    try:
        stats = await app.store.stats()
    except Exception as exc:
        _LOGGER.warning("vault info: store.stats failed: %s", exc)
        return 0, 0, None, str(exc)
    last_indexed_at_iso = (
        stats.last_indexed_at.isoformat() if stats.last_indexed_at is not None else None
    )
    return stats.note_count, stats.chunk_count, last_indexed_at_iso, None


def _group_by_folder(notes: list[Note]) -> dict[str, list[Note]]:
    grouped: dict[str, list[Note]] = {}
    for note in notes:
        folder = str(Path(note.rel_path).parent)
        if folder == ".":
            folder = ""
        grouped.setdefault(folder, []).append(note)
    for items in grouped.values():
        items.sort(key=lambda n: n.rel_path)
    return grouped


def _format_note_line(app: DatacronApp, note: Note) -> str:
    filename = _sanitize_retrieval_metadata(app, Path(note.rel_path).name)
    title = _sanitize_retrieval_metadata(app, note.title.strip() or filename)
    important_marker = " *" if note.frontmatter.get("important") is True else ""
    tag_suffix = ""
    if note.tags:
        tags = [_sanitize_retrieval_metadata(app, tag) for tag in note.tags[:_VAULT_MAP_TAG_LIMIT]]
        tag_suffix = f"  [{', '.join(tags)}{', ...' if len(note.tags) > 5 else ''}]"
    return f"- `{filename}` - {title}{important_marker}{tag_suffix}"


def _sanitize_retrieval_metadata(app: DatacronApp, value: str) -> str:
    if app.secret_redactor.retrieval_enabled(app.settings):
        value = app.secret_redactor.redact_text(value)
    return sanitize_metadata_value(value)


def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
    """Truncate ``text`` so its ~token count stays under ``max_tokens``.

    Uses the same shared characters-per-token heuristic as the chunker; appends a visible
    marker so consumers know content was cut.
    """
    char_budget = max(
        0,
        max_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN - len(_TRUNCATION_MARKER),
    )
    if len(text) <= char_budget:
        return text
    safe_tail = "\n\n" + _TRUNCATION_MARKER + "\n"
    return text[: max(0, char_budget - len(safe_tail))] + safe_tail
