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
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Final

from datacron.core.config import (
    GROUPED_OVERFETCH_GROWTH,
    GROUPED_OVERFETCH_MAX_CHUNKS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_SEARCH_QUERY_TERMS,
    TEMPORAL_OVERFETCH_FACTOR,
)
from datacron.core.frontmatter import normalize_tag_filter
from datacron.core.models import Chunk, SearchResult
from datacron.core.paths import PathConfinementError
from datacron.core.scope import ScopedVaultReader
from datacron.core.temporal import rerank_temporal
from datacron.core.vault import DuplicateNoteIdentityError
from datacron.indexing.fts5_store import fts5_query_terms
from datacron.indexing.reconcile import ReconcileStats, reconcile
from datacron.indexing.ripgrep import (
    RegexFallbackError,
    RegexGlobError,
    RipgrepError,
    RipgrepOutputError,
    matches_vault_glob,
)
from datacron.mcp.sandbox import (
    wrap_vault_content,
)
from datacron.mcp.tools.payloads import (
    _LOGGER,
    _audit,
    _bounded_count,
    _error_response,
    _internal_error_response,
    _redact_retrieval_text,
    _sanitize_optional_retrieval_metadata,
    _sanitize_retrieval_metadata,
    _validate_frontmatter_filter,
)
from datacron.mcp.tools.retrieval import SearchIndexStaleError, bound_results, protect_results

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SEARCH_QUERY_TOO_LARGE_CODE: Final[str] = "search_query_too_large"


async def _search_text_impl(
    app: DatacronApp,
    *,
    query: str,
    limit: int,
    include_superseded: bool = False,
    include_timings: bool = False,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict[str, str] | None = None,
    group_by_note: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    timings_ms: dict[str, float] = {}
    cleaned = query.strip()
    query_error = _search_query_error(cleaned)
    if query_error is not None:
        # The query is not echoed: bounding it is the point of one of these refusals.
        return _error_response("search_text", query_error, started, query_chars=len(query))
    validation_error = _validate_frontmatter_filter(frontmatter)
    if validation_error is not None:
        exc, context = validation_error
        return _error_response("search_text", exc, started, query=query, **context)
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    try:
        scope_folder = _authorized_search_folder(app, folder)
    except (FileNotFoundError, ValueError, PathConfinementError) as exc:
        return _error_response("search_text", exc, started, query=query, folder=folder)
    filters = _search_filters(folder=scope_folder, tags=tags, frontmatter=frontmatter)
    try:
        raw_results, note_matches, repair = await _retrieve_ranked_results(
            app,
            query=cleaned,
            bounded_limit=bounded_limit,
            include_superseded=include_superseded,
            folder=scope_folder,
            tags=tags,
            frontmatter=frontmatter,
            group_by_note=group_by_note,
            timings_ms=timings_ms,
        )
    except (DuplicateNoteIdentityError, SearchIndexStaleError) as exc:
        return _error_response("search_text", exc, started, query=query)
    except Exception:
        return _internal_error_response("search_text", started, query=query)

    stage_started = time.perf_counter()
    results, truncated_for_tokens = bound_results(
        [
            _search_result_summary(app, result, note_matches=note_matches.get(result.chunk.note_id))
            for result in raw_results
        ],
        max_tokens=app.settings.max_result_tokens,
    )
    timings_ms["budget"] = _elapsed_ms(stage_started)

    stage_started = time.perf_counter()
    payload: dict[str, Any] = {
        "query": _redact_retrieval_text(app, cleaned),
        "results": results,
        "returned": len(results),
        "limit_applied": bounded_limit,
        "truncated_for_tokens": truncated_for_tokens,
    }
    if filters:
        payload["filters"] = filters
    if group_by_note:
        payload["grouped_by_note"] = True
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
        filters=filters or None,
        group_by_note=group_by_note or None,
        reindexed_notes=repair["reindexed_notes"],
        deleted_notes=repair["deleted_notes"],
        truncated_for_tokens=truncated_for_tokens,
    )
    return payload


class SearchQueryTooLargeError(ValueError):
    """A ``search_text`` query longer or with more terms than one search accepts."""

    code: Final[str] = SEARCH_QUERY_TOO_LARGE_CODE


