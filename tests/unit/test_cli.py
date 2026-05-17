# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.cli`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.paths import sidecar_dir, sidecar_index_dir, sidecar_vault_config


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestInit:
    def test_creates_sidecar(self, runner: CliRunner, tmp_path: Path) -> None:
        vault = tmp_path / "my-vault"
        result = runner.invoke(app, ["init", str(vault)])
        assert result.exit_code == 0, result.stdout
        assert sidecar_dir(vault).is_dir()
        assert sidecar_index_dir(vault).is_dir()
        assert sidecar_vault_config(vault).is_file()

        config = yaml.safe_load(sidecar_vault_config(vault).read_text(encoding="utf-8"))
        assert len(config["vault_id"]) == 26
        assert config["encoding"] == "utf-8"
        assert config["line_endings"] == "lf"
        assert config["folders"]["drafts"] == "_drafts"

    def test_init_refuses_overwrite_without_force(self, runner: CliRunner, tmp_path: Path) -> None:
        vault = tmp_path / "my-vault"
        first = runner.invoke(app, ["init", str(vault)])
        assert first.exit_code == 0
        original = sidecar_vault_config(vault).read_text(encoding="utf-8")

        second = runner.invoke(app, ["init", str(vault)])
        assert second.exit_code == 0
        assert sidecar_vault_config(vault).read_text(encoding="utf-8") == original

    def test_init_with_force_overwrites(self, runner: CliRunner, tmp_path: Path) -> None:
        vault = tmp_path / "my-vault"
        runner.invoke(app, ["init", str(vault)])
        original = sidecar_vault_config(vault).read_text(encoding="utf-8")
        result = runner.invoke(app, ["init", str(vault), "--force"])
        assert result.exit_code == 0
        assert sidecar_vault_config(vault).read_text(encoding="utf-8") != original


class TestStatus:
    def test_status_uninitialized(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["status", "--vault", str(tmp_path)])
        assert result.exit_code == 0
        assert "no (run `datacron init`)" in result.stdout
        assert "not built" in result.stdout

    def test_status_after_init(self, runner: CliRunner, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", str(vault)])
        # Add a markdown file
        (vault / "hello.md").write_text("# Hello\n", encoding="utf-8")

        result = runner.invoke(app, ["status", "--vault", str(vault)])
        assert result.exit_code == 0, result.stdout
        assert "initialized: yes" in result.stdout
        assert "notes:      1" in result.stdout


class TestStubs:
    """Commands still pending in Sem 3-4. ``mcp serve`` moved to TestMcpServe
    once it was wired in Sem 2."""

    @pytest.mark.parametrize(
        "cmd",
        [
            ["index"],
            ["reindex"],
            ["ask", "anything"],
            ["eval", "--questions", "nope.yaml"],
            ["mcp", "install", "--client", "claude-desktop"],
        ],
    )
    def test_stub_exits_with_error(self, runner: CliRunner, cmd: list[str]) -> None:
        result = runner.invoke(app, cmd)
        assert result.exit_code == 1
        assert "not implemented" in result.stderr.lower() or "not implemented" in (
            result.stdout.lower()
        )


class TestMcpServe:
    """`datacron mcp serve` is wired in Sem 2. Spinning the full stdio loop
    requires an MCP client on the other end of the pipe, which lives in the
    integration tests (tests/integration/test_mcp_e2e.py). Here we only
    assert that the dispatcher resolves the vault and would launch the
    server (no vault → exit 1 with a clear message)."""

    def test_serve_without_vault_fails_clean(self, runner: CliRunner, tmp_path: Path) -> None:
        # tmp_path is empty: no .datacron/VAULT.yaml, no env var, no --vault.
        # The dispatcher must refuse instead of starting the server.
        result = runner.invoke(app, ["mcp", "serve", "--vault", str(tmp_path / "missing")])
        assert result.exit_code != 0
        combined = (result.stdout + result.stderr).lower()
        assert "vault" in combined or "not found" in combined
