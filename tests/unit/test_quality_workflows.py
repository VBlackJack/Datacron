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
"""Publication must depend on the complete reusable quality workflow."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"


def _leg_count(matrix: dict[str, Any]) -> int:
    """The jobs GitHub starts: the cross product, plus each ``include`` leg.

    Every ``include`` entry names an operating system outside the product, so
    GitHub adds it as a new combination instead of extending an existing one.
    """
    return len(matrix["os"]) * len(matrix["python-version"]) + len(matrix.get("include", []))


def _workflow(name: str) -> dict[str, Any]:
    # BaseLoader produces strings only and preserves the YAML 1.2 "on" key.
    return dict(
        yaml.load((_WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)  # noqa: S506
    )


def test_publication_reuses_entire_ci() -> None:
    ci = _workflow("ci.yml")
    assert "workflow_call" in ci["on"]
    gate = ci["jobs"]["quality-gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {
        "scope",
        "lint-type-test",
        "shellcheck",
        "dependency-audit",
        "lowest-direct",
    }
    for name, consumer in [("release.yml", "build"), ("publish-pypi.yml", "build-dist")]:
        workflow = _workflow(name)
        assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/ci.yml"
        assert workflow["jobs"][consumer]["needs"] == "verify"


@pytest.mark.parametrize(
    "job", ["scope", "lint-type-test", "shellcheck", "dependency-audit", "lowest-direct"]
)
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_aggregate_gate_executes_fail_closed(job: str, result: str) -> None:
    gate = _workflow("ci.yml")["jobs"]["quality-gate"]
    command = shlex.split(gate["steps"][0]["run"])
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs[job]["result"] = result
    process = subprocess.run(
        [sys.executable, *command[1:]],
        env={**os.environ, "RESULTS": json.dumps(needs)},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (process.returncode == 0) is (result == "success")


@pytest.mark.parametrize(
    ("paths", "event_name", "force_full", "expected_count"),
    [
        (["README.md", "docs/fr/setup.md"], "push", "", 1),
        (["README.fr.md"], "pull_request", "", 1),
        (["README.md", "src/code.py"], "push", "", 7),
        (["docs/fr/example.py"], "push", "", 7),
        ([".github/workflows/ci.yml"], "push", "", 7),
        (["README.md"], "push", "true", 7),
        (["README.md"], "workflow_dispatch", "", 7),
        ([], "push", "", 7),
        (["README.md"], "tag", "", 7),
        (["README.md"], "new_branch", "", 7),
        (["README.md"], "missing_base", "", 7),
        (["README.md"], "rename_code", "", 7),
    ],
)
def test_scope_uses_actual_git_diff(
    tmp_path: Path, paths: list[str], event_name: str, force_full: str, expected_count: int
) -> None:
    # Ignore the developer's global and system git configuration (hooks, signing,
    # identity guards) so the throwaway repository behaves the same on every machine.
    isolated_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=tmp_path, text=True, env=isolated_env
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    if event_name == "rename_code":
        (tmp_path / "code.py").write_text("Documentation or code", encoding="utf-8")
        git("add", "code.py")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    if event_name == "rename_code":
        git("rm", "code.py")
    if event_name == "new_branch":
        base = "0" * 40
    elif event_name == "missing_base":
        base = "1" * 40
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Documentation or code", encoding="utf-8")
    git("add", ".")
    git("commit", "--allow-empty", "-qm", "change")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"before": base, "pull_request": {"base": {"sha": base}}}),
        encoding="utf-8",
    )
    output = tmp_path / "output.txt"
    subprocess.run(
        [sys.executable, str(_WORKFLOWS.parents[1] / "scripts" / "ci_scope.py")],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_EVENT_NAME": event_name
            if event_name in {"pull_request", "workflow_dispatch"}
            else "push",
            "GITHUB_REF": "refs/tags/v1" if event_name == "tag" else "refs/heads/test",
            "GITHUB_SHA": git("rev-parse", "HEAD"),
            "GITHUB_OUTPUT": str(output),
            "FORCE_FULL": force_full,
        },
        check=True,
        timeout=30,
    )
    matrix = json.loads(output.read_text(encoding="utf-8").removeprefix("matrix="))
    assert _leg_count(matrix) == expected_count


def test_reusable_ci_defaults_to_full_matrix() -> None:
    ci = _workflow("ci.yml")
    assert ci["on"]["workflow_call"]["inputs"]["force-full"]["default"] == "true"
    assert ci["jobs"]["lint-type-test"]["needs"] == "scope"


def _scope_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ci_scope", _WORKFLOWS.parents[1] / "scripts" / "ci_scope.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_matrix_tests_every_platform_the_release_ships() -> None:
    """The release attaches a macOS arm64 binary, so macOS must be in the gate."""
    scope = _scope_module()
    build_matrix = _workflow("release.yml")["jobs"]["build"]["strategy"]["matrix"]
    released = {entry["os"] for entry in build_matrix["include"]}
    full = scope._FULL_MATRIX
    tested = set(full["os"]) | {leg["os"] for leg in full["include"]}
    assert released <= tested
    assert all(leg["python-version"] in full["python-version"] for leg in full["include"])
    assert "include" not in scope._DOC_MATRIX


def test_dependency_floors_are_installed_and_gated() -> None:
    ci = _workflow("ci.yml")
    job = ci["jobs"]["lowest-direct"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--resolution lowest-direct" in commands
    assert "datacron --help" in commands
    assert "pytest" in commands
    assert "lowest-direct" in ci["jobs"]["quality-gate"]["needs"]


_ISCC_SELECTION_END = 'Write-Host "Using $iscc"'
_PWSH_TIMEOUT_SECONDS = 60


def _iscc_selection_script() -> str:
    steps = _workflow("release.yml")["jobs"]["build"]["steps"]
    run = str(next(step["run"] for step in steps if step.get("name") == "Build Windows installer"))
    return run[: run.index(_ISCC_SELECTION_END) + len(_ISCC_SELECTION_END)]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_installer_build_picks_the_numerically_newest_inno_setup(tmp_path: Path) -> None:
    """Sorting the folders as text ranked "Inno Setup 10" below "Inno Setup 7"."""
    for folder in ("Inno Setup 6", "Inno Setup 7", "Inno Setup 10"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "ISCC.exe").write_bytes(b"")
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    process = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", _iscc_selection_script()],
        env={**os.environ, "ProgramFiles(x86)": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=_PWSH_TIMEOUT_SECONDS,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    chosen = Path(process.stdout.strip().removeprefix("Using ").strip())
    assert chosen.parent.name == "Inno Setup 10"


_SHA_PINNED_ACTION = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
_CHOCO_PINNED = re.compile(r"--version \d+(?:\.\d+)+")


@pytest.mark.parametrize(
    "name", ["ci.yml", "release.yml", "publish-pypi.yml", "runtime-validation.yml"]
)
def test_every_action_and_tool_is_pinned(name: str) -> None:
    """An unpinned tool changes a build without any commit saying so."""
    for job in _workflow(name)["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert _SHA_PINNED_ACTION.fullmatch(step["uses"]), step["uses"]
            for line in step.get("run", "").splitlines():
                if "choco install" in line:
                    assert _CHOCO_PINNED.search(line), line
                for tool in ("twine", "pip-audit"):
                    if f"--from {tool}" in line or f"--with {tool}" in line:
                        assert re.search(rf"{tool}==\d+(?:\.\d+)+", line), line