def _search_query_error(query: str) -> ValueError | None:
    """Refuse an empty query, or one that would hold the index connection for seconds.

    Fifty thousand terms took 5.8 s inside SQLite, on the one connection every
    other tool call shares, and the echoed query then outgrew the result budget.
    """
    if not query:
        return ValueError("query must not be empty")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        return SearchQueryTooLargeError(
            f"query must be at most {MAX_SEARCH_QUERY_CHARS} characters"
        )
    if len(fts5_query_terms(query)) > MAX_SEARCH_QUERY_TERMS:
        return SearchQueryTooLargeError(f"query must hold at most {MAX_SEARCH_QUERY_TERMS} terms")
    return None


def _authorized_search_folder(app: DatacronApp, folder: str | None) -> str | None:
    """Confine ``folder`` to the vault and return its vault-relative POSIX form.

    Mirrors ``list_notes``: the vault root itself means no folder restriction.
    """
    if folder is None or not folder.strip():
        return None
    authorized = app.scope.authorize_rel_path(folder, "read")
    relative = authorized.relative_to(app.vault_root)
    return None if relative.name == "" else relative.as_posix()


async def _retrieve_ranked_results(
    app: DatacronApp,
    *,
    query: str,
    bounded_limit: int,
    include_superseded: bool,
    folder: str | None,
    tags: list[str] | None,
    frontmatter: dict[str, str] | None,
    group_by_note: bool,
    timings_ms: dict[str, float],
) -> tuple[list[SearchResult], dict[str, int], ReconcileStats]:
    """Repair, search, re-rank, optionally collapse by note, and redact, with timings."""
    stage_started = time.perf_counter()
    repair = await _repair_index_on_read(app)
    timings_ms["repair"] = _elapsed_ms(stage_started)

    stage_started = time.perf_counter()
    temporal_meta = await app.store.list_temporal_metadata()
    timings_ms["temporal_metadata"] = _elapsed_ms(stage_started)

    # The window is counted in chunks, and grouping collapses chunks into notes, so a
    # hub note with many matching sections could fill the whole window and leave the
    # caller one note where several matched, with nothing saying so. A grouped search
    # widens the window until it holds enough notes or the index has no more hits.
    fetch_limit = bounded_limit * TEMPORAL_OVERFETCH_FACTOR
    timings_ms["fts"] = 0.0
    timings_ms["rerank"] = 0.0
    while True:
        stage_started = time.perf_counter()
        hits = await app.store.search(
            query,
            limit=fetch_limit,
            folder=folder,
            tags=tags,
            frontmatter=frontmatter,
        )
        timings_ms["fts"] += _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        ranked = rerank_temporal(
            _filter_admitted_results(app, hits),
            temporal_meta,
            include_superseded=include_superseded,
        )
        if group_by_note:
            ranked = _collapse_by_note(ranked)
        timings_ms["rerank"] += _elapsed_ms(stage_started)
        if (
            not group_by_note
            or len(ranked) >= bounded_limit
            or len(hits) < fetch_limit
            or fetch_limit >= GROUPED_OVERFETCH_MAX_CHUNKS
        ):
            break
        fetch_limit = min(fetch_limit * GROUPED_OVERFETCH_GROWTH, GROUPED_OVERFETCH_MAX_CHUNKS)
    results = ranked[:bounded_limit]

    note_matches: dict[str, int] = {}
    if group_by_note and results:
        # Counting the ranked rows would only ever count the overfetch window, so a note
        # with more matching sections than that window holds would under-report itself.
        stage_started = time.perf_counter()
        note_matches = await app.store.count_matches_by_note(
            query,
            [result.chunk.note_id for result in results],
            folder=folder,
            tags=tags,
            frontmatter=frontmatter,
        )
        timings_ms["note_matches"] = _elapsed_ms(stage_started)

    return await protect_results(app, results), note_matches, repair


def _collapse_by_note(results: list[SearchResult]) -> list[SearchResult]:
    """Keep the best-ranked chunk of every note, preserving the order of notes.

    ``results`` is already ranked, so the first chunk seen for a note is its best one.
    The count of matching chunks is asked of the store separately, because this list is
    the truncated overfetch window and not the note's full set of matches.
    """
    collapsed: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        note_id = result.chunk.note_id
        if note_id in seen:
            continue
        seen.add(note_id)
        collapsed.append(result)
    return collapsed


