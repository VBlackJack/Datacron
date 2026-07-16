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
"""Packaging and frozen-launcher contract tests."""

from __future__ import annotations

import runpy
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
LAUNCHER = ROOT / "packaging" / "datacron_launcher.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
WINDOWS_BUILD_SCRIPT = ROOT / "scripts" / "build_installer.ps1"
POSIX_BUILD_SCRIPT = ROOT / "scripts" / "build_installer.sh"


def test_truststore_dependency_and_build_contract_are_declared() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    launcher = LAUNCHER.read_text(encoding="utf-8")

    assert "truststore>=0.10.4" in project["dependencies"]
    assert launcher.index("truststore.inject_into_ssl()") < launcher.index(
        "from datacron.cli import app"
    )
    assert "--hidden-import truststore" in RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert '"--hidden-import", "truststore"' in WINDOWS_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "--hidden-import truststore" in POSIX_BUILD_SCRIPT.read_text(encoding="utf-8")


def test_launcher_injects_truststore_before_importing_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    truststore_module = ModuleType("truststore")
    truststore_module.inject_into_ssl = lambda: calls.append("inject")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", truststore_module)

    runpy.run_path(str(LAUNCHER), run_name="test_datacron_launcher")

    assert calls == ["inject"]


def test_launcher_reports_truststore_injection_failure_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    truststore_module = ModuleType("truststore")

    def fail_injection() -> None:
        raise RuntimeError("native store unavailable")

    truststore_module.inject_into_ssl = fail_injection  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", truststore_module)

    runpy.run_path(str(LAUNCHER), run_name="test_datacron_launcher")

    assert (
        capsys.readouterr().err
        == "[datacron] truststore injection failed: native store unavailable\n"
    )
