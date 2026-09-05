# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Parent-aware retrieval protection and rendered result budgets."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.models import Chunk, Note, SearchResult
from datacron.mcp.sandbox import VAULT_CONTENT_CLOSE

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp


async def protect_results(app: DatacronApp, results: list[SearchResult]) -> list[SearchResult]:
    """Load each admitted parent once; never redact stale chunks against new bytes."""
    if not results or not app.secret_redactor.retrieval_enabled(app.settings):
        return results
    parents: dict[str, Note] = {}
    live_chunks: dict[str, dict[str, Chunk]] = {}
    protected = []
    for result in results:
        chunk = result.chunk
        path = chunk.note_rel_path
        if path not in parents:
            parents[path] = await app.vault_reader.read_note(
                app.scope.authorize_note_rel_path(path)
            )
            live_chunks[path] = {item.chunk_id: item for item in app.chunker.chunk(parents[path])}
        note = parents[path]
        # Compare the actual returned chunk, not a later index metadata snapshot:
        # another request may have reindexed between search and this read. A
        # read-only index may still serve independently unchanged live chunks.
        if live_chunks[path].get(chunk.chunk_id) != chunk:
            raise ValueError("search source changed; refresh the index and retry")
        safe = app.secret_redactor.redact_fragment(
            chunk.content, note.raw_content, chunk.line_start, chunk.line_end
        )
        safe_result = result
        if safe != chunk.content:
            safe_result = result.model_copy(
                update={
                    "snippet": safe,
                    "redaction_source": safe,
                    "chunk": chunk.model_copy(update={"content": safe}),
                }
            )
        protected.append(safe_result)
    return protected


def bound_results(
    results: list[dict[str, Any]], max_tokens: int
) -> tuple[list[dict[str, Any]], bool]:
    """Bound serialized results, including escaping, metadata and sandbox envelopes.

    The outer tool envelope/query is not charged to the result budget. If even a
    result's metadata cannot fit, return fewer results with explicit truncation.
    """
    maximum = max_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN
    kept: list[dict[str, Any]] = []
    truncated = False
    for row in results:
        if _rendered_size([*kept, row]) <= maximum:
            kept.append(row)
            continue
        snippet = row["snippet"]
        # Split only the envelope we constructed, never untrusted delimiters.
        opening, notice, body = snippet.split("\n", 2)
        content = body.rsplit("\n", 1)[0]
        left, right = 0, len(content)
        best: dict[str, Any] | None = None
        while left <= right:
            length = (left + right) // 2
            candidate = dict(row)
            candidate["snippet"] = "\n".join(
                [opening, notice, _excerpt(content, length), VAULT_CONTENT_CLOSE]
            )
            if _rendered_size([*kept, candidate]) <= maximum:
                best = candidate
                left = length + 1
            else:
                right = length - 1
        if best is not None:
            kept.append(best)
        truncated = True
        break
    return kept, truncated


def _rendered_size(results: list[dict[str, Any]]) -> int:
    return len(json.dumps(results, ensure_ascii=True, indent=2))


def _excerpt(content: str, length: int) -> str:
    """Retain the first highlighted match, including a match at a long line's end."""
    match = content.find("**")
    start = max(0, match - length // 2) if match >= 0 else 0
    start = min(start, max(0, len(content) - length))
    return content[start : start + length]
