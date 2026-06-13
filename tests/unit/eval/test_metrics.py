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
"""Tests for :mod:`datacron.eval.metrics`."""

from __future__ import annotations

from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from datacron.eval.metrics import citation_precision, recall_at_k

_PATH_ALPHABET: Final[str] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-. "
_PATHS = st.lists(
    st.text(
        alphabet=_PATH_ALPHABET,
        min_size=1,
        max_size=40,
    ),
    max_size=30,
)


class TestRecallAtK:
    def test_empty_expected_paths_are_perfect_recall(self) -> None:
        retrieved = ["a.md", "b.md"]

        assert recall_at_k([], retrieved, 5) == 1.0
        assert recall_at_k([], retrieved, 10) == 1.0

    def test_empty_retrieved_paths_miss_non_empty_expected_paths(self) -> None:
        assert recall_at_k(["a.md"], [], 5) == 0.0

    def test_k_larger_than_retrieved_paths_is_bounded(self) -> None:
        assert recall_at_k(["a.md", "b.md"], ["a.md"], 20) == 0.5

    def test_retrieved_duplicates_count_as_unique_presence(self) -> None:
        assert recall_at_k(["a.md", "b.md"], ["a.md", "a.md", "a.md"], 10) == 0.5


class TestCitationPrecision:
    def test_empty_retrieved_paths_are_perfect_precision(self) -> None:
        assert citation_precision(["a.md"], []) == 1.0

    def test_retrieved_duplicates_count_each_occurrence(self) -> None:
        retrieved = ["a.md", "a.md", "miss.md", "b.md"]

        assert citation_precision(["a.md", "b.md"], retrieved) == 0.75


@given(expected_paths=_PATHS, retrieved_paths=_PATHS)
def test_metric_values_are_always_bounded(
    expected_paths: list[str],
    retrieved_paths: list[str],
) -> None:
    for k in (0, 1, 5, 10, 20, len(retrieved_paths) + 5):
        value = recall_at_k(expected_paths, retrieved_paths, k)
        assert 0.0 <= value <= 1.0

    precision = citation_precision(expected_paths, retrieved_paths)
    assert 0.0 <= precision <= 1.0


@given(expected_paths=_PATHS, retrieved_paths=_PATHS)
def test_recall_is_monotonic_as_k_increases(
    expected_paths: list[str],
    retrieved_paths: list[str],
) -> None:
    recall_5 = recall_at_k(expected_paths, retrieved_paths, 5)
    recall_10 = recall_at_k(expected_paths, retrieved_paths, 10)
    recall_20 = recall_at_k(expected_paths, retrieved_paths, 20)

    assert recall_5 <= recall_10 <= recall_20
