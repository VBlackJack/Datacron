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
"""Local retrieval evaluation harness for the store and real MCP tool layers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, Final

import yaml
from rich.console import Console
from rich.table import Table

from datacron.core.models import (
    EvalPipeline,
    EvalQuestion,
    EvalReport,
    EvalResult,
    EvalStageLatency,
    EvalSummary,
    EvalTransport,
    SearchResult,
)
from datacron.core.protocols import EvalHarness
from datacron.eval.metrics import (
    citation_precision,
    deduplicate_ranked,
    forbidden_violation,
    ndcg_at_k,
    payload_token_estimate,
    recall_at_k,
    reciprocal_rank,
)
from datacron.mcp.server import DatacronApp
from datacron.mcp.tools import _search_text_impl

__all__ = ["DEFAULT_K_VALUES", "LocalEvalHarness", "load_eval_questions"]

DEFAULT_K_VALUES: Final[tuple[int, int, int]] = (5, 10, 20)
_ENCODING_UTF8: Final[str] = "utf-8"
_NDCG_K: Final[int] = 10
_FORBIDDEN_K: Final[int] = 5

ToolSearch = Callable[[str, int], Awaitable[dict[str, Any]]]


def load_eval_questions(path: Path) -> list[EvalQuestion]:
    """Load and validate eval questions from a YAML list."""
    raw = yaml.safe_load(path.read_text(encoding=_ENCODING_UTF8))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Eval questions YAML must be a list of question objects.")
    return [EvalQuestion.model_validate(item) for item in raw]


class LocalEvalHarness:
    """Evaluate either the complete search tool or its underlying FTS5 store."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        tool_search: ToolSearch | None = None,
    ) -> None:
        self._console = console or Console()
        self._tool_search = tool_search

    async def run(
        self,
        eval_questions: list[EvalQuestion],
        app: DatacronApp,
        k_values: Sequence[int] = DEFAULT_K_VALUES,
        *,
        pipeline: EvalPipeline = EvalPipeline.TOOL,
        transport: EvalTransport = EvalTransport.IMPL,
        render: bool = True,
    ) -> EvalReport:
        """Execute the eval and return per-question plus aggregate metrics."""
        if pipeline is EvalPipeline.STORE and transport is EvalTransport.E2E:
            raise ValueError("The e2e transport is only available with the tool pipeline.")
        if transport is EvalTransport.E2E and self._tool_search is None:
            raise ValueError("The e2e transport requires an MCP search callback.")

        limit = max(k_values) if k_values else 0
        if pipeline is EvalPipeline.TOOL:
            limit = max(limit, app.settings.max_result_count)
        results: list[EvalResult] = []
        for question in eval_questions:
            started = perf_counter()
            payload = await self._search(app, question.question, limit, pipeline)
            latency_ms = (perf_counter() - started) * 1000.0
            results.append(
                _evaluate_payload(
                    question,
                    payload,
                    latency_ms=latency_ms,
                    k_values=k_values,
                )
            )

        report = EvalReport(
            summary=_summarize(results, k_values, pipeline=pipeline, transport=transport),
            results=results,
        )
        if render:
            self._print_summary(report)
        return report

    async def _search(
        self,
        app: DatacronApp,
        query: str,
        limit: int,
        pipeline: EvalPipeline,
    ) -> dict[str, Any]:
        if pipeline is EvalPipeline.STORE:
            search_results = await app.store.search(query, limit=limit)
            return _store_payload(query, search_results, limit)
        if self._tool_search is not None:
            return await self._tool_search(query, limit)
        return await _search_text_impl(app, query=query, limit=limit, include_timings=True)

    def _print_summary(self, report: EvalReport) -> None:
        """Render a compact Rich summary for an eval run."""
        results = report.results
        summary = report.summary
        if not results:
            self._console.print("[yellow]No eval questions supplied.[/yellow]")
            return

        table = Table(
            title=f"Datacron Eval Summary ({summary.pipeline.value}/{summary.transport.value})"
        )
        table.add_column("ID", no_wrap=True)
        table.add_column("Notes", justify="right")
        for k in summary.note_recall_at_k:
            table.add_column(f"R{k}", justify="right")
        table.add_column("MRR", justify="right")
        table.add_column("nDCG10", justify="right")
        table.add_column("P", justify="right")
        table.add_column("Fresh", justify="right")
        table.add_column("ms", justify="right")
        table.add_column("Tok", justify="right")

        for result in results:
            freshness = "FAIL" if result.forbidden_violation else "ok"
            if not result.forbidden_evaluated:
                freshness = "-"
            table.add_row(
                result.question_id,
                str(len(result.retrieved_paths)),
                *[f"{result.recall_at_k.get(k, 0.0):.2f}" for k in summary.note_recall_at_k],
                f"{result.reciprocal_rank:.2f}",
                f"{result.ndcg_at_10:.2f}",
                f"{result.citation_precision:.2f}",
                freshness,
                f"{result.latency_ms:.0f}",
                str(result.tokens_returned),
            )

        self._console.print(table)
        recalls = "  ".join(f"R@{k}: {value:.2f}" for k, value in summary.note_recall_at_k.items())
        freshness = (
            "n/a"
            if summary.forbidden_violation_rate is None
            else f"{summary.forbidden_violation_rate:.2%}"
        )
        self._console.print(
            f"{recalls}  MRR: {summary.mrr:.2f}  nDCG@10: {summary.ndcg_at_10:.2f}  "
            f"Precision: {summary.citation_precision:.2f}"
        )
        self._console.print(
            f"Forbidden@5: {freshness}  latency p50/p95: "
            f"{summary.latency_p50_ms:.0f}/{summary.latency_p95_ms:.0f}ms  "
            f"Payload tokens: {summary.total_tokens_returned}"
        )


