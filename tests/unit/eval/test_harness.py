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
"""Tests for :mod:`datacron.eval.harness`."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from rich.console import Console

from datacron.core.models import Chunk, EvalQuestion, SearchResult
from datacron.core.protocols import FTS5Store, RipgrepWrapper
from datacron.eval.harness import LocalEvalHarness, load_eval_questions


class _FakeStore:
    """Search-only fake for the BM25 path exercised by LocalEvalHarness."""

    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        self.calls.append({"query": query, "limit": limit})
        return self._results[:limit]


class _UnusedRipgrep:
    """Stub that fails if the Phase 0 harness tries to use regex mode."""

    async def search(self, **_kwargs: Any) -> list[SearchResult]:
        raise AssertionError("LocalEvalHarness should not call ripgrep in Phase 0.")


def _silent_harness() -> LocalEvalHarness:
    return LocalEvalHarness(console=Console(file=StringIO(), width=120))


def _result(chunk: Chunk, score: float = 1.0) -> SearchResult:
    return SearchResult(chunk=chunk, score=score, snippet="snippet")


@pytest.mark.asyncio
async def test_run_computes_metrics_from_path_order(
    chunk_factory: Any,
) -> None:
    first = chunk_factory(note_rel_path="first.md", chunk_id="note::first::0000", token_count=7)
    second = chunk_factory(note_rel_path="second.md", chunk_id="note::second::0000", token_count=11)
    third = chunk_factory(note_rel_path="miss.md", chunk_id="note::miss::0000", token_count=13)
    store = _FakeStore([_result(first), _result(second), _result(third)])

    results = await _silent_harness().run(
        [
            EvalQuestion(
                id="q1",
                question="Where is second?",
                expected_paths=["second.md"],
            )
        ],
        cast("FTS5Store", store),
        cast("RipgrepWrapper", _UnusedRipgrep()),
        k_values=(1, 2, 3),
    )

    assert store.calls == [{"query": "Where is second?", "limit": 3}]
    assert len(results) == 1
    result = results[0]
    assert result.retrieved_chunk_ids == [
        "note::first::0000",
        "note::second::0000",
        "note::miss::0000",
    ]
    assert set(result.recall_at_k) == {1, 2, 3}
    assert result.recall_at_k[1] == 0.0
    assert result.recall_at_k[2] == 1.0
    assert result.recall_at_k[3] == 1.0
    assert result.citation_precision == pytest.approx(1 / 3)
    assert result.tokens_returned == 31
    assert result.latency_ms >= 0.0
    assert result.trust_label is None


@pytest.mark.asyncio
async def test_run_handles_empty_results(chunk_factory: Any) -> None:
    store = _FakeStore([])

    results = await _silent_harness().run(
        [
            EvalQuestion(
                id="q-empty",
                question="No hits",
                expected_paths=["expected.md"],
            )
        ],
        cast("FTS5Store", store),
        cast("RipgrepWrapper", _UnusedRipgrep()),
        k_values=(5, 10),
    )

    assert len(results) == 1
    result = results[0]
    assert result.retrieved_chunk_ids == []
    assert result.recall_at_k == {5: 0.0, 10: 0.0}
    assert result.citation_precision == 1.0
    assert result.tokens_returned == 0
    assert result.latency_ms >= 0.0
    assert store.calls == [{"query": "No hits", "limit": 10}]


def test_load_eval_questions_validates_yaml_list(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text(
        """
- id: q1
  question: What mentions Datacron?
  expected_paths:
    - docs/ARCHITECTURE.md
- id: q2
  question: What covers contracts?
  expected_paths:
    - docs/agent-briefs/01-contracts.md
""".lstrip(),
        encoding="utf-8",
    )

    questions = load_eval_questions(path)

    assert [question.id for question in questions] == ["q1", "q2"]
    assert questions[0].expected_paths == ["docs/ARCHITECTURE.md"]


def test_load_eval_questions_rejects_non_list_yaml(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text("questions: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a list"):
        load_eval_questions(path)
