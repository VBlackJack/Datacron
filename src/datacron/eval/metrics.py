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
"""Pure metric functions for path-level retrieval evaluation."""

from __future__ import annotations

__all__ = ["citation_precision", "recall_at_k"]


def recall_at_k(expected_paths: list[str], retrieved_paths: list[str], k: int) -> float:
    """Fraction of expected paths present among the paths of the top-k retrieved chunks."""
    if not expected_paths:
        return 1.0
    top_k = set(retrieved_paths[:k])
    return sum(1 for path in expected_paths if path in top_k) / len(expected_paths)


def citation_precision(expected_paths: list[str], retrieved_paths: list[str]) -> float:
    """Fraction of retrieved chunks whose path is in the expected set. 1.0 if none retrieved."""
    if not retrieved_paths:
        return 1.0
    expected = set(expected_paths)
    return sum(1 for path in retrieved_paths if path in expected) / len(retrieved_paths)
