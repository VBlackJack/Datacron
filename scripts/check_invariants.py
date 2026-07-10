#!/usr/bin/env python3
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
"""Run the complete blocking Datacron invariant gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

from datacron.core.config import Settings
from datacron.core.logger import configure_logging, get_logger, shutdown_logging

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_COMMANDS: Final[tuple[tuple[str, ...], ...]] = (
    (sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"),
    (sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"),
    (sys.executable, "-m", "mypy", "--strict", "src", "tests", "scripts"),
    (sys.executable, "-m", "pytest", "-m", "invariants"),
)


def main() -> int:
    """Run each gate layer in order and return the first non-zero exit code."""
    configure_logging(Settings(log_dir=_REPO_ROOT / "local" / "logs"))
    logger = get_logger(__name__)
    try:
        for command in _COMMANDS:
            rendered = " ".join(command[1:])
            print(f"[invariants] {rendered}", flush=True)
            logger.info("invariant gate command started: %s", rendered)
            completed = subprocess.run(command, cwd=_REPO_ROOT, check=False)  # noqa: S603
            if completed.returncode:
                logger.error(
                    "invariant gate command failed: command=%s exit_code=%d",
                    rendered,
                    completed.returncode,
                )
                return completed.returncode
        logger.info("invariant gate passed")
        return 0
    finally:
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
