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

import json
import subprocess
import sys
from pathlib import Path
from shutil import copyfile

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


def test_bump_writes_lf_line_endings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script = repo / "scripts" / "bump_version.py"
    init_file = repo / "src" / "datacron" / "__init__.py"
    server_file = repo / "server.json"
    script.parent.mkdir(parents=True)
    init_file.parent.mkdir(parents=True)
    copyfile(_SCRIPT, script)
    init_file.write_bytes(b'"""Test package."""\r\n\r\n__version__ = "2026.0715.00"\r\n')
    server_file.write_bytes(b'{"version":"2026.715.0","packages":[{"version":"2026.715.0"}]}\r\n')

    subprocess.run(
        [sys.executable, str(script), "--date", "2026-07-16"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert b"\r" not in init_file.read_bytes()
    assert b'__version__ = "2026.0716.00"' in init_file.read_bytes()
    assert b"\r" not in server_file.read_bytes()
    server_data = json.loads(server_file.read_text(encoding="utf-8"))
    assert server_data["version"] == "2026.716.0"
    assert server_data["packages"][0]["version"] == "2026.716.0"
