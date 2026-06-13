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
"""Local retrieval evaluation harness for Phase 0."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Final

import yaml
from rich.console import Console
from rich.table import Table

from datacron.core.models import EvalQuestion, EvalResult
from datacron.core.protocols import EvalHarness, FTS5Store, RipgrepWrapper
from datacron.eval.metrics import citation_precision, recall_at_k

__all__ = ["DEFAULT_K_VALUES", "LocalEvalHarness", "load_eval_questions"]

DEFAULT_K_VALUES: Final[tuple[int, int, int]] = (5, 10, 20)
_ENCODING_UTF8: Final[str] = "utf-8"


def load_eval_questions(path: Path) -> list[EvalQuestion]:
    """Load and validate eval questions from a YAML list."""
    raw = yaml.safe_load(path.read_text(encoding=_ENCODING_UTF8))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Eval questions YAML must be a list of question objects.")
    return [EvalQuestion.model_validate(item) for item in raw]


class LocalEvalHarness:
    """Run BM25-only retrieval evals directly against the local FTS5 store."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    async def run(
        self,
        eval_questions: list[EvalQuestion],
        store: FTS5Store,
        ripgrep: RipgrepWrapper,
        k_values: Sequence[int] = DEFAULT_K_VALUES,
    ) -> list[EvalResult]:
        """Execute the eval and return one :class:`EvalResult` per question."""
        _ = ripgrep
        limit = max(k_values) if k_values else 0
        results: list[EvalResult] = []

        for question in eval_questions:
            started = perf_counter()
            search_results = await store.search(question.question, limit=limit)
            latency_ms = (perf_counter() - started) * 1000.0

            retrieved_chunk_ids = [result.chunk.chunk_id for result in search_results]
            retrieved_paths = [result.chunk.note_rel_path for result in search_results]
            recall = {k: recall_at_k(question.expected_paths, retrieved_paths, k) for k in k_values}
            precision = citation_precision(question.expected_paths, retrieved_paths)
            tokens_returned = sum(result.chunk.token_count for result in search_results)

            results.append(
                EvalResult(
                    question_id=question.id,
                    retrieved_chunk_ids=retrieved_chunk_ids,
                    recall_at_k=recall,
                    citation_precision=precision,
                    latency_ms=latency_ms,
                    tokens_returned=tokens_returned,
                    trust_label=None,
                )
            )

        self._print_summary(results, k_values)
        return results

    def _print_summary(self, results: list[EvalResult], k_values: Sequence[int]) -> None:
        """Render a compact rich summary for an eval run."""
        if not results:
            self._console.print("[yellow]No eval questions supplied.[/yellow]")
            return

        table = Table(title="Datacron Eval Summary")
        table.add_column("ID", no_wrap=True)
        table.add_column("N", justify="right")
        for k in k_values:
            table.add_column(f"R{k}", justify="right")
        table.add_column("P", justify="right")
        table.add_column("ms", justify="right")
        table.add_column("Tok", justify="right")

        for result in results:
            table.add_row(
                result.question_id,
                str(len(result.retrieved_chunk_ids)),
                *[f"{result.recall_at_k.get(k, 0.0):.2f}" for k in k_values],
                f"{result.citation_precision:.2f}",
                f"{result.latency_ms:.0f}",
                str(result.tokens_returned),
            )

        avg_precision = _average([result.citation_precision for result in results])
        avg_latency = _average([result.latency_ms for result in results])
        total_tokens = sum(result.tokens_returned for result in results)

        self._console.print(table)
        for k in k_values:
            avg_recall = _average([result.recall_at_k.get(k, 0.0) for result in results])
            self._console.print(f"Avg recall@{k}: {avg_recall:.2f}")
        self._console.print(
            f"Avg precision: {avg_precision:.2f}  "
            f"Avg latency: {avg_latency:.0f}ms  "
            f"Total tokens: {total_tokens}"
        )


def _average(values: Sequence[float]) -> float:
    """Return the arithmetic mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0


def _conformance_check(_: EvalHarness) -> None:
    """Mypy structural conformance: LocalEvalHarness must satisfy EvalHarness."""


_conformance_check(LocalEvalHarness())
