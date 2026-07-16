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
"""Evaluation primitives for Datacron retrieval quality."""

from __future__ import annotations

from datacron.eval.baseline import (
    BaselineComparison,
    EvalBaseline,
    compare_with_baseline,
    load_baseline,
    save_baseline,
)
from datacron.eval.harness import LocalEvalHarness, load_eval_questions
from datacron.eval.metrics import (
    citation_precision,
    deduplicate_ranked,
    forbidden_violation,
    ndcg_at_k,
    payload_token_estimate,
    recall_at_k,
    reciprocal_rank,
)

__all__ = [
    "BaselineComparison",
    "EvalBaseline",
    "LocalEvalHarness",
    "citation_precision",
    "compare_with_baseline",
    "deduplicate_ranked",
    "forbidden_violation",
    "load_baseline",
    "load_eval_questions",
    "ndcg_at_k",
    "payload_token_estimate",
    "recall_at_k",
    "reciprocal_rank",
    "save_baseline",
]
