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

from datacron.core.models import Chunk, Note, SearchResult
from datacron.core.temporal import TemporalMeta, rerank_temporal

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_CURRENT_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_OLD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_LOW_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX7"
_OTHER_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX8"


def _result(
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
    *,
    note_id: str,
    score: float,
    rel_path: str | None = None,
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
    return SearchResult(chunk=chunk, score=score, snippet="temporal signal")


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
