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
"""Tests for the deterministic frozen contradiction advisory."""

from __future__ import annotations

import socket

import pytest

from datacron.contradictions import ADVISORY_WARNING, build_advisory_report


def test_frozen_advisory_replay_is_deterministic_and_explicit() -> None:
    first = build_advisory_report()
    second = build_advisory_report()

    assert first == second
    assert next(iter(first)) == "warning"
    assert first["warning"] == ADVISORY_WARNING
    assert "NOT VALIDATED ON REAL CONTENT (0/4)" in first["warning"]
    assert "UNCALIBRATED" in first["warning"]
    assert first["advisory_only"] is True
    assert first["effects"] == {
        "writes": "none",
        "merges": "none",
        "health": "none",
        "ci": "none",
    }
    assert first["frozen_input"] == {
        "pool_pairs": 393,
        "evidence_sha256": ("eb421b383c460440cc4749ef649f3f7e23280b8d91d282d079b411f19c80012a"),
        "ranking_sha256": ("f6e1c1d31a40da7134b285dce1f0690bd033680b22d1fcac8cbc2aa9c619681b"),
    }
    assert first["cache_replay"]["cache_hits"] == 393
    assert first["cache_replay"]["cache_misses"] == 0
    assert first["cache_replay"]["network_calls"] == 0
    assert first["candidate_count"] == len(first["candidates"]) == 24
    assert first["human_adjudication_backlog"] == {
        "unjudged_pairs": 254,
        "retained_candidates": 5,
        "included_in_precision": False,
    }
    assert all(candidate["confidence_calibrated"] is False for candidate in first["candidates"])
    assert all(
        candidate[side]["source_assertion"]["text"]
        for candidate in first["candidates"]
        for side in ("left", "right")
    )


def test_frozen_advisory_replay_has_no_network_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(socket, "create_connection", fail_network)

    report = build_advisory_report()

    assert report["available"] is True
    assert report["cache_replay"]["network_calls"] == 0
