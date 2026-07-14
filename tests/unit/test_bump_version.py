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
"""Tests for the CalVer bump helper (scripts/bump_version.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bump_version.py"


def _next(current: str, on_date: str) -> str:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run", "--current", current, "--date", on_date],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_new_day_resets_counter_to_zero() -> None:
    assert _next("2026.0714.03", "2026-07-15") == "2026.0715.00"


def test_same_day_increments_counter() -> None:
    assert _next("2026.0714.00", "2026-07-14") == "2026.0714.01"


def test_counter_stays_two_digit_padded() -> None:
    assert _next("2026.0714.08", "2026-07-14") == "2026.0714.09"


def test_non_calver_current_starts_at_zero() -> None:
    assert _next("0.1.0.dev0", "2026-07-14") == "2026.0714.00"


def test_single_digit_month_and_day_are_padded() -> None:
    assert _next("2025.1231.00", "2026-01-05") == "2026.0105.00"
