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
"""Tests for the guided ``datacron setup`` command and its bootstrap primitives."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from datacron import setup_wizard
from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.installers.claude_desktop import ClaudeDesktopConfigError, MCPServerInvocation
from datacron.installers.mcp_clients import ClientTarget, InstallOutcome
from datacron.setup_wizard import (
    CLIENT_ALL,
    CLIENT_CLAUDE_CODE,
    CLIENT_CLAUDE_DESKTOP,
    CLIENT_NONE,
    SetupPlan,
    claude_code_stdio_config,
    run_setup,
)

_runner = CliRunner()


def _write_note(vault: Path, name: str, body: str) -> None:
    (vault / name).write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# bootstrap.initialize_vault
# ---------------------------------------------------------------------------


def test_initialize_vault_creates_sidecar(tmp_path: Path) -> None:
    result = initialize_vault(tmp_path)
    assert result.created is True
    assert result.vault_id is not None
    assert sidecar_vault_config(tmp_path).is_file()
    assert (result.sidecar_path / "index").is_dir()
    assert (result.sidecar_path / "logs").is_dir()


def test_initialize_vault_is_idempotent(tmp_path: Path) -> None:
    first = initialize_vault(tmp_path)
    original = sidecar_vault_config(tmp_path).read_text(encoding="utf-8")
    second = initialize_vault(tmp_path)
    assert second.created is False
    assert second.vault_id is None
    assert sidecar_vault_config(tmp_path).read_text(encoding="utf-8") == original
    assert first.vault_id is not None


def test_initialize_vault_force_overwrites(tmp_path: Path) -> None:
    first = initialize_vault(tmp_path)
    forced = initialize_vault(tmp_path, force=True)
    assert forced.created is True
    assert forced.vault_id != first.vault_id


def test_initialize_vault_rejects_non_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        initialize_vault(file_path)


# ---------------------------------------------------------------------------
# setup_wizard.run_setup
# ---------------------------------------------------------------------------


def test_run_setup_indexes_without_client(tmp_path: Path) -> None:
    _write_note(tmp_path, "a.md", "# A\n\nhello world\n")
    result = asyncio.run(run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_NONE)))
    assert result.bootstrap.created is True
    assert result.indexed_notes == 1
    assert result.client_config_path is None
    assert result.write_path is None
    assert sidecar_index_db(tmp_path).is_file()


def test_run_setup_can_skip_index(tmp_path: Path) -> None:
    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_NONE, build_index=False))
    )
    assert result.indexed_notes is None


def test_run_setup_enables_write_default_memory(tmp_path: Path) -> None:
    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=CLIENT_NONE,
                enable_write=True,
                build_index=False,
            )
        )
    )
    assert result.write_path == tmp_path / "_memory"
    assert (tmp_path / "_memory").is_dir()


def test_run_setup_rejects_invalid_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="client"):
        asyncio.run(run_setup(SetupPlan(vault_path=tmp_path, client="bogus")))


def test_run_setup_rejects_invalid_durability(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="durability"):
        asyncio.run(
            run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_NONE, durability="turbo"))
        )


def test_run_setup_configures_claude_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_install(
        vault_root: Path, *, extra_env: dict[str, str] | None = None, **_: Any
    ) -> Path:
        captured["vault_root"] = vault_root
        captured["extra_env"] = extra_env
        return tmp_path / "config.json"

    monkeypatch.setattr(setup_wizard, "install_claude_desktop_config", fake_install)
    plan = SetupPlan(
        vault_path=tmp_path,
        client=CLIENT_CLAUDE_DESKTOP,
        enable_write=True,
        read_only=True,
        durability="strict",
        build_index=False,
    )
    result = asyncio.run(run_setup(plan))
    assert result.client_config_path == tmp_path / "config.json"
    assert captured["extra_env"]["DATACRON_DURABILITY"] == "strict"
    assert captured["extra_env"]["DATACRON_READ_ONLY"] == "true"
    assert "DATACRON_WRITE_PATHS" in captured["extra_env"]


def test_run_setup_client_failure_becomes_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(vault_root: Path, *, extra_env: dict[str, str] | None = None, **_: Any) -> Path:
        raise ClaudeDesktopConfigError("no config here")

    monkeypatch.setattr(setup_wizard, "install_claude_desktop_config", boom)
    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_CLAUDE_DESKTOP, build_index=False))
    )
    assert result.client_config_path is None
    assert result.warnings
    assert "no config here" in result.warnings[0]


def test_run_setup_claude_code_returns_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = str((tmp_path / "bin" / "datacron-mcp").resolve())
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command=command, args=()),
    )
    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=CLIENT_CLAUDE_CODE,
                enable_write=True,
                read_only=True,
                durability="strict",
                build_index=False,
            )
        )
    )
    assert result.client_config_path is None
    assert result.stdio_config is not None
    parsed = json.loads(result.stdio_config)
    server = parsed["mcpServers"]["datacron"]
    assert server["command"] == command
    assert server["args"] == []
    assert server["env"]["DATACRON_VAULT_ROOT"] == str(tmp_path)
    assert server["env"]["DATACRON_READ_ONLY"] == "true"
    assert server["env"]["DATACRON_DURABILITY"] == "strict"
    assert "DATACRON_WRITE_PATHS" in server["env"]


def test_claude_code_stdio_config_is_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = str((tmp_path / "bin" / "datacron-mcp").resolve())
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command=command, args=()),
    )
    snippet = claude_code_stdio_config(tmp_path, {"DATACRON_DURABILITY": "best-effort"})
    parsed = json.loads(snippet)
    server = parsed["mcpServers"]["datacron"]
    assert server["command"] == command
    assert server["args"] == []
    assert server["env"] == {
        "DATACRON_DURABILITY": "best-effort",
        "DATACRON_READ_PATHS": str(tmp_path),
        "DATACRON_VAULT_ROOT": str(tmp_path),
    }


def test_claude_code_stdio_config_uses_frozen_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dist" / "datacron.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    snippet = claude_code_stdio_config(tmp_path, {})
    server = json.loads(snippet)["mcpServers"]["datacron"]

    assert server["command"] == str(executable.resolve())
    assert server["args"] == ["mcp", "serve"]
    assert server["env"] == {
        "DATACRON_READ_PATHS": str(tmp_path),
        "DATACRON_VAULT_ROOT": str(tmp_path),
    }


def test_run_setup_all_installs_detected_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    target = ClientTarget("cursor", "Cursor", "user", tmp_path / "mcp.json", "json-mcpservers")

    def fake_discover(
        *, scopes: Any, project_dir: Any, include: Any = None, exclude: Any = ()
    ) -> Any:
        captured["scopes"] = scopes
        captured["project_dir"] = project_dir
        return [target]

    def fake_install(targets: Any, *, command: str, args: Any, env: dict[str, str]) -> Any:
        captured["command"] = command
        captured["args"] = args
        captured["env"] = env
        return [InstallOutcome("cursor", "Cursor", "user", target.config_path, installed=True)]

    command = str((tmp_path / "bin" / "datacron-mcp").resolve())
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command=command, args=()),
    )
    monkeypatch.setattr(setup_wizard, "discover_targets", fake_discover)
    monkeypatch.setattr(setup_wizard, "install_targets", fake_install)

    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_ALL, build_index=False))
    )
    assert len(result.client_installs) == 1
    assert result.client_installs[0].installed is True
    assert captured["command"] == command
    assert captured["args"] == []
    assert captured["env"]["DATACRON_VAULT_ROOT"] == str(tmp_path)
    assert captured["env"]["DATACRON_READ_PATHS"] == str(tmp_path)
    assert captured["project_dir"] == tmp_path


def test_run_setup_all_writes_frozen_invocation_to_client_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dist" / "datacron.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    project_config = tmp_path / ".mcp.json"
    user_config = tmp_path / "user" / ".cursor" / "mcp.json"
    targets = [
        ClientTarget(
            "claude-code",
            "Claude Code",
            "project",
            project_config,
            "json-mcpservers",
        ),
        ClientTarget("cursor", "Cursor", "user", user_config, "json-mcpservers"),
    ]
    monkeypatch.setattr(setup_wizard, "discover_targets", lambda **_: targets)

    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_ALL, build_index=False))
    )

    assert all(outcome.installed for outcome in result.client_installs)
    for config_path in (project_config, user_config):
        server = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["datacron"]
        assert server["command"] == str(executable.resolve())
        assert server["args"] == ["mcp", "serve"]
        assert server["env"]["DATACRON_VAULT_ROOT"] == str(tmp_path)
        assert server["env"]["DATACRON_READ_PATHS"] == str(tmp_path)


def test_run_setup_all_warns_when_no_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command="datacron-mcp", args=()),
    )
    monkeypatch.setattr(setup_wizard, "discover_targets", lambda **_: [])

    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_ALL, build_index=False))
    )
    assert result.client_installs == []
    assert any("No MCP clients" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_setup_yes_client_none(tmp_path: Path) -> None:
    _write_note(tmp_path, "a.md", "# A\n\nhello world\n")
    result = _runner.invoke(app, ["setup", "--vault", str(tmp_path), "--client", "none", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Datacron setup complete." in result.output
    assert sidecar_vault_config(tmp_path).is_file()


def test_cli_setup_rejects_unknown_client(tmp_path: Path) -> None:
    result = _runner.invoke(app, ["setup", "--vault", str(tmp_path), "--client", "bogus", "--yes"])
    assert result.exit_code != 0
