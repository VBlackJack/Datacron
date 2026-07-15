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
"""Safety and CLI tests for the surgical Datacron setup reset."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from datacron import setup_wizard
from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.core.config import INDEX_DIR_NAME, SIDECAR_DIR_NAME, VAULT_CONFIG_FILENAME
from datacron.core.paths import sidecar_index_dir, sidecar_vault_config
from datacron.setup_wizard import (
    ResetExecutionError,
    ResetGuardError,
    ResetResult,
    reset_user_state,
)

_runner = CliRunner()


def _canonical_reset_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    vault = tmp_path / "vault"
    sidecar = vault / SIDECAR_DIR_NAME
    config = sidecar / VAULT_CONFIG_FILENAME
    index = sidecar / INDEX_DIR_NAME
    index.mkdir(parents=True)
    config.write_text("vault_id: fixture\n", encoding="utf-8")
    return vault, sidecar, config, index


def test_guard_accepts_canonical_reset_paths(tmp_path: Path) -> None:
    vault, _sidecar, config, index = _canonical_reset_paths(tmp_path)

    guarded_config, guarded_index = setup_wizard._guard_reset_paths(vault, config, index)

    assert guarded_config == config
    assert guarded_index == index


@pytest.mark.parametrize(
    ("config_relative", "index_relative"),
    [
        (Path(SIDECAR_DIR_NAME) / "settings.yaml", Path(SIDECAR_DIR_NAME) / INDEX_DIR_NAME),
        (Path(SIDECAR_DIR_NAME) / VAULT_CONFIG_FILENAME, Path(SIDECAR_DIR_NAME) / "search"),
        (Path(VAULT_CONFIG_FILENAME), Path(SIDECAR_DIR_NAME) / INDEX_DIR_NAME),
        (Path(SIDECAR_DIR_NAME) / VAULT_CONFIG_FILENAME, Path(INDEX_DIR_NAME)),
        (Path(SIDECAR_DIR_NAME) / VAULT_CONFIG_FILENAME, Path(".")),
    ],
)
def test_guard_rejects_noncanonical_target_shape(
    tmp_path: Path,
    config_relative: Path,
    index_relative: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(ResetGuardError, match="Invalid Datacron"):
        setup_wizard._guard_reset_paths(
            vault,
            vault / config_relative,
            vault / index_relative,
        )


def test_guard_rejects_sidecar_not_directly_under_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    nested_sidecar = vault / "nested" / SIDECAR_DIR_NAME
    monkeypatch.setattr(setup_wizard, "SIDECAR_DIR_NAME", f"nested/{SIDECAR_DIR_NAME}")

    with pytest.raises(ResetGuardError, match="sidecar"):
        setup_wizard._guard_reset_paths(
            vault,
            nested_sidecar / VAULT_CONFIG_FILENAME,
            nested_sidecar / INDEX_DIR_NAME,
        )


@pytest.mark.parametrize("linked_label", ["vault", "sidecar", "config", "index"])
def test_guard_rejects_a_link_anywhere_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_label: str,
) -> None:
    vault, sidecar, config, index = _canonical_reset_paths(tmp_path)
    paths = {"vault": vault, "sidecar": sidecar, "config": config, "index": index}
    index_marker = index / "datacron.db"
    index_marker.write_bytes(b"index")
    config_before = config.read_bytes()
    note = vault / "source.md"
    note.write_bytes(b"# Source of truth\n")

    monkeypatch.setattr(
        setup_wizard,
        "_is_linked_path",
        lambda path: path == paths[linked_label],
    )

    with pytest.raises(ResetGuardError, match="symlink or reparse point"):
        reset_user_state(vault, config_path=config, index_dir=index)

    assert config.read_bytes() == config_before
    assert index_marker.read_bytes() == b"index"
    assert note.read_bytes() == b"# Source of truth\n"


def test_is_linked_path_detects_real_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert setup_wizard._is_linked_path(link) is True


def test_is_linked_path_detects_windows_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_stat = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=setup_wizard._FILE_ATTRIBUTE_REPARSE_POINT,
    )
    monkeypatch.setattr(os, "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(sys, "platform", "win32")

    assert setup_wizard._is_linked_path(tmp_path / "junction") is True


def test_is_linked_path_fails_closed_without_windows_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        os,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(ResetGuardError, match="Cannot read file attributes"):
        setup_wizard._is_linked_path(tmp_path / "unknown")


@pytest.mark.parametrize("wrong_type", ["vault", "sidecar", "index", "config"])
def test_guard_rejects_wrong_types(tmp_path: Path, wrong_type: str) -> None:
    vault = tmp_path / "vault"
    sidecar = vault / SIDECAR_DIR_NAME
    config = sidecar / VAULT_CONFIG_FILENAME
    index = sidecar / INDEX_DIR_NAME

    if wrong_type == "vault":
        vault.write_text("not a directory", encoding="utf-8")
    elif wrong_type == "sidecar":
        vault.mkdir()
        sidecar.write_text("not a directory", encoding="utf-8")
    else:
        sidecar.mkdir(parents=True)
        if wrong_type == "index":
            index.write_text("not a directory", encoding="utf-8")
        else:
            config.mkdir()

    with pytest.raises(ResetGuardError):
        setup_wizard._guard_reset_paths(vault, config, index)


def test_reset_removes_only_config_and_index(tmp_path: Path) -> None:
    vault, sidecar, config, index = _canonical_reset_paths(tmp_path)
    preserved_files = {
        vault / "root.md": b"# Root\n",
        vault / "notes" / "nested.md": b"# Nested\n",
        sidecar / "ulids.json": b'{"root.md":"01"}',
        sidecar / "ulids.json.migrated": b'{"nested.md":"02"}',
        sidecar / "history" / "blob": b"history",
        sidecar / "oplog" / "pending" / "operation.json": b"operation",
        sidecar / "scrubber" / "state.json": b"scrubber",
        sidecar / "logs" / "setup.log": b"log",
    }
    for path, content in preserved_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (index / "datacron.db").write_bytes(b"generated index")

    result = reset_user_state(vault)

    assert result == ResetResult(config_removed=True, index_removed=True)
    assert not config.exists()
    assert not index.exists()
    for path, content in preserved_files.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("sidecar_exists", [False, True])
def test_reset_is_idempotent_when_targets_are_absent(tmp_path: Path, sidecar_exists: bool) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    if sidecar_exists:
        (vault / SIDECAR_DIR_NAME).mkdir()

    assert reset_user_state(vault) == ResetResult(config_removed=False, index_removed=False)


def test_reset_wraps_index_deletion_error_without_partial_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _sidecar, config, index = _canonical_reset_paths(tmp_path)
    marker = index / "datacron.db"
    marker.write_bytes(b"locked")
    config_before = config.read_bytes()
    note = vault / "source.md"
    note.write_bytes(b"# Source of truth\n")

    def fail_rmtree(_path: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "rmtree", fail_rmtree)

    with pytest.raises(ResetExecutionError) as caught:
        reset_user_state(vault)

    message = str(caught.value)
    assert "Close all AI clients" in message
    assert str(index) in message
    assert marker.read_bytes() == b"locked"
    assert config.read_bytes() == config_before
    assert note.read_bytes() == b"# Source of truth\n"


def test_reset_wraps_config_deletion_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _sidecar, config, index = _canonical_reset_paths(tmp_path)
    index.rmdir()
    real_unlink = Path.unlink

    def fail_config_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == config:
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_config_unlink)

    with pytest.raises(ResetExecutionError) as caught:
        reset_user_state(vault)

    assert "Close all AI clients" in str(caught.value)
    assert str(config) in str(caught.value)
    assert config.is_file()


def test_setup_reset_yes_resets_then_reinitializes_without_touching_notes(tmp_path: Path) -> None:
    initialize_vault(tmp_path)
    config = sidecar_vault_config(tmp_path)
    index = sidecar_index_dir(tmp_path)
    config.write_text("vault_id: old\nquery_expansion:\n  old: [setting]\n", encoding="utf-8")
    old_config = config.read_bytes()
    (index / "datacron.db").write_bytes(b"old index")
    note = tmp_path / "source.md"
    note.write_bytes(b"# Source of truth\n")

    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--reset",
            "--yes",
            "--client",
            "none",
            "--no-index",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "reset:      config removed / index removed" in result.output
    assert config.is_file()
    assert config.read_bytes() != old_config
    assert index.is_dir()
    assert list(index.iterdir()) == []
    assert note.read_bytes() == b"# Source of truth\n"


def test_setup_reset_refusal_makes_no_changes(tmp_path: Path) -> None:
    initialize_vault(tmp_path)
    config = sidecar_vault_config(tmp_path)
    index_marker = sidecar_index_dir(tmp_path) / "datacron.db"
    index_marker.write_bytes(b"old index")
    note = tmp_path / "source.md"
    note.write_bytes(b"# Source of truth\n")
    config_before = config.read_bytes()

    result = _runner.invoke(
        app,
        ["setup", "--vault", str(tmp_path), "--reset", "--client", "none", "--no-index"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Reset cancelled; nothing changed." in result.output
    assert config.read_bytes() == config_before
    assert index_marker.read_bytes() == b"old index"
    assert note.read_bytes() == b"# Source of truth\n"


@pytest.mark.parametrize("error_type", [ResetGuardError, ResetExecutionError])
def test_setup_reset_reports_safety_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[RuntimeError],
) -> None:
    def fail_reset(_vault: Path) -> ResetResult:
        raise error_type("safe reset failure")

    monkeypatch.setattr(setup_wizard, "reset_user_state", fail_reset)

    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--reset",
            "--yes",
            "--client",
            "none",
            "--no-index",
        ],
    )

    assert result.exit_code == 1
    assert "safe reset failure" in result.output


def test_reset_preserves_unknown_sidecar_children(tmp_path: Path) -> None:
    vault, sidecar, _config, _index = _canonical_reset_paths(tmp_path)
    future_file = sidecar / "future-thing.dat"
    future_nested = sidecar / "future-dir" / "payload.bin"
    future_file.write_bytes(b"future file")
    future_nested.parent.mkdir()
    future_nested.write_bytes(b"future directory")

    reset_user_state(vault)

    assert future_file.read_bytes() == b"future file"
    assert future_nested.read_bytes() == b"future directory"


def test_guard_fails_closed_on_permission_error_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _sidecar, config, index = _canonical_reset_paths(tmp_path)
    index_marker = index / "datacron.db"
    index_marker.write_bytes(b"index")
    config_before = config.read_bytes()
    note = vault / "source.md"
    note.write_bytes(b"# Source of truth\n")
    real_lstat = os.lstat

    def denied_lstat(path: os.PathLike[str] | str) -> os.stat_result:
        if Path(path) == config:
            raise PermissionError("denied")
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", denied_lstat)

    with pytest.raises(ResetGuardError, match="Cannot inspect reset target"):
        reset_user_state(vault)

    assert config.read_bytes() == config_before
    assert index_marker.read_bytes() == b"index"
    assert note.read_bytes() == b"# Source of truth\n"


def test_missing_target_is_not_an_inspection_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert setup_wizard._is_linked_path(missing) is False
