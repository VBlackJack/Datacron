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
"""Full-text, regex, and backlink tool implementations."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Final

from datacron.core.config import TEMPORAL_OVERFETCH_FACTOR
from datacron.core.models import SearchResult
from datacron.core.temporal import rerank_temporal
from datacron.indexing.reconcile import ReconcileStats, reconcile
from datacron.indexing.ripgrep import RegexFallbackError, RipgrepError
from datacron.mcp.sandbox import (
    wrap_vault_content,
)
from datacron.mcp.tools.payloads import (
    _LOGGER,
    _audit,
    _bounded_count,
    _error_response,
    _redact_retrieval_text,
    _sanitize_optional_retrieval_metadata,
    _sanitize_retrieval_metadata,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


async def _search_text_impl(
    app: DatacronApp,
    *,
    query: str,
    limit: int,
    include_superseded: bool = False,
    include_timings: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    timings_ms: dict[str, float] = {}
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
        stage_started = time.perf_counter()
        repair = await _repair_index_on_read(app)
        timings_ms["repair"] = _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        raw_results = await app.store.search(
            cleaned,
            limit=bounded_limit * TEMPORAL_OVERFETCH_FACTOR,
        )
        timings_ms["fts"] = _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        temporal_meta = await app.store.list_temporal_metadata()
        timings_ms["temporal_metadata"] = _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        raw_results = [
            result
            for result in raw_results
            if app.scope.allows_rel_path(result.chunk.note_rel_path, "read")
        ]
        raw_results = rerank_temporal(
            raw_results,
            temporal_meta,
            include_superseded=include_superseded,
        )[:bounded_limit]
        timings_ms["rerank"] = _elapsed_ms(stage_started)
    except Exception:
        _LOGGER.exception("search_text failed (query=%r)", query)
        return _error_response("search_text", RuntimeError("internal error"), started, query=query)

    stage_started = time.perf_counter()
    results, truncated_for_tokens = _apply_token_budget(
        raw_results, max_tokens=app.settings.max_result_tokens
    )
    timings_ms["budget"] = _elapsed_ms(stage_started)

    stage_started = time.perf_counter()
    payload: dict[str, Any] = {
        "query": _redact_retrieval_text(app, cleaned),
        "results": [_search_result_summary(app, result) for result in results],
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    if repair["reindexed_notes"] or repair["deleted_notes"]:
        payload["index_repair"] = repair
    timings_ms["serialization"] = _elapsed_ms(stage_started)
    if include_timings:
        payload["timings_ms"] = timings_ms
    _LOGGER.debug(
        "search_text stage timings repair_ms=%.3f fts_ms=%.3f temporal_metadata_ms=%.3f "
        "rerank_ms=%.3f budget_ms=%.3f serialization_ms=%.3f",
        timings_ms["repair"],
        timings_ms["fts"],
        timings_ms["temporal_metadata"],
        timings_ms["rerank"],
        timings_ms["budget"],
        timings_ms["serialization"],
    )
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


def _elapsed_ms(started: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.perf_counter() - started) * 1000.0


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
        search_root = app.scope.authorize_path(app.vault_root, "read")
        raw_results = await app.ripgrep.search(
            pattern=pattern,
            vault_root=search_root,
            glob=glob,
            limit=bounded_limit,
            store=app.store,
            rg_path=app.settings.ripgrep_path,
            fallback_max_pattern_length=app.settings.regex_fallback_max_pattern_length,
            fallback_timeout_seconds=app.settings.regex_fallback_timeout_seconds,
        )
        raw_results = [
            result
            for result in raw_results
            if app.scope.allows_rel_path(result.chunk.note_rel_path, "read")
        ]
    except (FileNotFoundError, RegexFallbackError) as exc:
        mapped_exc = ValueError(str(exc)) if isinstance(exc, RegexFallbackError) else exc
        return _error_response("search_regex", mapped_exc, started, pattern=pattern, glob=glob)
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
        "pattern": _redact_retrieval_text(app, pattern),
        "glob": _sanitize_optional_retrieval_metadata(app, glob),
        "results": [_search_result_summary(app, result) for result in results],
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
            "target": _redact_retrieval_text(app, cleaned),
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
        "target": _redact_retrieval_text(app, cleaned),
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


def _search_result_summary(app: DatacronApp, result: SearchResult) -> dict[str, Any]:
    chunk = result.chunk
    returned_rel_path = _redact_retrieval_text(app, chunk.note_rel_path)
    wrapped_snippet = wrap_vault_content(
        returned_rel_path,
        _redact_retrieval_text(app, result.snippet),
    )
    return {
        "chunk_id": _redact_retrieval_text(app, chunk.chunk_id),
        "note_id": chunk.note_id,
        "note_rel_path": returned_rel_path,
        "header_path": _sanitize_retrieval_metadata(app, chunk.header_path),
        "section_title": _sanitize_optional_retrieval_metadata(app, chunk.section_title),
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
    enabled when the configured minimum interval has elapsed. Between sweeps,
    reads serve the current index. ``content_hash`` remains the authority on
    any note whose mtime moved.
    """
    async with app.reconcile_lock:
        now = _repair_clock()
        last_sweep = app.repair_state.last_sweep_completed_at
        interval = app.settings.repair_min_interval_seconds
        if interval > 0.0 and last_sweep is not None and now - last_sweep < interval:
            return _throttled_repair_stats()

        if not app.write_policy.writes_allowed:
            indexed = await app.store.list_indexed_notes_with_mtime()
            live = await app.vault_reader.stat_notes()
            stats: ReconcileStats = {
                "checked_notes": len(live),
                "indexed_notes_before": len(indexed),
                "reindexed_notes": 0,
                "deleted_notes": 0,
                "skipped_notes": len(live),
            }
        else:
            stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        app.repair_state.last_sweep_completed_at = _repair_clock()

    await _invalidate_alias_cache_if_index_changed(app, stats)
    return stats


