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
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console

from datacron.core.models import (
    Chunk,
    EvalPipeline,
    EvalQuestion,
    EvalTransport,
    SearchResult,
)
from datacron.eval.harness import LocalEvalHarness, load_eval_questions
from datacron.eval.metrics import payload_token_estimate
from datacron.mcp.server import DatacronApp


class _FakeStore:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        self.calls.append({"query": query, "limit": limit})
        return self._results[:limit]


class _FailingStore:
    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        _ = query, limit
        raise AssertionError("tool mode must not call the store branch directly")


def _silent_harness(**kwargs: Any) -> LocalEvalHarness:
    return LocalEvalHarness(console=Console(file=StringIO(), width=120), **kwargs)


def _app(store: object, *, max_result_count: int = 20) -> DatacronApp:
    settings = SimpleNamespace(max_result_count=max_result_count)
    return cast("DatacronApp", SimpleNamespace(store=store, settings=settings))


def _result(chunk: Chunk, score: float = 1.0) -> SearchResult:
    return SearchResult(chunk=chunk, score=score, snippet="snippet")


@pytest.mark.asyncio
async def test_tool_mode_calls_impl_and_never_direct_store_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "query": "Where is current?",
        "results": [
            {
                "chunk_id": "current::0000",
                "note_rel_path": "current.md",
                "snippet": '<vault_content path="current.md">current</vault_content>',
            },
            {
                "chunk_id": "current::0001",
                "note_rel_path": "current.md",
                "snippet": '<vault_content path="current.md">more</vault_content>',
            },
            {
                "chunk_id": "old::0000",
                "note_rel_path": "old.md",
                "snippet": '<vault_content path="old.md">old</vault_content>',
            },
        ],
        "returned": 3,
        "limit_applied": 20,
        "truncated_for_tokens": False,
        "timings_ms": {
            "repair": 10.0,
            "fts": 2.0,
            "temporal_metadata": 4.0,
            "rerank": 1.0,
            "budget": 0.5,
            "serialization": 0.25,
        },
    }
    calls: list[dict[str, Any]] = []

    async def fake_search_impl(
        app: DatacronApp,
        *,
        query: str,
        limit: int,
        include_superseded: bool = False,
        include_timings: bool = False,
    ) -> dict[str, Any]:
        _ = app
        calls.append(
            {
                "query": query,
                "limit": limit,
                "include_superseded": include_superseded,
                "include_timings": include_timings,
            }
        )
        return payload

    monkeypatch.setattr("datacron.eval.harness._search_text_impl", fake_search_impl)
    report = await _silent_harness().run(
        [
            EvalQuestion(
                id="q1",
                question="Where is current?",
                expected_paths=["current.md"],
                expected_chunk_ids=["current::0001"],
                forbidden_paths=["old.md"],
            )
        ],
        _app(_FailingStore()),
    )

    assert calls == [
        {
            "query": "Where is current?",
            "limit": 20,
            "include_superseded": False,
            "include_timings": True,
        }
    ]
    result = report.results[0]
    assert result.retrieved_paths == ["current.md", "old.md"]
    assert result.recall_at_k[5] == 1.0
    assert result.chunk_recall_at_k is not None
    assert result.chunk_recall_at_k[5] == 1.0
    assert result.reciprocal_rank == 1.0
    assert result.ndcg_at_10 == 1.0
    assert result.citation_precision == 0.5
    assert result.forbidden_violation is True
    assert result.tokens_returned == payload_token_estimate(payload)
    assert result.stage_timings_ms["repair"] == 10.0
    assert report.summary.forbidden_violation_rate == 1.0
    assert report.summary.stage_latency_ms["repair"].p50_ms == 10.0
    assert report.summary.stage_latency_ms["repair"].p95_ms == 10.0


@pytest.mark.asyncio
async def test_store_mode_computes_metrics_from_path_order(chunk_factory: Any) -> None:
    first = chunk_factory(note_rel_path="first.md", chunk_id="note::first::0000")
    second = chunk_factory(note_rel_path="second.md", chunk_id="note::second::0000")
    third = chunk_factory(note_rel_path="miss.md", chunk_id="note::miss::0000")
    store = _FakeStore([_result(first), _result(second), _result(third)])

    report = await _silent_harness().run(
        [
            EvalQuestion(
                id="q-store",
                question="Where is second?",
                expected_paths=["second.md"],
            )
        ],
        _app(store),
        k_values=(1, 2, 3),
        pipeline=EvalPipeline.STORE,
    )

    assert store.calls == [{"query": "Where is second?", "limit": 3}]
    result = report.results[0]
    assert result.recall_at_k == {1: 0.0, 2: 1.0, 3: 1.0}
    assert result.reciprocal_rank == 0.5
    assert result.citation_precision == pytest.approx(1 / 3)
    assert result.tokens_returned > 0
    assert report.summary.note_recall_at_k == {1: 0.0, 2: 1.0, 3: 1.0}
    assert report.summary.latency_p50_ms >= 0.0
    assert report.summary.latency_p95_ms >= 0.0


@pytest.mark.asyncio
async def test_e2e_callback_is_used_for_tool_transport() -> None:
    calls: list[tuple[str, int]] = []

    async def search(query: str, limit: int) -> dict[str, Any]:
        calls.append((query, limit))
        return {"query": query, "results": [], "returned": 0, "limit_applied": limit}

    report = await _silent_harness(tool_search=search).run(
        [EvalQuestion(id="q-empty", question="No hits", expected_paths=["expected.md"])],
        _app(_FailingStore()),
        k_values=(5, 10),
        transport=EvalTransport.E2E,
    )

    assert calls == [("No hits", 20)]
    assert report.results[0].recall_at_k == {5: 0.0, 10: 0.0}
    assert report.results[0].citation_precision == 1.0
    assert report.summary.transport is EvalTransport.E2E


@pytest.mark.asyncio
async def test_tool_eval_requests_configured_result_ceiling() -> None:
    calls: list[tuple[str, int]] = []

    async def search(query: str, limit: int) -> dict[str, Any]:
        calls.append((query, limit))
        return {"query": query, "results": [], "returned": 0, "limit_applied": limit}

    await _silent_harness(tool_search=search).run(
        [EvalQuestion(id="q-limit", question="ceiling")],
        _app(_FailingStore(), max_result_count=40),
        render=False,
    )

    assert calls == [("ceiling", 40)]


def test_load_eval_questions_validates_extended_yaml(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text(
        """
- id: q1
  question: What mentions Datacron?
  expected_chunk_ids:
    - note::chunk::0000
  expected_paths:
    - docs/ARCHITECTURE.md
  forbidden_paths:
    - docs/ARCHITECTURE-old.md
""".lstrip(),
        encoding="utf-8",
    )

    questions = load_eval_questions(path)

    assert questions[0].expected_chunk_ids == ["note::chunk::0000"]
    assert questions[0].expected_paths == ["docs/ARCHITECTURE.md"]
    assert questions[0].forbidden_paths == ["docs/ARCHITECTURE-old.md"]


def test_load_eval_questions_rejects_non_list_yaml(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text("questions: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a list"):
        load_eval_questions(path)
