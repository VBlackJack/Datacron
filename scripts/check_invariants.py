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

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

from datacron import __version__
from datacron.core.config import Settings
from datacron.core.logger import configure_logging, get_logger, shutdown_logging
from datacron.core.versioning import normalize_calver

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SERVER_JSON_PATH: Final[Path] = _REPO_ROOT / "server.json"
_COMMANDS: Final[tuple[tuple[str, ...], ...]] = (
    (sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"),
    (sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"),
    (sys.executable, "-m", "mypy", "--strict", "src", "tests", "scripts"),
    (sys.executable, "-m", "pytest", "-m", "invariants"),
)


def _read_server_versions(path: Path) -> tuple[str, str]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("server.json must contain a JSON object")
    descriptor_version = raw.get("version")
    packages = raw.get("packages")
    if not isinstance(descriptor_version, str):
        raise ValueError("server.json version must be a string")
    if not isinstance(packages, list) or not packages or not isinstance(packages[0], dict):
        raise ValueError("server.json must contain at least one package object")
    package_version = packages[0].get("version")
    if not isinstance(package_version, str):
        raise ValueError("server.json package version must be a string")
    return descriptor_version, package_version


def main() -> int:
    """Run each gate layer in order and return the first non-zero exit code."""
    configure_logging(Settings(log_dir=_REPO_ROOT / "local" / "logs"))
    logger = get_logger(__name__)
    try:
        expected_version = normalize_calver(__version__)
        try:
            descriptor_version, package_version = _read_server_versions(_SERVER_JSON_PATH)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error("server.json version invariant could not be checked: %s", exc)
            print(f"[invariants] server.json version check failed: {exc}", flush=True)
            return 1
        if descriptor_version != expected_version or package_version != expected_version:
            logger.error(
                "server.json version drift: source=%s expected=%s descriptor=%s package=%s",
                __version__,
                expected_version,
                descriptor_version,
                package_version,
            )
            print(
                "[invariants] server.json version drift: "
                f"expected={expected_version} descriptor={descriptor_version} "
                f"package={package_version}",
                flush=True,
            )
            return 1
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
