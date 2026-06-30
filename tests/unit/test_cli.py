# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.cli`."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.paths import (
    sidecar_dir,
    sidecar_index_db,
    sidecar_index_dir,
    sidecar_vault_config,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


async def _create_empty_index(db_path: Path) -> None:
    from datacron.indexing.fts5_store import SQLiteFTS5Store

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()


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
        assert config["excluded_folders"] == [
            "_attachments",
            "zzz_Corbeille",
            "_trash",
            "_archive",
        ]
        assert config["excluded_files"] == ["00_INDEX.md"]
        assert config["query_expansion"]["supervision"] == ["monitoring"]

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
        assert "not built" in result.stdout

    def test_status_with_empty_index_file_reports_empty(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", str(vault)])
        (vault / "hello.md").write_text("# Hello\n", encoding="utf-8")
        asyncio.run(_create_empty_index(sidecar_index_db(vault)))

        result = runner.invoke(app, ["status", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout
        assert "notes:      1" in result.stdout
        assert "index:      empty" in result.stdout
        assert "run `datacron index`" in result.stdout
        assert "built (" not in result.stdout

    def test_status_after_index_reports_counts(self, runner: CliRunner, tmp_vault: Path) -> None:
        indexed = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert indexed.exit_code == 0, indexed.stdout + indexed.stderr

        result = runner.invoke(app, ["status", "--vault", str(tmp_vault)])

        assert result.exit_code == 0, result.stdout
        assert "index:      built (6 notes," in result.stdout
        assert " chunks)" in result.stdout


class TestStubs:
    """Commands still pending in Sem 4. ``index`` / ``reindex`` / ``mcp install``
    moved to their own test classes once they were wired in Sem 3."""

    @pytest.mark.parametrize("cmd", [["ask", "anything"]])
    def test_stub_exits_with_error(self, runner: CliRunner, cmd: list[str]) -> None:
        result = runner.invoke(app, cmd)
        assert result.exit_code == 1
        assert "not implemented" in result.stderr.lower() or "not implemented" in (
            result.stdout.lower()
        )


class TestIndex:
    def test_index_builds_fts5_store_on_demo_vault(
        self, runner: CliRunner, tmp_vault: Path
    ) -> None:
        result = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert result.exit_code == 0, result.stdout + result.stderr

        from datacron.core.paths import sidecar_index_db

        db_path = sidecar_index_db(tmp_vault)
        assert db_path.is_file(), "index() must create the FTS5 database"
        # The demo vault has 6 notes; the stdout summary should reflect that.
        assert "Indexed 6 notes" in result.stdout

    def test_reindex_drops_then_rebuilds(self, runner: CliRunner, tmp_vault: Path) -> None:
        from datacron.core.paths import sidecar_index_db

        db_path = sidecar_index_db(tmp_vault)
        # First build
        first = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert first.exit_code == 0
        first_size = db_path.stat().st_size

        # Touch a marker file to confirm the parent dir survives the rebuild.
        marker = db_path.parent / "marker.txt"
        marker.write_text("kept", encoding="utf-8")

        second = runner.invoke(app, ["reindex", "--vault", str(tmp_vault)])
        assert second.exit_code == 0
        assert db_path.is_file()
        assert marker.is_file(), "reindex must not touch unrelated files"
        # File size after rebuild should be sane (non-zero, comparable to first build).
        assert db_path.stat().st_size > 0
        # Within an order of magnitude of the first build.
        assert db_path.stat().st_size <= first_size * 4

    def test_index_uses_vault_excluded_folders(self, runner: CliRunner, tmp_vault: Path) -> None:
        from datacron.core.paths import sidecar_index_db, sidecar_vault_config

        initialized = runner.invoke(app, ["init", str(tmp_vault)])
        assert initialized.exit_code == 0, initialized.stdout + initialized.stderr
        config_path = sidecar_vault_config(tmp_vault)
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["excluded_folders"] = ["custom-trash"]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        trash = tmp_vault / "custom-trash"
        trash.mkdir()
        (trash / "ignored.md").write_text("# ignored", encoding="utf-8")

        indexed = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert indexed.exit_code == 0, indexed.stdout + indexed.stderr

        with sqlite3.connect(sidecar_index_db(tmp_vault)) as connection:
            rel_paths = {row[0] for row in connection.execute("SELECT rel_path FROM notes")}

        assert "custom-trash/ignored.md" not in rel_paths

    def test_index_uses_vault_excluded_files(self, runner: CliRunner, tmp_vault: Path) -> None:
        from datacron.core.paths import sidecar_index_db, sidecar_vault_config

        initialized = runner.invoke(app, ["init", str(tmp_vault)])
        assert initialized.exit_code == 0, initialized.stdout + initialized.stderr
        config_path = sidecar_vault_config(tmp_vault)
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["excluded_files"] = ["00_INDEX.md"]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        (tmp_vault / "00_INDEX.md").write_text("# ignored", encoding="utf-8")
        nested = tmp_vault / "nested-index"
        nested.mkdir()
        (nested / "00_INDEX.md").write_text("# also ignored", encoding="utf-8")
        (nested / "kept.md").write_text("# kept", encoding="utf-8")

        indexed = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert indexed.exit_code == 0, indexed.stdout + indexed.stderr

        with sqlite3.connect(sidecar_index_db(tmp_vault)) as connection:
            rel_paths = {row[0] for row in connection.execute("SELECT rel_path FROM notes")}

        assert "nested-index/kept.md" in rel_paths
        assert "00_INDEX.md" not in rel_paths
        assert "nested-index/00_INDEX.md" not in rel_paths


class TestEval:
    def test_eval_runs_against_existing_index(
        self, runner: CliRunner, tmp_vault: Path, tmp_path: Path
    ) -> None:
        indexed = runner.invoke(app, ["index", "--vault", str(tmp_vault)])
        assert indexed.exit_code == 0, indexed.stdout + indexed.stderr

        questions = tmp_path / "eval-questions.yaml"
        questions.write_text(
            """
- id: q-welcome
  question: welcome
  expected_paths:
    - welcome.md
""".lstrip(),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "eval",
                "--vault",
                str(tmp_vault),
                "--questions",
                str(questions),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Datacron Eval Summary" in result.stdout
        assert "q-welcome" in result.stdout

    def test_eval_requires_existing_index(
        self, runner: CliRunner, tmp_vault: Path, tmp_path: Path
    ) -> None:
        questions = tmp_path / "eval-questions.yaml"
        questions.write_text("[]\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "eval",
                "--vault",
                str(tmp_vault),
                "--questions",
                str(questions),
            ],
        )

        assert result.exit_code == 1
        assert "No index found" in result.stdout


class TestMcpInstall:
    def test_install_writes_config_with_explicit_path(
        self, runner: CliRunner, tmp_vault: Path, tmp_path: Path
    ) -> None:
        config_target = tmp_path / "claude_desktop_config.json"
        result = runner.invoke(
            app,
            [
                "mcp",
                "install",
                "--client",
                "claude-desktop",
                "--vault",
                str(tmp_vault),
                "--config-path",
                str(config_target),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Wrote Datacron MCP entry" in result.stdout
        assert config_target.is_file()

        import json

        data = json.loads(config_target.read_text(encoding="utf-8"))
        # CLI does not expose --command, so the installer resolves an absolute
        # path (PATH lookup, then current venv's Scripts/ dir). The exact path
        # depends on the test runner's environment; assert only that it ends
        # with the expected binary name and is absolute.
        command = data["mcpServers"]["datacron"]["command"]
        expected_binary = "datacron-mcp.exe" if sys.platform == "win32" else "datacron-mcp"
        assert Path(command).is_absolute()
        assert Path(command).name == expected_binary
        assert data["mcpServers"]["datacron"]["env"]["DATACRON_VAULT_ROOT"] == str(
            tmp_vault.resolve()
        )

    def test_install_rejects_unknown_client(self, runner: CliRunner, tmp_vault: Path) -> None:
        result = runner.invoke(
            app,
            [
                "mcp",
                "install",
                "--client",
                "cursor",
                "--vault",
                str(tmp_vault),
            ],
        )
        assert result.exit_code == 1
        assert "Unknown client" in result.stderr


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
