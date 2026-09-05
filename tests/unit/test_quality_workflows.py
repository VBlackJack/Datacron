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
    assert set(gate["needs"]) == {"lint-type-test", "shellcheck", "dependency-audit"}
    for name, consumer in [("release.yml", "build"), ("publish-pypi.yml", "build-dist")]:
        workflow = _workflow(name)
        assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/ci.yml"
        assert workflow["jobs"][consumer]["needs"] == "verify"


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_aggregate_gate_executes_fail_closed(result: str) -> None:
    gate = _workflow("ci.yml")["jobs"]["quality-gate"]
    command = shlex.split(gate["steps"][0]["run"])
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs["dependency-audit"]["result"] = result
    process = subprocess.run(
        [sys.executable, *command[1:]],
        env={**os.environ, "RESULTS": json.dumps(needs)},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (process.returncode == 0) is (result == "success")
