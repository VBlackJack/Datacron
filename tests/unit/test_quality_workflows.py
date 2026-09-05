# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Publication must depend on the complete reusable quality workflow."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"


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
    assert set(gate["needs"]) == {"scope", "lint-type-test", "shellcheck", "dependency-audit"}
    for name, consumer in [("release.yml", "build"), ("publish-pypi.yml", "build-dist")]:
        workflow = _workflow(name)
        assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/ci.yml"
        assert workflow["jobs"][consumer]["needs"] == "verify"


@pytest.mark.parametrize("job", ["scope", "lint-type-test", "shellcheck", "dependency-audit"])
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
        (["README.en.md"], "pull_request", "", 1),
        (["README.md", "src/code.py"], "push", "", 6),
        (["docs/fr/example.py"], "push", "", 6),
        ([".github/workflows/ci.yml"], "push", "", 6),
        (["README.md"], "push", "true", 6),
        (["README.md"], "workflow_dispatch", "", 6),
        ([], "push", "", 6),
        (["README.md"], "tag", "", 6),
        (["README.md"], "new_branch", "", 6),
        (["README.md"], "missing_base", "", 6),
        (["README.md"], "rename_code", "", 6),
    ],
)
def test_scope_uses_actual_git_diff(
    tmp_path: Path, paths: list[str], event_name: str, force_full: str, expected_count: int
) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

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
    assert len(matrix["os"]) * len(matrix["python-version"]) == expected_count


def test_reusable_ci_defaults_to_full_matrix() -> None:
    ci = _workflow("ci.yml")
    assert ci["on"]["workflow_call"]["inputs"]["force-full"]["default"] == "true"
    assert ci["jobs"]["lint-type-test"]["needs"] == "scope"
