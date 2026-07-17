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
"""Temporal retrieval policy for explicit memory lifecycle signals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from datacron.core.config import CONFIDENCE_PENALTY, SUPERSEDED_DEMOTION_FACTOR
from datacron.core.models import SearchResult

__all__ = ["TemporalMeta", "rerank_temporal"]


@dataclass(frozen=True)
class TemporalMeta:
    """Explicit lifecycle metadata stored for one indexed note."""

    confidence: str | None
    supersedes: list[str]


def rerank_temporal(
    results: list[SearchResult],
    meta: Mapping[str, TemporalMeta],
    *,
    include_superseded: bool,
) -> list[SearchResult]:
    """Return results sorted by explicit temporal confidence signals.

    BM25 scores are positive in Datacron (higher is better), so penalties are
    multiplicative factors below 1.0. The sort is stable: equivalent adjusted
    scores keep the original BM25 order.
    """
    if not results or not meta:
        return list(results)

    superseded_ids = {
        superseded_id
        for item in meta.values()
        for superseded_id in item.supersedes
        if superseded_id
    }
    adjusted: list[tuple[int, SearchResult]] = []
    signal_applied = False
    for result in results:
        demoted_bucket, factor = _temporal_adjustment(
            result.chunk.note_id,
            meta,
            superseded_ids=superseded_ids,
            include_superseded=include_superseded,
        )
        signal_applied = signal_applied or demoted_bucket > 0 or factor != 1.0
        adjusted_result = (
            result if factor == 1.0 else result.model_copy(update={"score": result.score * factor})
        )
        adjusted.append((demoted_bucket, adjusted_result))

    if not signal_applied:
        return list(results)

    ranked = sorted(
        adjusted,
        key=lambda item: (item[0], item[1].tier, -item[1].score),
    )
    return [result for _bucket, result in ranked]


def _temporal_adjustment(
    note_id: str,
    meta: Mapping[str, TemporalMeta],
    *,
    superseded_ids: set[str],
    include_superseded: bool,
) -> tuple[int, float]:
    item = meta.get(note_id)
    confidence = item.confidence.lower() if item and item.confidence else None
    factor = CONFIDENCE_PENALTY.get(confidence or "", 1.0)
    is_superseded = note_id in superseded_ids and not include_superseded
    if is_superseded:
        factor *= SUPERSEDED_DEMOTION_FACTOR
    return (int(is_superseded), factor)
