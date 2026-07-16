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
"""Pure metric functions for retrieval evaluation."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN

__all__ = [
    "citation_precision",
    "deduplicate_ranked",
    "forbidden_violation",
    "ndcg_at_k",
    "payload_token_estimate",
    "recall_at_k",
    "reciprocal_rank",
]


def deduplicate_ranked(values: Sequence[str]) -> list[str]:
    """Return the first occurrence of each value without changing rank order."""
    return list(dict.fromkeys(values))


def recall_at_k(expected_paths: Sequence[str], retrieved_paths: Sequence[str], k: int) -> float:
    """Return the fraction of distinct expected values present in the top ``k``."""
    if not expected_paths:
        return 1.0
    expected = set(expected_paths)
    top_k = set(retrieved_paths[:k])
    return len(expected & top_k) / len(expected)


def citation_precision(expected_paths: Sequence[str], retrieved_paths: Sequence[str]) -> float:
    """Return precision over distinct retrieved note paths, preserving their first rank."""
    retrieved = deduplicate_ranked(retrieved_paths)
    if not retrieved:
        return 1.0
    expected = set(expected_paths)
    return sum(1 for path in retrieved if path in expected) / len(retrieved)


def reciprocal_rank(expected_paths: Sequence[str], retrieved_paths: Sequence[str]) -> float:
    """Return the reciprocal rank of the first expected distinct note, or zero."""
    expected = set(expected_paths)
    if not expected:
        return 0.0
    for rank, path in enumerate(deduplicate_ranked(retrieved_paths), start=1):
        if path in expected:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(expected_paths: Sequence[str], retrieved_paths: Sequence[str], k: int) -> float:
    """Return binary-relevance nDCG at ``k`` over distinct note paths."""
    expected = set(expected_paths)
    if not expected:
        return 1.0
    if k <= 0:
        return 0.0

    ranked = deduplicate_ranked(retrieved_paths)[:k]
    dcg = sum(
        1.0 / math.log2(rank + 1) for rank, path in enumerate(ranked, start=1) if path in expected
    )
    ideal_hits = min(len(expected), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal_dcg


def forbidden_violation(
    forbidden_paths: Sequence[str],
    retrieved_paths: Sequence[str],
    k: int = 5,
) -> bool:
    """Return whether a forbidden note occurs among the top ``k`` distinct notes."""
    forbidden = set(forbidden_paths)
    return bool(forbidden & set(deduplicate_ranked(retrieved_paths)[:k]))


def payload_token_estimate(payload: Any) -> int:
    """Estimate tokens from the compact JSON response using Datacron's 4-char rule."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, len(serialized) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)