def _search_filters(
    *,
    folder: str | None,
    tags: list[str] | None,
    frontmatter: dict[str, str] | None,
) -> dict[str, Any]:
    """Return the scope filters that actually narrow the search, for the payload."""
    filters: dict[str, Any] = {}
    if folder:
        filters["folder"] = folder
    required_tags = normalize_tag_filter(tags)
    if required_tags:
        filters["tags"] = required_tags
    if frontmatter:
        filters["frontmatter"] = dict(frontmatter)
    return filters


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
    # No Python pre-validation: ripgrep's dialect is not Python's, and refusing a
    # Unicode class such as \p{Lu} here refused a pattern the supported path runs.
    # Each path judges the pattern it runs: ripgrep's parse error comes back in its
    # stderr, and the indexed fallback compiles with Python before any index read.
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    try:
        repair = await _repair_index_on_read(app)
        search_root = app.scope.authorize_path(app.vault_root, "read")
        if glob is not None:
            matches_vault_glob("", glob)  # Validate even when the index is empty.
            indexed = await app.store.list_indexed_notes()
            if not any(
                matches_vault_glob(path, glob) and app.scope.allows_note_rel_path(path)
                for path in indexed
            ):
                raise RegexGlobError(
                    "regex_glob_no_files",
                    "Glob selects no admitted indexed note; check the file filter",
                )
        raw_results = await app.ripgrep.search(
            pattern=pattern,
            vault_root=search_root,
            glob=glob,
            limit=bounded_limit,
            store=app.store,
            rg_path=app.settings.ripgrep_path,
            fallback_max_pattern_length=app.settings.regex_fallback_max_pattern_length,
            fallback_timeout_seconds=app.settings.regex_fallback_timeout_seconds,
            admit=app.scope.allows_note_rel_path,
            max_frame_bytes=app.settings.regex_max_frame_bytes,
        )
        raw_results = _filter_admitted_results(app, raw_results)
        raw_results = await protect_results(app, raw_results)
    except (
        FileNotFoundError,
        RegexFallbackError,
        RegexGlobError,
        RipgrepOutputError,
        DuplicateNoteIdentityError,
        SearchIndexStaleError,
    ) as exc:
        mapped_exc = ValueError(str(exc)) if isinstance(exc, RegexFallbackError) else exc
        return _error_response("search_regex", mapped_exc, started, pattern=pattern, glob=glob)
    except RipgrepError as exc:
        # Only a run that produced nothing reaches here, so the pattern is the
        # likely cause; it is still not the only one, and the message says so
        # rather than asserting a rejection over a stderr that reads "Access is
        # denied". An agent told its regex was rejected rewrites the regex.
        message = exc.stderr.strip() or str(exc)
        return _error_response(
            "search_regex",
            ValueError(f"ripgrep returned no results and exited with an error: {message}"),
            started,
            pattern=pattern,
            glob=glob,
        )
    except Exception:
        return _internal_error_response("search_regex", started, pattern=pattern, glob=glob)

    results, truncated_for_tokens = bound_results(
        [_search_result_summary(app, result) for result in raw_results],
        max_tokens=app.settings.max_result_tokens,
    )
    payload: dict[str, Any] = {
        "pattern": _redact_retrieval_text(app, pattern),
        "glob": _sanitize_optional_retrieval_metadata(app, glob),
        "results": results,
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
        return _internal_error_response("get_backlinks", started, stage="resolution", target=target)

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
    except SearchIndexStaleError as exc:
        return _error_response(
            "get_backlinks", exc, started, target=target, resolved_note_id=resolved_id
        )
    except Exception:
        return _internal_error_response(
            "get_backlinks", started, stage="scan", target=target, resolved_note_id=resolved_id
        )

    payload: dict[str, Any] = {
        "target": _redact_retrieval_text(app, cleaned),
        "resolved_note_id": resolved_id,
        "results": sources,
        "returned": len(sources),
        "limit_applied": bounded_limit,
        # The scan stops the moment it has a full page, so a hub note referenced
        # more times than the cap returned exactly the cap with nothing to say the
        # rest existed. Every other listing on this surface carries a truncation
        # signal; this one could only mislead by omission, and a caller reading
        # `returned` as a count answered "twenty notes reference this".
        "truncated": len(sources) >= bounded_limit,
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


def _search_result_summary(
    app: DatacronApp,
    result: SearchResult,
    *,
    note_matches: int | None = None,
) -> dict[str, Any]:
    chunk = result.chunk
    returned_rel_path = _redact_retrieval_text(app, chunk.note_rel_path)
    source = result.redaction_source if result.redaction_source is not None else chunk.content
    redacted_source = _redact_retrieval_text(app, source)
    # Decorated/truncated snippets can hide the label or split a secret. When
    # source redaction changes text, render that safe source without decoration.
    redacted_chunk = _redact_retrieval_text(app, chunk.content)
    if redacted_chunk != chunk.content:
        snippet = redacted_chunk
    else:
        snippet = redacted_source if redacted_source != source else result.snippet
    wrapped_snippet = wrap_vault_content(
        returned_rel_path,
        _redact_retrieval_text(app, snippet),
    )
    summary: dict[str, Any] = {
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
    if note_matches is not None:
        summary["note_matches"] = note_matches
    if result.lifecycle is not None:
        summary["lifecycle"] = result.lifecycle
    return summary


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
        # A note a read found stale cannot heal while the sweep is throttled, and a
        # read-only server cannot heal it at all, so only a writable one skips the wait.
        stale = frozenset(app.repair_state.stale_note_paths)
        pending_stale = bool(stale) and app.write_policy.writes_allowed
        if (
            interval > 0.0
            and last_sweep is not None
            and now - last_sweep < interval
            and not pending_stale
        ):
            return _throttled_repair_stats()

        # One walk per sweep. Its keys are the paths this scope admits as live
        # notes, which is the answer list_notes used to buy a second time, once
        # per indexed note, right after this returns.
        live = await app.vault_reader.stat_notes()
        app.repair_state.live_note_paths = frozenset(live)
        if not app.write_policy.writes_allowed:
            indexed = await app.store.list_indexed_notes_with_mtime()
            stats: ReconcileStats = {
                "checked_notes": len(live),
                "indexed_notes_before": len(indexed),
                "reindexed_notes": 0,
                "deleted_notes": 0,
                "skipped_notes": len(live),
            }
        else:
            stats = await reconcile(
                app.store,
                app.vault_reader,
                app.chunker,
                mtime_gate=True,
                live=live,
                rechunk_paths=stale,
            )
            app.repair_state.stale_note_paths.difference_update(stale)
        app.repair_state.last_sweep_completed_at = _repair_clock()

    await _invalidate_alias_cache_if_index_changed(app, stats)
    return stats


async def _reconcile_note_serialized(
    app: DatacronApp, rel_path: str, content_hash: str
) -> ReconcileStats:
    """Refresh only a committed note; leave global freshness to read repair/health."""
    async with app.reconcile_lock:
        path = app.scope.authorize_note_rel_path(rel_path)
        note = await app.vault_reader.read_note(path)
        if note.content_hash != content_hash:
            raise ValueError("committed note changed before targeted index refresh")
        old_id = await app.store.get_note_id(rel_path)
        deleted = 0
        if old_id is not None and old_id != note.id:
            await app.store.delete_note(old_id)
            deleted = 1
        # Do not attach an mtime observed after the read: it could hide a concurrent edit.
        await app.store.upsert_note(note, app.chunker.chunk(note))
        await app.store.increment_generation()
        return {
            "checked_notes": 1,
            "indexed_notes_before": int(old_id is not None),
            "reindexed_notes": 1,
            "deleted_notes": deleted,
            "skipped_notes": 0,
        }


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
    resolve = _backlink_alias_resolver(app, target_note_id)
    admission_cache: dict[str, bool] = {}
    seen_chunk_ids: set[str] = set()
    matched_ids: list[str] = []

    # The scan reads four fields per candidate and keeps at most one page. Reading
    # whole chunks brought every chunk body in the vault into memory as a validated
    # model before the first candidate was examined, so stopping at the limit saved
    # nothing. The page is fetched whole afterwards, because the protection pass
    # compares each returned chunk against the note as it is on disk right now.
    async for source in app.store.iter_wikilink_sources():
        admitted = admission_cache.get(source.note_rel_path)
        if admitted is None:
            admitted = app.scope.allows_note_rel_path(source.note_rel_path)
            admission_cache[source.note_rel_path] = admitted
        if not admitted:
            continue
        if source.note_id == target_note_id:
            continue
        if source.chunk_id in seen_chunk_ids:
            continue
        if not await _chunk_links_to(resolve, source.wikilinks_out, target_note_id, alias_cache):
            continue
        seen_chunk_ids.add(source.chunk_id)
        matched_ids.append(source.chunk_id)
        if len(matched_ids) >= limit:
            break

    by_id = await app.store.chunks_by_ids(matched_ids)
    # A chunk can vanish between the scan and this fetch if another request reindexed
    # its note. Dropping it is right: the protection pass below would refuse it anyway.
    matched: list[Chunk] = [by_id[chunk_id] for chunk_id in matched_ids if chunk_id in by_id]
    # Protect every source in one pass: the parent note is read once per source note,
    # not once per matching chunk.
    protected = await protect_results(
        app, [SearchResult(chunk=chunk, score=0, snippet="") for chunk in matched]
    )
    sources: list[dict[str, Any]] = []
    for result in protected:
        safe_chunk = result.chunk
        sources.append(
            {
                "source_chunk_id": _redact_retrieval_text(app, safe_chunk.chunk_id),
                "source_note_id": safe_chunk.note_id,
                "source_note_rel_path": _redact_retrieval_text(app, safe_chunk.note_rel_path),
                "header_path": _sanitize_retrieval_metadata(app, safe_chunk.header_path),
                "section_title": _sanitize_optional_retrieval_metadata(
                    app, safe_chunk.section_title
                ),
            }
        )
    return sources


def _filter_admitted_results(
    app: DatacronApp,
    results: list[SearchResult],
) -> list[SearchResult]:
    """Filter search results with one live admission decision per note and call."""
    decisions: dict[str, bool] = {}
    admitted_results: list[SearchResult] = []
    for result in results:
        rel_path = result.chunk.note_rel_path
        admitted = decisions.get(rel_path)
        if admitted is None:
            admitted = app.scope.allows_note_rel_path(rel_path)
            decisions[rel_path] = admitted
        if admitted:
            admitted_results.append(result)
    return admitted_results


def _backlink_alias_resolver(
    app: DatacronApp, target_note_id: str
) -> Callable[[str], Awaitable[str | None]]:
    """Return an alias resolver that agrees with the scoped one on ``target_note_id``.

    The scan only asks whether an alias names the target. The scoped resolver
    admitted the note behind every distinct alias of the vault, a database lookup,
    a realpath and a stat each, to answer a question about one note: 5.9 s at 3000
    notes. Here an alias is resolved without admission and the target alone is
    admitted, once and only when some alias names it, so the answer is unchanged.
    """
    reader = app.vault_reader
    if not isinstance(reader, ScopedVaultReader):
        return reader.resolve_alias
    target_admitted: bool | None = None

    async def resolve(alias: str) -> str | None:
        nonlocal target_admitted
        resolved = await reader.resolve_alias_unscoped(alias)
        if resolved != target_note_id:
            return resolved
        if target_admitted is None:
            target_admitted = await reader.admits_note_id(target_note_id)
        return resolved if target_admitted else None

    return resolve


async def _chunk_links_to(
    resolve: Callable[[str], Awaitable[str | None]],
    wikilinks: Sequence[str],
    target_note_id: str,
    alias_cache: dict[str, str | None],
) -> bool:
    """Return True if any wikilink in ``wikilinks`` resolves to ``target_note_id``.

    Indexed wikilinks are raw target aliases; resolution happens here via
    ``resolve``, cached across the scan.
    """
    for target_alias in wikilinks:
        key = target_alias.strip().lower()
        if not key:
            continue
        if key not in alias_cache:
            alias_cache[key] = await resolve(target_alias)
        if alias_cache[key] == target_note_id:
            return True
    return False
