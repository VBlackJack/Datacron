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
"""Regressions for the 2026-10-18 audit, configuration and CLI binding lot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from typer.testing import CliRunner

import datacron.cli as cli_module
import datacron.migration as migration_module
from datacron.bootstrap import initialize_vault
from datacron.cli import _prompt_memory_vault, _settings_for_cli_vault, app
from datacron.config_contract import (
    EXIT_VALIDATION_ERROR,
    ConfigValidationError,
    read_vault_config_snapshot,
    write_vault_config_cas,
)
from datacron.core.config import get_settings, reset_settings_cache
from datacron.migration import MigrationResult

_EXIT_USAGE: Final[int] = 2
_KEPT_VAULT_ID: Final[str] = "VID-KEEP"
_KEPT_CREATED: Final[str] = "2026-01-01"
_KEPT_RETENTION: Final[int] = 7
_CHANGED_RETENTION: Final[int] = 60
_ECHO_RETENTION: Final[int] = 90
_TIMEOUT_SECONDS: Final[int] = 60

runner = CliRunner()


def _vault_yaml(vault_root: Path) -> Path:
    return vault_root / ".datacron" / "VAULT.yaml"


def _write_vault_yaml(vault_root: Path, content: str) -> Path:
    config = _vault_yaml(vault_root)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(content, encoding="utf-8")
    return config


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()


# ---------------------------------------------------------------------------
# E-1 / D-1 / D-11: a write never turns an invalid file into defaults
# ---------------------------------------------------------------------------


def test_config_set_refuses_invalid_vault_yaml_and_keeps_its_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _write_vault_yaml(
        vault,
        yaml.safe_dump(
            {
                "schema_version": 1,
                "vault_id": _KEPT_VAULT_ID,
                "created": _KEPT_CREATED,
                "history_retention_days": 0,
            }
        ),
    )
    before = config.read_bytes()

    result = runner.invoke(
        app,
        ["config", "set", "--vault", str(vault), "--values-json", '{"history_mode": "full"}'],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert config.read_bytes() == before

    reset = runner.invoke(
        app,
        [
            "config",
            "set",
            "--vault",
            str(vault),
            "--reset-invalid",
            "--values-json",
            json.dumps({"history_retention_days": _CHANGED_RETENTION}),
        ],
    )
    assert reset.exit_code == 0, reset.output
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {
        "history_retention_days": _CHANGED_RETENTION
    }


def test_core_write_refuses_unparsable_vault_yaml(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _write_vault_yaml(vault, "vault_id: [unclosed\n")
    snapshot = read_vault_config_snapshot(vault)
    assert snapshot.valid is False

    with pytest.raises(ConfigValidationError):
        write_vault_config_cas(
            vault,
            {**snapshot.values, "history_retention_days": _CHANGED_RETENTION},
            expected_hash=snapshot.content_hash,
        )
    assert config.read_text(encoding="utf-8") == "vault_id: [unclosed\n"


def test_preserved_unknown_key_does_not_block_a_save(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _write_vault_yaml(
        vault,
        yaml.safe_dump({"schema_version": 1, "vault_id": _KEPT_VAULT_ID, "x_team_note": "keep"}),
    )

    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "--vault",
            str(vault),
            "--values-json",
            json.dumps({"history_retention_days": _CHANGED_RETENTION}),
        ],
    )

    assert result.exit_code == 0, result.output
    persisted = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert persisted["x_team_note"] == "keep"
    assert persisted["history_retention_days"] == _CHANGED_RETENTION


def test_companion_set_refuses_invalid_profile_instead_of_enabling_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "companion.json"
    profile.write_text(
        json.dumps({"ui_language": "xx", "enable_write": False, "vault_path": "V"}),
        encoding="utf-8",
    )
    before = profile.read_bytes()
    _set_env(monkeypatch, DATACRON_COMPANION_CONFIG=str(profile))

    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "--target",
            "companion",
            "--values-json",
            json.dumps({"cli_timeout_seconds": _TIMEOUT_SECONDS}),
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert profile.read_bytes() == before


# ---------------------------------------------------------------------------
# D-3: only the changed keys reach VAULT.yaml
# ---------------------------------------------------------------------------


def test_config_set_persists_only_the_changed_key(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _write_vault_yaml(
        vault,
        yaml.safe_dump({"schema_version": 1, "vault_id": _KEPT_VAULT_ID}, sort_keys=False),
    )

    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "--vault",
            str(vault),
            "--values-json",
            json.dumps({"history_retention_days": _CHANGED_RETENTION}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "vault_id": _KEPT_VAULT_ID,
        "history_retention_days": _CHANGED_RETENTION,
    }


def test_echoed_snapshot_does_not_freeze_model_defaults(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _write_vault_yaml(
        vault,
        yaml.safe_dump(
            {
                "schema_version": 1,
                "vault_id": _KEPT_VAULT_ID,
                "created": _KEPT_CREATED,
                "history_retention_days": _KEPT_RETENTION,
            },
            sort_keys=False,
        ),
    )
    snapshot = read_vault_config_snapshot(vault)

    written = write_vault_config_cas(
        vault,
        {**snapshot.values, "history_retention_days": _ECHO_RETENTION},
        expected_hash=snapshot.content_hash,
    )

    persisted: dict[str, Any] = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert persisted == {
        "schema_version": 1,
        "vault_id": _KEPT_VAULT_ID,
        "created": _KEPT_CREATED,
        "history_retention_days": _ECHO_RETENTION,
    }
    assert written.values["history_retention_days"] == _ECHO_RETENTION
    assert written.values["vault_id"] == _KEPT_VAULT_ID


# ---------------------------------------------------------------------------
# D-4 / E-2: the environment memory root belongs to the environment vault
# ---------------------------------------------------------------------------


def test_explicit_vault_drops_foreign_environment_memory_and_write_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_vault = tmp_path / "env-vault"
    env_memory = tmp_path / "env-memory"
    other = tmp_path / "other"
    for root in (env_vault, env_memory, other):
        root.mkdir()
    _set_env(
        monkeypatch,
        DATACRON_VAULT_ROOT=str(env_vault),
        DATACRON_MEMORY_ROOT=str(env_memory),
        DATACRON_WRITE_PATHS=f"{env_memory / '_memory'};{env_vault / '_drafts'}",
    )
    settings = get_settings()

    foreign = _settings_for_cli_vault(settings, other.resolve())
    assert foreign.memory_root is None
    assert foreign.write_paths == [other.resolve()]

    own = _settings_for_cli_vault(settings, env_vault.resolve())
    assert own.memory_root == env_memory.resolve()
    assert own.write_paths == settings.write_paths

    explicit = _settings_for_cli_vault(settings, other.resolve(), memory_root=env_memory)
    assert explicit.memory_root == env_memory.resolve()


@pytest.mark.parametrize("command", ["index", "reindex"])
def test_index_on_another_vault_leaves_the_environment_memory_vault_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    env_memory = tmp_path / "env-memory"
    env_memory.mkdir()
    other = tmp_path / "other"
    initialize_vault(other)
    (other / "note.md").write_text("# Note\n\nBody.\n", encoding="utf-8")
    _set_env(monkeypatch, DATACRON_MEMORY_ROOT=str(env_memory))

    result = runner.invoke(app, [command, "--vault", str(other)])

    assert result.exit_code == 0, result.output
    assert not (env_memory / ".datacron").exists()
    assert any((other / ".datacron" / "index").iterdir())


def test_setup_ignores_environment_memory_root_of_another_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_memory = tmp_path / "env-memory"
    env_memory.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _set_env(monkeypatch, DATACRON_MEMORY_ROOT=str(env_memory))

    resolved = _prompt_memory_vault(
        None,
        vault_root=other.resolve(),
        dual_vault=False,
        assume_yes=True,
    )

    assert resolved is None


# ---------------------------------------------------------------------------
# D-8 / E-3: graph-health and compact-memory accept --vault
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["graph-health", "compact-memory"])
def test_vault_option_is_accepted(tmp_path: Path, command: str) -> None:
    vault = tmp_path / "vault"
    initialize_vault(vault)

    result = runner.invoke(app, [command, "--vault", str(vault), "--json"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# M (CLI part): migrate-memory defaults
# ---------------------------------------------------------------------------


def _capture_migration(
    monkeypatch: pytest.MonkeyPatch,
    current_write: tuple[bool, bool] | None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_existing(
        vault_root: Path, client: str, install_scope: str
    ) -> tuple[bool, bool] | None:
        return current_write

    async def fake_migrate(
        vault_root: Path,
        memory_root: Path,
        **kwargs: Any,
    ) -> MigrationResult:
        captured.update(kwargs)
        return MigrationResult(
            vault_path=vault_root,
            memory_vault_path=memory_root,
            copied_sidecar_files=0,
            copied_memory_files=0,
            verified_files=0,
            indexed_notes=0,
            indexed_chunks=0,
            source_cleaned=not kwargs["keep_source"],
        )

    monkeypatch.setattr(cli_module, "existing_client_settings", fake_existing)
    monkeypatch.setattr(migration_module, "migrate_vault_to_dual", fake_migrate)
    return captured


@pytest.mark.parametrize(
    ("current_write", "expected_write"), [(None, False), ((True, False), True)]
)
def test_migrate_memory_defaults_reconfigure_clients_and_keep_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_write: tuple[bool, bool] | None,
    expected_write: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    captured = _capture_migration(monkeypatch, current_write)

    result = runner.invoke(
        app,
        ["migrate-memory", "--vault", str(vault), "--memory-vault", str(tmp_path / "memory")],
    )

    assert result.exit_code == 0, result.output
    assert captured["client"] == "all"
    assert captured["install_scope"] == "user"
    assert captured["keep_source"] is True
    assert captured["enable_write"] is expected_write


def test_migrate_memory_cleans_the_source_only_on_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    captured = _capture_migration(monkeypatch, None)

    result = runner.invoke(
        app,
        [
            "migrate-memory",
            "--vault",
            str(vault),
            "--memory-vault",
            str(tmp_path / "memory"),
            "--cleanup-source",
            "--client",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["keep_source"] is False
    assert captured["client"] == "none"


# ---------------------------------------------------------------------------
# E-4: a mistyped vault path is refused, not created
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["index", "--vault"],
        ["config", "set", "--values-json", '{"history_retention_days": 60}', "--vault"],
    ],
)
def test_missing_vault_path_is_refused_without_creating_it(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    missing = tmp_path / "typo"

    result = runner.invoke(app, [*arguments, str(missing)])

    assert result.exit_code == _EXIT_USAGE, result.output
    assert not missing.exists()


# ---------------------------------------------------------------------------
# E-5: error paths end in one line, not a traceback
# ---------------------------------------------------------------------------


def test_eval_with_malformed_questions_reports_one_line(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    initialize_vault(vault)
    (vault / "note.md").write_text("# Note\n\nBody.\n", encoding="utf-8")
    assert runner.invoke(app, ["index", "--vault", str(vault)]).exit_code == 0
    questions = tmp_path / "questions.yaml"
    questions.write_text("[unclosed\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["eval", "--vault", str(vault), "--questions", str(questions)],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception


def test_scrub_without_an_index_reports_one_line(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    initialize_vault(vault)

    result = runner.invoke(app, ["scrub", "--vault", str(vault)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception


def test_ops_inspect_on_a_missing_vault_reports_one_line(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ops", "inspect", "--vault", str(tmp_path / "missing")])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
