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

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from datacron.cli import app
from datacron.core.config import reset_settings_cache
from datacron.core.paths import sidecar_vault_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORGANIZATION = {
    "organization": {
        "scope": "_memory",
        "rules": [
            {"tag": "memory/decision", "folder": "_memory/decisions", "naming": "{date}-{slug}"},
            {"tag": "memory/fact", "folder": "_memory/facts", "naming": "{date}-{slug}"},
        ],
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
    payload: dict[str, object] = {"vault_id": "test-vault"}
    if with_rules:
        payload.update(_ORGANIZATION)
        (root / "_memory").mkdir(parents=True, exist_ok=True)
    _write_config(root, payload)
    return root


def _write_config(root: Path, payload: object) -> None:
    config_path = sidecar_vault_config(root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _write_note(root: Path, rel_path: str, tag: str, *, created: str = "2026-08-29") -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: note\ncreated: {created}\ntags:\n  - {tag}\n---\n\nbody\n",
        encoding="utf-8",
    )


def _assert_configuration_error(result: Result) -> None:
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr
    assert "Traceback" not in result.output


def _create_directory_link(link: Path, target: Path) -> None:
    """Create a directory symlink, using an NTFS junction when needed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.fail(f"directory symlink creation failed: {exc}")
    command_shell = os.environ.get("COMSPEC")
    assert command_shell is not None, "COMSPEC is required to create an NTFS junction"
    process = subprocess.run(
        [command_shell, "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def _manifest(root: Path) -> tuple[tuple[str, str, int, str | None, int], ...]:
    """Capture the recursive byte and mtime contract for a vault."""
    entries: list[tuple[str, str, int, str | None, int]] = []
    paths = [root, *root.rglob("*")]
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        kind = "file" if path.is_file() else "directory"
        entries.append(
            (path.relative_to(root).as_posix(), kind, stat.st_size, digest, stat.st_mtime_ns)
        )
    return tuple(entries)


def _subprocess_env(log_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["DATACRON_LOG_DIR"] = str(log_dir)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(_REPO_ROOT / "src")
    environment.pop("DATACRON_VAULT_ROOT", None)
    return environment


def _run_cli(arguments: list[str], *, log_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "datacron.cli", *arguments],
        cwd=_REPO_ROOT,
        env=_subprocess_env(log_dir),
        check=False,
        capture_output=True,
        text=True,
    )


def test_dry_run_is_mandatory(runner: CliRunner, tmp_path: Path) -> None:
    """The flag must stay explicit so no habit of running without it forms."""
    _make_vault(tmp_path)

    result = runner.invoke(app, ["reorganize", "--vault", str(tmp_path)])

    _assert_configuration_error(result)
    assert "--dry-run" in result.stderr


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
    assert "WRONG_FOLDER" in result.stdout
    assert "_memory/decisions" in result.stdout


def test_json_output_is_valid_and_stable(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/2026-08-29-misplaced.md", "memory/decision")

    first = runner.invoke(app, ["reorganize", "--dry-run", "--json", "--vault", str(tmp_path)])
    second = runner.invoke(app, ["reorganize", "--dry-run", "--json", "--vault", str(tmp_path)])

    assert first.exit_code == 1
    payload = json.loads(first.stdout)
    assert payload["schema"] == "organization-plan-v1"
    assert payload["scope"] == "_memory"
    assert payload["counts"]["WRONG_FOLDER"] == 1
    assert first.stdout == second.stdout


def test_kind_filter_narrows_the_report(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    _write_note(tmp_path, "_memory/facts/undated.md", "memory/fact")

    filtered = runner.invoke(
        app,
        ["reorganize", "--dry-run", "--json", "--kind", "WRONG_FOLDER", "--vault", str(tmp_path)],
    )

    assert filtered.exit_code == 0
    assert json.loads(filtered.stdout)["deviations"] == []


@pytest.mark.parametrize(
    "case",
    ["unknown-kind", "missing-vault", "missing-sidecar", "sidecar-directory"],
)
def test_basic_configuration_errors_are_code_two(
    runner: CliRunner,
    tmp_path: Path,
    case: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    arguments = ["reorganize", "--dry-run", "--vault", str(vault)]
    if case == "unknown-kind":
        arguments[2:2] = ["--kind", "NOPE"]
    elif case == "missing-vault":
        arguments[-1] = str(tmp_path / "absent")
    elif case == "missing-sidecar":
        pass
    elif case == "sidecar-directory":
        sidecar_vault_config(vault).mkdir(parents=True)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    result = runner.invoke(app, arguments)

    _assert_configuration_error(result)


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("invalid-yaml", None),
        ("non-mapping-yaml", ["not", "a", "mapping"]),
        ("false-yaml", False),
        ("null-yaml", None),
        ("empty-yaml", None),
        ("invalid-pydantic", None),
        ("invalid-list-type", None),
        ("missing-scope", None),
    ],
)
def test_invalid_sidecar_content_is_code_two(
    runner: CliRunner,
    tmp_path: Path,
    case: str,
    payload: object,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    if case == "invalid-yaml":
        config_path = sidecar_vault_config(vault)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("organization: [unterminated", encoding="utf-8")
    elif case in {"non-mapping-yaml", "false-yaml", "null-yaml"}:
        _write_config(vault, payload)
    elif case == "empty-yaml":
        config_path = sidecar_vault_config(vault)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("", encoding="utf-8")
    elif case == "invalid-pydantic":
        _write_config(
            vault,
            {
                "organization": {
                    "scope": "_memory",
                    "rules": [{"tag": "memory/fact", "foldr": "_memory/facts"}],
                }
            },
        )
    elif case == "invalid-list-type":
        _write_config(vault, {"excluded_folders": "not-a-list"})
    elif case == "missing-scope":
        _write_config(
            vault,
            {"organization": {"rules": [{"tag": "memory/fact", "folder": "_memory/facts"}]}},
        )
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(vault)])

    _assert_configuration_error(result)


@pytest.mark.parametrize(
    ("scope", "folder"),
    [("../escape", "_memory/facts"), ("_memory", "../escape")],
)
def test_escaped_declared_paths_are_code_two(
    runner: CliRunner,
    tmp_path: Path,
    scope: str,
    folder: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_config(
        vault,
        {
            "organization": {
                "scope": scope,
                "rules": [{"tag": "memory/fact", "folder": folder}],
            }
        },
    )

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(vault)])

    _assert_configuration_error(result)


@pytest.mark.parametrize(
    "case",
    ["missing-scope-directory", "scope-file", "target-file", "target-outside-scope"],
)
def test_invalid_runtime_scope_or_target_is_code_two(
    runner: CliRunner,
    tmp_path: Path,
    case: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    if case == "missing-scope-directory":
        _write_config(vault, _ORGANIZATION)
    elif case == "scope-file":
        (vault / "_memory").write_text("not a directory", encoding="utf-8")
        _write_config(vault, _ORGANIZATION)
    elif case == "target-file":
        (vault / "_memory").mkdir()
        (vault / "_memory" / "facts").write_text("not a directory", encoding="utf-8")
        _write_config(vault, _ORGANIZATION)
    elif case == "target-outside-scope":
        (vault / "_memory").mkdir()
        payload = {
            "organization": {
                "scope": "_memory",
                "rules": [{"tag": "memory/fact", "folder": "other/facts"}],
            }
        }
        _write_config(vault, payload)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(vault)])

    _assert_configuration_error(result)


def test_no_discoverable_vault_is_code_two(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reorganize", "--dry-run"])

    _assert_configuration_error(result)


def test_vault_expansion_failure_is_code_two(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_expanduser = Path.expanduser

    def fail_selected_path(path: Path) -> Path:
        if str(path) == "~unresolvable-user":
            raise RuntimeError("home directory is unavailable")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", fail_selected_path)

    result = runner.invoke(
        app,
        ["reorganize", "--dry-run", "--vault", "~unresolvable-user"],
    )

    _assert_configuration_error(result)


def test_vault_without_rules_reports_nothing_to_measure(runner: CliRunner, tmp_path: Path) -> None:
    _make_vault(tmp_path, with_rules=False)
    _write_note(tmp_path, "_memory/facts/undated.md", "memory/fact")

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(tmp_path)])

    assert result.exit_code == 0
    assert "nothing to measure" in result.stdout


@pytest.mark.parametrize("expected_code", [0, 1, 2])
def test_subprocess_is_byte_and_mtime_read_only_for_all_exit_families(
    tmp_path: Path,
    expected_code: int,
) -> None:
    vault = tmp_path / f"vault-{expected_code}"
    vault.mkdir()
    if expected_code == 0:
        _make_vault(vault)
        _write_note(vault, "_memory/facts/2026-08-29-clean.md", "memory/fact")
    elif expected_code == 1:
        _make_vault(vault)
        _write_note(vault, "_memory/facts/2026-08-29-wrong.md", "memory/decision")
    else:
        _write_config(
            vault,
            {"organization": {"rules": [{"tag": "memory/fact", "folder": "_memory/facts"}]}},
        )
    log_dir = vault / ".datacron" / "cli-logs"
    before = _manifest(vault)

    process = _run_cli(
        ["reorganize", "--dry-run", "--json", "--vault", str(vault)],
        log_dir=log_dir,
    )

    after = _manifest(vault)
    assert process.returncode == expected_code
    assert before == after
    assert not log_dir.exists()
    assert "Traceback" not in process.stderr
    if expected_code == 2:
        assert process.stdout == ""
        assert process.stderr


def test_ordinary_command_still_initializes_the_file_logger(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    log_dir = tmp_path / "ordinary-logs"

    process = _run_cli(["status", "--vault", str(vault)], log_dir=log_dir)

    assert process.returncode == 0, process.stderr
    assert log_dir.is_dir()
    assert list(log_dir.glob("datacron_*.log"))


@pytest.mark.parametrize("linked_path", ["scope", "target"])
def test_outgoing_scope_or_target_link_is_code_two(
    runner: CliRunner,
    tmp_path: Path,
    linked_path: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_path == "scope":
        _create_directory_link(vault / "_memory", outside)
    else:
        (vault / "_memory").mkdir()
        _create_directory_link(vault / "_memory" / "facts", outside)
    _write_config(vault, _ORGANIZATION)

    result = runner.invoke(app, ["reorganize", "--dry-run", "--vault", str(vault)])

    _assert_configuration_error(result)


def test_outgoing_non_rule_link_is_never_scanned(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault)
    outside = tmp_path / "outside"
    _write_note(outside, "valid.md", "memory/fact")
    _create_directory_link(vault / "_memory" / "external", outside)

    result = runner.invoke(
        app,
        ["reorganize", "--dry-run", "--json", "--vault", str(vault)],
    )

    assert result.exit_code == 0
    payload: Mapping[str, object] = json.loads(result.stdout)
    assert payload["scanned"] == 0
    assert payload["governed"] == 0
