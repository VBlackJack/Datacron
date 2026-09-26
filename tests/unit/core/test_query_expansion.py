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
"""Tests for :mod:`datacron.core.query_expansion`."""

from __future__ import annotations

from datacron.core.query_expansion import expand_terms, normalize_term_map


def test_expand_terms_groups_mapped_and_unmapped_terms() -> None:
    groups = expand_terms(["supervision", "oscare"], {"supervision": ["monitoring"]})

    assert groups == [["supervision", "monitoring"], ["oscare"]]


def test_expand_terms_lookup_is_case_insensitive() -> None:
    groups = expand_terms(["Supervision"], {"supervision": ["monitoring"]})

    assert groups == [["Supervision", "monitoring"]]


def test_expand_terms_deduplicates_each_group() -> None:
    groups = expand_terms(
        ["monitoring"],
        {"supervision": ["monitoring", "Monitoring"], "monitoring": ["supervision"]},
    )

    assert groups == [["monitoring", "supervision"]]


def test_normalize_term_map_is_bidirectional() -> None:
    term_map = normalize_term_map({"supervision": ["monitoring"]})

    assert term_map["supervision"] == ["monitoring"]
    assert term_map["monitoring"] == ["supervision"]
