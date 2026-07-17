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
"""Tests for explicit temporal retrieval re-ranking."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from datacron.core.models import Chunk, Note, SearchResult
from datacron.core.temporal import TemporalMeta, rerank_temporal

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_CURRENT_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_OLD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_LOW_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX7"
_OTHER_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX8"
_INVALID_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX9"


def _result(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
    *,
    note_id: str,
    score: float,
    rel_path: str | None = None,
    tier: int = 0,
) -> SearchResult:
    note = note_factory(
        id=note_id,
        rel_path=rel_path or f"{note_id}.md",
        title=note_id,
    )
    chunk = chunk_factory(
        note=note,
        chunk_id=f"{note_id}::::0000",
        content="temporal signal",
    )
    return SearchResult(chunk=chunk, score=score, snippet="temporal signal", tier=tier)


def _note_ids(results: list[SearchResult]) -> list[str]:
    return [result.chunk.note_id for result in results]


def test_empty_meta_preserves_order_strictly(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_CURRENT_ID, score=0.4),
        _result(note_factory, chunk_factory, note_id=_OLD_ID, score=0.8),
    ]

    assert rerank_temporal(results, {}, include_superseded=False) == results


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    scores=st.lists(
        st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=20,
    ),
    tiers=st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=20),
)
def test_no_effective_temporal_signal_preserves_arbitrary_store_order(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
    scores: list[float],
    tiers: list[int],
) -> None:
    size = min(len(scores), len(tiers))
    results = [
        _result(
            note_factory,
            chunk_factory,
            note_id=f"01HQXR7K9YZ8M2N3PQRSTVW{i:03d}",
            score=scores[i],
            tier=tiers[i],
        )
        for i in range(size)
    ]
    meta = {
        result.chunk.note_id: TemporalMeta(confidence="high", supersedes=[]) for result in results
    }

    assert rerank_temporal(results, meta, include_superseded=False) == results


def test_confidence_penalty_stays_within_retrieval_tier(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    tier_zero_low = _result(
        note_factory,
        chunk_factory,
        note_id=_LOW_ID,
        score=0.1,
        tier=0,
    )
    tier_one_high = _result(
        note_factory,
        chunk_factory,
        note_id=_CURRENT_ID,
        score=100.0,
        tier=1,
    )
    meta = {
        _LOW_ID: TemporalMeta(confidence="low", supersedes=[]),
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(
        [tier_zero_low, tier_one_high],
        meta,
        include_superseded=False,
    )

    assert _note_ids(reranked) == [_LOW_ID, _CURRENT_ID]


def test_superseded_bucket_sinks_below_all_retrieval_tiers(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    superseded_and = _result(
        note_factory,
        chunk_factory,
        note_id=_OLD_ID,
        score=100.0,
        tier=0,
    )
    current_or = _result(
        note_factory,
        chunk_factory,
        note_id=_CURRENT_ID,
        score=0.1,
        tier=1,
    )
    meta = {
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[_OLD_ID]),
        _OLD_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(
        [superseded_and, current_or],
        meta,
        include_superseded=False,
    )

    assert _note_ids(reranked) == [_CURRENT_ID, _OLD_ID]


def test_needs_verification_recedes_below_high_confidence_result(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_LOW_ID, score=1.0),
        _result(note_factory, chunk_factory, note_id=_CURRENT_ID, score=0.8),
    ]
    meta = {
        _LOW_ID: TemporalMeta(confidence="needs_verification", supersedes=[]),
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(results, meta, include_superseded=False)

    assert _note_ids(reranked) == [_CURRENT_ID, _LOW_ID]
    assert reranked[1].score == pytest.approx(0.5)


def test_superseded_note_is_demoted_without_being_removed(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_OLD_ID, score=1.0),
        _result(note_factory, chunk_factory, note_id=_CURRENT_ID, score=0.2),
    ]
    meta = {
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[_OLD_ID]),
        _OLD_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(results, meta, include_superseded=False)

    assert _note_ids(reranked) == [_CURRENT_ID, _OLD_ID]
    assert reranked[1].score == pytest.approx(0.1)


def test_invalidated_note_sinks_below_all_retrieval_tiers(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    invalidated_and = _result(
        note_factory,
        chunk_factory,
        note_id=_INVALID_ID,
        score=100.0,
        tier=0,
    )
    active_or = _result(
        note_factory,
        chunk_factory,
        note_id=_CURRENT_ID,
        score=0.1,
        tier=1,
    )
    meta = {
        _INVALID_ID: TemporalMeta(
            confidence="high",
            supersedes=[],
            invalid_at="2026-07-17T08:30:00+00:00",
            invalidated_by=_CURRENT_ID,
        ),
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(
        [invalidated_and, active_or],
        meta,
        include_superseded=False,
    )

    assert _note_ids(reranked) == [_CURRENT_ID, _INVALID_ID]
    assert reranked[1].score == pytest.approx(10.0)


def test_include_superseded_disables_supersedes_demotion_only(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_OLD_ID, score=1.0),
        _result(note_factory, chunk_factory, note_id=_CURRENT_ID, score=0.8),
    ]
    meta = {
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[_OLD_ID]),
        _OLD_ID: TemporalMeta(confidence="low", supersedes=[]),
    }

    reranked = rerank_temporal(results, meta, include_superseded=True)

    assert _note_ids(reranked) == [_CURRENT_ID, _OLD_ID]
    assert reranked[1].score == pytest.approx(0.7)


def test_include_superseded_also_disables_invalidation_demotion(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_INVALID_ID, score=1.0),
        _result(note_factory, chunk_factory, note_id=_CURRENT_ID, score=0.8),
    ]
    meta = {
        _INVALID_ID: TemporalMeta(
            confidence="low",
            supersedes=[],
            invalid_at="2026-07-17T08:30:00+00:00",
        ),
        _CURRENT_ID: TemporalMeta(confidence="high", supersedes=[]),
    }

    reranked = rerank_temporal(results, meta, include_superseded=True)

    assert _note_ids(reranked) == [_CURRENT_ID, _INVALID_ID]
    assert reranked[1].score == pytest.approx(0.7)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    score=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    tier=st.integers(min_value=0, max_value=1),
)
def test_absent_bitemporal_fields_preserve_legacy_behavior(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
    score: float,
    tier: int,
) -> None:
    results = [
        _result(
            note_factory,
            chunk_factory,
            note_id=_CURRENT_ID,
            score=score,
            tier=tier,
        )
    ]
    legacy = {_CURRENT_ID: TemporalMeta(confidence="high", supersedes=[])}
    explicit_absence = {
        _CURRENT_ID: TemporalMeta(
            confidence="high",
            supersedes=[],
            valid_from=None,
            invalid_at=None,
            invalidated_by=None,
        )
    }

    assert rerank_temporal(results, legacy, include_superseded=False) == rerank_temporal(
        results, explicit_absence, include_superseded=False
    )


def test_equal_adjusted_scores_keep_bm25_order(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    results = [
        _result(note_factory, chunk_factory, note_id=_LOW_ID, score=1.0),
        _result(note_factory, chunk_factory, note_id=_OTHER_ID, score=1.0),
    ]
    meta = {
        _LOW_ID: TemporalMeta(confidence="low", supersedes=[]),
        _OTHER_ID: TemporalMeta(confidence="low", supersedes=[]),
    }

    reranked = rerank_temporal(results, meta, include_superseded=False)

    assert _note_ids(reranked) == [_LOW_ID, _OTHER_ID]