async def _reconcile_serialized(app: DatacronApp) -> ReconcileStats:
    """Serialize a write-triggered reconcile and reset the repair interval."""
    async with app.reconcile_lock:
        stats = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        app.repair_state.last_sweep_completed_at = _repair_clock()
        return stats


def _repair_clock() -> float:
    """Return a monotonic timestamp, split out for deterministic tests."""
    return time.monotonic()


def _throttled_repair_stats() -> ReconcileStats:
    """Return the no-op outcome used when the repair sweep is throttled."""
    return {
        "checked_notes": 0,
        "indexed_notes_before": 0,
        "reindexed_notes": 0,
        "deleted_notes": 0,
        "skipped_notes": 0,
    }


async def _invalidate_alias_cache_if_index_changed(app: DatacronApp, stats: ReconcileStats) -> None:
    if stats["reindexed_notes"] or stats["deleted_notes"]:
        await app.vault_reader.invalidate_alias_cache()


async def _resolve_backlink_target(app: DatacronApp, target: str) -> str | None:
    """Return a note_id from a ULID or an alias, or None if unresolved."""
    if _ULID_PATTERN.match(target):
        # Keep the caller-supplied stable ID even after target deletion so
        # scoped source chunks can still expose broken-backlink evidence.
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
        if not app.scope.allows_rel_path(chunk.note_rel_path, "read"):
            continue
        if chunk.note_id == target_note_id:
            continue
        if chunk.chunk_id in seen_chunk_ids:
            continue
        if not await _chunk_links_to(app, chunk.wikilinks_out, target_note_id, alias_cache):
            continue
        seen_chunk_ids.add(chunk.chunk_id)
        sources.append(
            {
                "source_chunk_id": _redact_retrieval_text(app, chunk.chunk_id),
                "source_note_id": chunk.note_id,
                "source_note_rel_path": _redact_retrieval_text(app, chunk.note_rel_path),
                "header_path": _sanitize_retrieval_metadata(app, chunk.header_path),
                "section_title": _sanitize_optional_retrieval_metadata(app, chunk.section_title),
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