def _store_payload(query: str, results: list[SearchResult], limit: int) -> dict[str, Any]:
    """Normalize raw store hits to the same shape consumed from the MCP tool."""
    return {
        "query": query,
        "results": [
            {
                "chunk_id": result.chunk.chunk_id,
                "note_rel_path": result.chunk.note_rel_path,
                "score": result.score,
                "snippet": result.snippet,
                "token_count": result.chunk.token_count,
            }
            for result in results
        ],
        "returned": len(results),
        "limit_applied": limit,
    }


def _evaluate_payload(
    question: EvalQuestion,
    payload: dict[str, Any],
    *,
    latency_ms: float,
    k_values: Sequence[int],
) -> EvalResult:
    """Compute all metrics for one normalized response payload."""
    if "error" in payload:
        raise RuntimeError(f"search failed for {question.id}: {payload['error']}")
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise TypeError(f"search response for {question.id} has a non-list results field")

    retrieved_chunk_ids: list[str] = []
    retrieved_paths_raw: list[str] = []
    for item in raw_results:
        if not isinstance(item, dict):
            raise TypeError(f"search response for {question.id} contains a non-object result")
        chunk_id = item.get("chunk_id")
        note_rel_path = item.get("note_rel_path")
        if isinstance(chunk_id, str):
            retrieved_chunk_ids.append(chunk_id)
        if isinstance(note_rel_path, str):
            retrieved_paths_raw.append(note_rel_path)

    retrieved_paths = deduplicate_ranked(retrieved_paths_raw)
    chunk_recall = None
    if question.expected_chunk_ids:
        chunk_recall = {
            k: recall_at_k(question.expected_chunk_ids, retrieved_chunk_ids, k) for k in k_values
        }
    return EvalResult(
        question_id=question.id,
        retrieved_chunk_ids=retrieved_chunk_ids,
        retrieved_paths=retrieved_paths,
        recall_at_k={k: recall_at_k(question.expected_paths, retrieved_paths, k) for k in k_values},
        chunk_recall_at_k=chunk_recall,
        reciprocal_rank=reciprocal_rank(question.expected_paths, retrieved_paths),
        ndcg_at_10=ndcg_at_k(question.expected_paths, retrieved_paths, _NDCG_K),
        citation_precision=citation_precision(question.expected_paths, retrieved_paths),
        forbidden_violation=forbidden_violation(
            question.forbidden_paths,
            retrieved_paths,
            _FORBIDDEN_K,
        ),
        forbidden_evaluated=bool(question.forbidden_paths),
        latency_ms=latency_ms,
        stage_timings_ms=_stage_timings(payload, question.id),
        tokens_returned=payload_token_estimate(payload),
        trust_label=None,
    )


def _summarize(
    results: list[EvalResult],
    k_values: Sequence[int],
    *,
    pipeline: EvalPipeline,
    transport: EvalTransport,
) -> EvalSummary:
    """Aggregate an eval run without losing optional chunk/freshness semantics."""
    chunk_results = [result for result in results if result.chunk_recall_at_k is not None]
    chunk_recall = None
    if chunk_results:
        chunk_recall = {
            k: _average(
                [
                    result.chunk_recall_at_k.get(k, 0.0)
                    for result in chunk_results
                    if result.chunk_recall_at_k is not None
                ]
            )
            for k in k_values
        }
    freshness_results = [result for result in results if result.forbidden_evaluated]
    stage_names = sorted({stage for result in results for stage in result.stage_timings_ms})
    return EvalSummary(
        pipeline=pipeline,
        transport=transport,
        question_count=len(results),
        note_recall_at_k={
            k: _average([result.recall_at_k.get(k, 0.0) for result in results]) for k in k_values
        },
        chunk_recall_at_k=chunk_recall,
        mrr=_average([result.reciprocal_rank for result in results]),
        ndcg_at_10=_average([result.ndcg_at_10 for result in results]),
        citation_precision=_average([result.citation_precision for result in results]),
        forbidden_violation_rate=(
            _average([float(result.forbidden_violation) for result in freshness_results])
            if freshness_results
            else None
        ),
        latency_p50_ms=_percentile([result.latency_ms for result in results], 0.50),
        latency_p95_ms=_percentile([result.latency_ms for result in results], 0.95),
        stage_latency_ms={
            stage: EvalStageLatency(
                p50_ms=_percentile(
                    [
                        result.stage_timings_ms[stage]
                        for result in results
                        if stage in result.stage_timings_ms
                    ],
                    0.50,
                ),
                p95_ms=_percentile(
                    [
                        result.stage_timings_ms[stage]
                        for result in results
                        if stage in result.stage_timings_ms
                    ],
                    0.95,
                ),
            )
            for stage in stage_names
        },
        total_tokens_returned=sum(result.tokens_returned for result in results),
        avg_tokens_returned=_average([float(result.tokens_returned) for result in results]),
    )


def _stage_timings(payload: dict[str, Any], question_id: str) -> dict[str, float]:
    """Validate optional per-stage timing values from a tool response."""
    raw = payload.get("timings_ms")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError(f"search response for {question_id} has invalid timings_ms")
    timings: dict[str, float] = {}
    for stage, value in raw.items():
        if not isinstance(stage, str) or not isinstance(value, int | float) or value < 0:
            raise TypeError(f"search response for {question_id} has invalid stage timing")
        timings[stage] = float(value)
    return timings


def _average(values: Sequence[float]) -> float:
    """Return the arithmetic mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile, or zero for no observations."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _conformance_check(_: EvalHarness) -> None:
    """Mypy structural conformance: LocalEvalHarness must satisfy EvalHarness."""


_conformance_check(LocalEvalHarness())
