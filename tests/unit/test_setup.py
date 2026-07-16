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

import datacron.cli as cli_module
from datacron import setup_wizard
from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.installers.claude_desktop import ClaudeDesktopConfigError, MCPServerInvocation
from datacron.installers.mcp_clients import ClientTarget, InstallOutcome, UnregisterOutcome
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
    assert result.index_error is None
    assert result.client_config_path is None
    assert result.write_path is None
    assert sidecar_index_db(tmp_path).is_file()


def test_run_setup_can_skip_index(tmp_path: Path) -> None:
    result = asyncio.run(
        run_setup(SetupPlan(vault_path=tmp_path, client=CLIENT_NONE, build_index=False))
    )
    assert result.indexed_notes is None
    assert result.index_error is None


def test_run_setup_registers_before_index_and_defers_index_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_initialize = initialize_vault

    def fake_reset(vault_root: Path) -> setup_wizard.ResetResult:
        calls.append("reset")
        return setup_wizard.ResetResult(config_removed=False, index_removed=False)

    def fake_initialize(vault_path: Path, *, force: bool) -> Any:
        calls.append("init")
        return real_initialize(vault_path, force=force)

    def fake_resolve_write_path(plan: SetupPlan, vault_root: Path) -> Path:
        calls.append("write_path")
        return vault_root / "_memory"

    def fake_extra_env(plan: SetupPlan, write_path: Path | None) -> dict[str, str]:
        calls.append("extra_env")
        assert write_path == tmp_path / "_memory"
        return {"DATACRON_WRITE_PATHS": str(write_path)}

    def fake_install(
        vault_root: Path, *, extra_env: dict[str, str] | None = None, **_: Any
    ) -> Path:
        calls.append("register")
        assert (vault_root / "_memory").is_dir()
        assert extra_env == {"DATACRON_WRITE_PATHS": str(tmp_path / "_memory")}
        return tmp_path / "client.json"

    async def fail_index(vault_root: Path, settings: Any) -> int:
        calls.append("index")
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(setup_wizard, "reset_user_state", fake_reset)
    monkeypatch.setattr(setup_wizard, "initialize_vault", fake_initialize)
    monkeypatch.setattr(setup_wizard, "_resolve_write_path", fake_resolve_write_path)
    monkeypatch.setattr(setup_wizard, "_build_extra_env", fake_extra_env)
    monkeypatch.setattr(setup_wizard, "install_claude_desktop_config", fake_install)
    monkeypatch.setattr(setup_wizard, "_build_index", fail_index)

    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=CLIENT_CLAUDE_DESKTOP,
                enable_write=True,
                reset=True,
            )
        )
    )

    assert calls == ["reset", "init", "write_path", "extra_env", "register", "index"]
    assert result.client_config_path == tmp_path / "client.json"
    assert result.indexed_notes is None
    assert result.index_error == "RuntimeError: index unavailable"


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


