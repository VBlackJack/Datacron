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
"""End-to-end behaviour of ``datacron reorganize``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.config import reset_settings_cache
from datacron.core.paths import sidecar_vault_config

_ORGANIZATION = {
    "organization": {
        "rules": [
            {"tag": "memory/decision", "folder": "_memory/decisions", "naming": "{date}-{slug}"},
            {"tag": "memory/fact", "folder": "_memory/facts", "naming": "{date}-{slug}"},
        ]
    }
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATACRON_VAULT_ROOT", raising=False)
    reset_settings_cache()


def _make_vault(root: Path, *, with_rules: bool = True) -> Path:
    config_path = sidecar_vault_config(root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"vault_id": "test-vault"}
    if with_rules:
        payload.update(_ORGANIZATION)
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return root


def _write_note(root: Path, rel_path: str, tag: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: note\ntags:\n  - {tag}\n---\n\nbody\n", encoding="utf-8")


def test_dry_run_is_mandatory(runner: CliRunner, tmp_path: Path) -> None:
    """The flag must stay explicit so no habit of running without it forms."""
    _make_vault(tmp_path)

    result = runner.invoke(app, ["reorganize", "--vault", str(tmp_path)])

    assert result.exit_code == 2
    assert "--dry-run" in result.output


def test_clean_vault_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/2026-08-29-clean.md", "memory/fact")

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(tmp_path)])

    assert result.exit_code == 0


def test_deviations_exit_one_and_are_listed(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/2026-08-29-misplaced.md", "memory/decision")

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(tmp_path)])

    assert result.exit_code == 1
    assert "WRONG_FOLDER" in result.output
    assert "_memory/decisions" in result.output


def test_json_output_is_valid_and_stable(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/2026-08-29-misplaced.md", "memory/decision")

    first = runner.invoke(app, ["reorganize", "--dry-run", "--json", "--vault", str(tmp_path)])
    second = runner.invoke(app, ["reorganize", "--dry-run", "--json", "--vault", str(tmp_path)])

    assert first.exit_code == 1
    payload = json.loads(first.output)
    assert payload["schema"] == "organization-plan-v1"
    assert payload["counts"]["WRONG_FOLDER"] == 1
    assert first.output == second.output


def test_kind_filter_narrows_the_report(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/undated.md", "memory/fact")

    filtered = runner.invoke(
        app,
        ["reorganize", "--dry-run", "--json", "--kind", "WRONG_FOLDER", "--vault", str(tmp_path)],
    )

    assert filtered.exit_code == 0
    assert json.loads(filtered.output)["deviations"] == []


def test_unknown_kind_is_a_configuration_error(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)

    result = runner.invoke(
        app, ["reorganize", "--dry-run", "--kind", "NOPE", "--vault", str(tmp_path)]
    )

    assert result.exit_code == 2


def test_vault_without_sidecar_is_a_configuration_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(tmp_path)])

    assert result.exit_code == 2


def test_vault_without_rules_reports_nothing_to_measure(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path, with_rules=False)
    _write_note(tmp_path, "_memory/facts/undated.md", "memory/fact")

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(tmp_path)])

    assert result.exit_code == 0
    assert "nothing to measure" in result.output
