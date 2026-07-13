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
"""Regression tests for side-effect-free Datacron imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_IMPORT_COMMAND = (
    "import datacron.mcp.tools, datacron.core.vault_writer, datacron.core.operation_log"
)
_CONFIGURE_LOGGING_COMMAND = (
    "from datacron.core.logger import configure_logging; configure_logging()"
)


def _run_import(
    tmp_path: Path, *, log_level: str | None = None
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    working_directory = tmp_path / "work"
    home.mkdir()
    working_directory.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("DATACRON_")
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if log_level is not None:
        environment["DATACRON_LOG_LEVEL"] = log_level

    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_COMMAND],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert list(home.iterdir()) == []
    assert list(working_directory.iterdir()) == []
    return completed


def test_imports_are_silent_and_do_not_create_runtime_directories(tmp_path: Path) -> None:
    _run_import(tmp_path)


def test_imports_do_not_parse_invalid_logging_environment(tmp_path: Path) -> None:
    _run_import(tmp_path, log_level="INVALID")

    home = tmp_path / "configure-home"
    working_directory = tmp_path / "configure-work"
    home.mkdir()
    working_directory.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("DATACRON_")
    }
    environment.update(
        {
            "DATACRON_LOG_LEVEL": "INVALID",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", _CONFIGURE_LOGGING_COMMAND],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Invalid DATACRON_LOG_LEVEL" in completed.stderr