def test_cli_setup_reports_deferred_index_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_index(vault_root: Path, settings: Any) -> int:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(setup_wizard, "_build_index", fail_index)

    result = _runner.invoke(
        app,
        ["setup", "--vault", str(tmp_path), "--client", "none", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "index:      deferred - RuntimeError: index unavailable" in result.output
    assert "run `datacron index` when indexing is available" in result.output


def test_cli_setup_rejects_unknown_client(tmp_path: Path) -> None:
    result = _runner.invoke(app, ["setup", "--vault", str(tmp_path), "--client", "bogus", "--yes"])
    assert result.exit_code != 0


def test_cli_unregister_user_scope_removes_without_resolving_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"mcpServers":{"datacron":{}}}', encoding="utf-8")
    target = ClientTarget("cursor", "Cursor", "user", config_path, "json-mcpservers")
    captured: dict[str, Any] = {}

    def fake_discover(**kwargs: Any) -> list[ClientTarget]:
        captured.update(kwargs)
        return [target]

    def forbidden_vault(*_args: Any, **_kwargs: Any) -> Path:
        raise AssertionError("user scope must not resolve a vault")

    monkeypatch.setattr(cli_module, "discover_unregistration_targets", fake_discover)
    monkeypatch.setattr(cli_module, "_resolve_vault_root", forbidden_vault)

    result = _runner.invoke(
        app,
        ["unregister", "--scope", "user", "--client", "cursor", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "removed" in result.output
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"] == {}
    assert captured["project_dir"] is None
    assert captured["include"] == ("cursor",)


def test_cli_unregister_no_config_is_successful_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_discover(**kwargs: Any) -> list[ClientTarget]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli_module, "discover_unregistration_targets", fake_discover)

    result = _runner.invoke(app, ["unregister", "--scope", "user", "--yes"])

    assert result.exit_code == 0, result.output
    assert "No client config found; nothing to unregister." in result.output
    assert captured["include"] is None


def test_cli_unregister_already_absent_is_successful_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_bytes(b'{"mcpServers":{}}')
    before_bytes = config_path.read_bytes()
    before_mtime = config_path.stat().st_mtime_ns
    target = ClientTarget("cursor", "Cursor", "user", config_path, "json-mcpservers")
    monkeypatch.setattr(
        cli_module,
        "discover_unregistration_targets",
        lambda **_: [target],
    )

    result = _runner.invoke(app, ["unregister", "--scope", "user", "--yes"])

    assert result.exit_code == 0, result.output
    assert "already unregistered" in result.output
    assert config_path.read_bytes() == before_bytes
    assert config_path.stat().st_mtime_ns == before_mtime


def test_cli_unregister_refusal_leaves_config_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_bytes(b'{"mcpServers":{"datacron":{}}}')
    before_bytes = config_path.read_bytes()
    target = ClientTarget("cursor", "Cursor", "user", config_path, "json-mcpservers")
    monkeypatch.setattr(
        cli_module,
        "discover_unregistration_targets",
        lambda **_: [target],
    )

    result = _runner.invoke(app, ["unregister", "--scope", "user"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted; nothing changed." in result.output
    assert config_path.read_bytes() == before_bytes


@pytest.mark.parametrize("scope", ["project", "both", None])
def test_cli_unregister_project_scope_requires_vault_before_discovery(
    tmp_path: Path, scope: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_bytes(b'{"mcpServers":{"datacron":{}}}')
    before_bytes = config_path.read_bytes()

    def missing_vault(*_args: Any, **_kwargs: Any) -> Path:
        cli_module._error("No vault root provided for project configs.")

    def forbidden_discovery(**_kwargs: Any) -> list[ClientTarget]:
        raise AssertionError("discovery must not run before vault validation")

    monkeypatch.setattr(cli_module, "_resolve_vault_root", missing_vault)
    monkeypatch.setattr(cli_module, "discover_unregistration_targets", forbidden_discovery)

    args = ["unregister", "--yes"]
    if scope is not None:
        args.extend(("--scope", scope))
    result = _runner.invoke(app, args)

    assert result.exit_code == 1
    assert "No vault root provided for project configs." in result.output
    assert config_path.read_bytes() == before_bytes


def test_cli_unregister_renders_all_outcomes_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken_target = ClientTarget(
        "cursor", "Cursor", "user", tmp_path / "broken.json", "json-mcpservers"
    )
    good_target = ClientTarget("vscode", "VS Code", "user", tmp_path / "good.json", "json-servers")
    outcomes = [
        UnregisterOutcome(
            "cursor",
            "Cursor",
            "user",
            broken_target.config_path,
            successful=False,
            changed=False,
            detail="invalid JSON",
        ),
        UnregisterOutcome(
            "vscode",
            "VS Code",
            "user",
            good_target.config_path,
            successful=True,
            changed=True,
        ),
    ]
    monkeypatch.setattr(
        cli_module,
        "discover_unregistration_targets",
        lambda **_: [broken_target, good_target],
    )
    monkeypatch.setattr(cli_module, "unregister_targets", lambda _targets: outcomes)

    result = _runner.invoke(app, ["unregister", "--scope", "user", "--yes"])
    rendered = result.stdout + result.stderr

    assert result.exit_code == 1
    assert "Cursor (user)" in rendered
    assert "invalid JSON" in rendered
    assert "VS Code (user)" in rendered
    assert "removed" in rendered


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        ("--client", "bogus", "Unknown client"),
        ("--scope", "bogus", "Unknown scope"),
    ],
)
def test_cli_unregister_rejects_unknown_selection(option: str, value: str, expected: str) -> None:
    result = _runner.invoke(app, ["unregister", option, value, "--yes"])
    assert result.exit_code == 1
    assert expected in result.output
