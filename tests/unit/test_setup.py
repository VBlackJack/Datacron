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
import os
import sys
import tomllib
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import datacron.cli as cli_module
from datacron import setup_wizard
from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.core.config import reset_settings_cache
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.installers import mcp_clients
from datacron.installers.claude_desktop import ClaudeDesktopConfigError, MCPServerInvocation
from datacron.installers.mcp_clients import ClientTarget, InstallOutcome, UnregisterOutcome
from datacron.setup_wizard import (
    CLIENT_ALL,
    CLIENT_CHOICES,
    CLIENT_CLAUDE_CODE,
    CLIENT_CLAUDE_DESKTOP,
    CLIENT_NONE,
    SetupPlan,
    claude_code_stdio_config,
    run_setup,
)

_runner = CliRunner()


def _fake_winreg(
    current_value: str | None,
    *,
    current_type: int = 2,
) -> tuple[SimpleNamespace, list[tuple[str, int, str]]]:
    value = [current_value]
    writes: list[tuple[str, int, str]] = []

    def open_key(
        _root: object,
        _sub_key: str,
        _reserved: int,
        _access: int,
    ) -> AbstractContextManager[object]:
        return nullcontext(object())

    def query_value(_key: object, _name: str) -> tuple[object, int]:
        if value[0] is None:
            raise FileNotFoundError
        return value[0], current_type

    def set_value(
        _key: object,
        name: str,
        _reserved: int,
        value_type: int,
        new_value: str,
    ) -> None:
        writes.append((name, value_type, new_value))
        value[0] = new_value

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        KEY_SET_VALUE=2,
        REG_EXPAND_SZ=2,
        REG_SZ=1,
        OpenKey=open_key,
        CreateKeyEx=open_key,
        QueryValueEx=query_value,
        SetValueEx=set_value,
    )
    return fake, writes


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
    assert result.write_paths == []
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

    def fake_resolve_write_paths(plan: SetupPlan, vault_root: Path) -> list[Path]:
        calls.append("write_paths")
        return [vault_root / "_memory", vault_root / "_drafts", vault_root / "_journal"]

    def fake_extra_env(plan: SetupPlan, write_paths: list[Path]) -> dict[str, str]:
        calls.append("extra_env")
        assert write_paths == [
            tmp_path / "_memory",
            tmp_path / "_drafts",
            tmp_path / "_journal",
        ]
        return {"DATACRON_WRITE_PATHS": os.pathsep.join(str(path) for path in write_paths)}

    def fake_install(
        vault_root: Path, *, extra_env: dict[str, str] | None = None, **_: Any
    ) -> Path:
        calls.append("register")
        assert all((vault_root / name).is_dir() for name in ("_memory", "_drafts", "_journal"))
        assert extra_env == {
            "DATACRON_WRITE_PATHS": os.pathsep.join(
                str(tmp_path / name) for name in ("_memory", "_drafts", "_journal")
            )
        }
        return tmp_path / "client.json"

    async def fail_index(vault_root: Path, settings: Any) -> int:
        calls.append("index")
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(setup_wizard, "reset_user_state", fake_reset)
    monkeypatch.setattr(setup_wizard, "initialize_vault", fake_initialize)
    monkeypatch.setattr(setup_wizard, "_resolve_write_paths", fake_resolve_write_paths)
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

    assert calls == ["reset", "init", "write_paths", "extra_env", "register", "index"]
    assert result.client_config_path == tmp_path / "client.json"
    assert result.indexed_notes is None
    assert result.index_error == "RuntimeError: index unavailable"


def test_run_setup_enables_write_default_subfolders(tmp_path: Path) -> None:
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
    assert result.write_paths == [
        tmp_path / "_memory",
        tmp_path / "_drafts",
        tmp_path / "_journal",
    ]
    assert all(path.is_dir() for path in result.write_paths)


def test_resolve_write_paths_preserves_explicit_multi_path_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    plan = SetupPlan(
        vault_path=tmp_path,
        enable_write=True,
        write_paths=[first, second, first],
    )

    assert setup_wizard._resolve_write_paths(plan, tmp_path) == [
        first.resolve(),
        second.resolve(),
    ]


def test_run_setup_single_write_path_keeps_cli_compatibility(tmp_path: Path) -> None:
    explicit = tmp_path / "custom"
    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=CLIENT_NONE,
                enable_write=True,
                write_paths=[explicit],
                build_index=False,
            )
        )
    )

    assert result.write_paths == [explicit.resolve()]
    assert explicit.is_dir()
    assert not (tmp_path / "_memory").exists()


def test_configure_user_write_env_windows_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "_memory", tmp_path / "_drafts", tmp_path / "_journal"]
    serialized = os.pathsep.join(str(path) for path in paths)
    fake_winreg, writes = _fake_winreg(serialized)
    broadcasts: list[bool] = []

    monkeypatch.setattr(setup_wizard, "_is_windows", lambda: True)
    monkeypatch.setattr(setup_wizard, "_load_winreg", lambda: fake_winreg)
    monkeypatch.setattr(
        setup_wizard,
        "_broadcast_environment_change",
        lambda: broadcasts.append(True),
    )

    result = setup_wizard.configure_user_write_env(paths)

    assert result.action == "unchanged"
    assert result.effective_value == serialized
    assert writes == []
    assert broadcasts == []


def test_configure_user_write_env_windows_preserves_or_replaces_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "_memory", tmp_path / "_drafts", tmp_path / "_journal"]
    serialized = os.pathsep.join(str(path) for path in paths)
    fake_winreg, writes = _fake_winreg("C:\\existing", current_type=2)
    broadcasts: list[bool] = []

    monkeypatch.setattr(setup_wizard, "_is_windows", lambda: True)
    monkeypatch.setattr(setup_wizard, "_load_winreg", lambda: fake_winreg)
    monkeypatch.setattr(
        setup_wizard,
        "_broadcast_environment_change",
        lambda: broadcasts.append(True),
    )

    preserved = setup_wizard.configure_user_write_env(paths)
    replaced = setup_wizard.configure_user_write_env(paths, replace_existing=True)

    assert preserved.action == "preserved"
    assert preserved.effective_value == "C:\\existing"
    assert replaced.action == "replaced"
    assert writes == [("DATACRON_WRITE_PATHS", 2, serialized)]
    assert broadcasts == [True]


def test_run_setup_routes_machine_wide_opt_in_without_real_registry_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_configure(
        write_paths: list[Path],
        *,
        replace_existing: bool = False,
    ) -> setup_wizard.MachineWriteEnvResult:
        captured["write_paths"] = write_paths
        captured["replace_existing"] = replace_existing
        value = os.pathsep.join(str(path) for path in write_paths)
        return setup_wizard.MachineWriteEnvResult(value, value, None, "created")

    monkeypatch.setattr(setup_wizard, "configure_user_write_env", fake_configure)

    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=CLIENT_NONE,
                enable_write=True,
                machine_wide_write=True,
                replace_existing_write_env=True,
                build_index=False,
            )
        )
    )

    assert captured["write_paths"] == result.write_paths
    assert captured["replace_existing"] is True
    assert result.machine_write_env is not None
    assert result.machine_write_env.action == "created"


def test_configure_user_write_env_unix_returns_profile_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "_memory", tmp_path / "_drafts", tmp_path / "_journal"]
    serialized = os.pathsep.join(str(path) for path in paths)
    monkeypatch.setattr(setup_wizard, "_is_windows", lambda: False)
    monkeypatch.delenv("DATACRON_WRITE_PATHS", raising=False)

    result = setup_wizard.configure_user_write_env(paths)

    assert result.action == "manual-export"
    assert result.export_command is not None
    assert result.export_command.startswith("export DATACRON_WRITE_PATHS=")
    assert serialized in result.export_command


def test_single_writer_warning_for_known_sync_folder(tmp_path: Path) -> None:
    warning = setup_wizard._single_writer_warning(tmp_path / "OneDrive" / "vault")

    assert warning is not None
    assert "one active writer" in warning


def test_machine_wide_prompt_displays_existing_value_and_offers_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    questions: list[str] = []

    def confirm(question: str, *, default: bool) -> bool:
        questions.append(question)
        assert default is False
        return True

    monkeypatch.setattr(cli_module, "get_user_write_env", lambda: "C:\\existing")
    monkeypatch.setattr(typer, "confirm", confirm)

    result = cli_module._prompt_machine_wide_write(
        True,
        True,
        [tmp_path / "_memory", tmp_path / "_drafts", tmp_path / "_journal"],
        False,
    )

    assert result == (True, True)
    assert questions == ["Replace the existing user write allowlist?"]
    output = capsys.readouterr().out
    assert "The existing user-wide write allowlist differs" in output
    assert "Default: no, which preserves the existing user environment" in output


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
        captured["include"] = include
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
    assert captured["include"] is None


@pytest.mark.parametrize(
    ("client", "relative_path"),
    [
        ("cursor", Path(".cursor/mcp.json")),
        ("gemini-cli", Path(".gemini/settings.json")),
        ("antigravity", Path(".agents/mcp_config.json")),
        ("codex-cli", Path(".codex/config.toml")),
        ("vscode", Path(".vscode/mcp.json")),
    ],
)
def test_run_setup_specific_client_writes_only_requested_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    relative_path: Path,
) -> None:
    command = str((tmp_path / "bin" / "datacron-mcp").resolve())
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command=command, args=()),
    )
    monkeypatch.setattr(mcp_clients, "_is_present", lambda candidate: candidate == client)

    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client=client,
                install_scope="project",
                read_only=True,
                build_index=False,
            )
        )
    )

    expected = tmp_path / relative_path
    candidate_paths = {
        tmp_path / ".cursor" / "mcp.json",
        tmp_path / ".gemini" / "settings.json",
        tmp_path / ".agents" / "mcp_config.json",
        tmp_path / ".codex" / "config.toml",
        tmp_path / ".vscode" / "mcp.json",
    }
    assert client in CLIENT_CHOICES
    assert len(result.client_installs) == 1
    assert result.client_installs[0].client_id == client
    assert result.client_installs[0].scope == "project"
    assert result.client_installs[0].config_path == expected
    assert {path for path in candidate_paths if path.is_file()} == {expected}

    if expected.suffix == ".toml":
        server = tomllib.loads(expected.read_text(encoding="utf-8"))["mcp_servers"]["datacron"]
    else:
        parsed = json.loads(expected.read_text(encoding="utf-8"))
        server = parsed.get("mcpServers", parsed.get("servers"))["datacron"]
    assert server["command"] == command
    assert server["env"]["DATACRON_READ_ONLY"] == "true"


def test_run_setup_specific_client_warns_when_not_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command="datacron-mcp", args=()),
    )

    def fake_discover(**kwargs: Any) -> list[ClientTarget]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(setup_wizard, "discover_targets", fake_discover)
    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=tmp_path,
                client="gemini-cli",
                install_scope="project",
                build_index=False,
            )
        )
    )

    assert result.client_installs == []
    assert captured["include"] == ("gemini-cli",)
    assert any("gemini-cli" in warning for warning in result.warnings)


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


def test_cli_setup_interactive_explains_prompts_without_changing_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_wizard, "discover_targets", lambda **_: [])

    result = _runner.invoke(
        app,
        ["setup", "--no-index"],
        input="\n\n\n\nno\nno\nno\n",
    )

    assert result.exit_code == 0, result.output
    assert "Choose the dedicated Markdown folder" in result.output
    assert "Vault path" in result.output
    assert "Default: all, which registers every supported client" in result.output
    assert "MCP client" in result.output
    assert "Default: both, so user-wide and project-local" in result.output
    assert "Install scope" in result.output
    assert "Default: best-effort, which works across common filesystems" in result.output
    assert "Durability mode" in result.output
    assert "Default: no, which keeps write tools unavailable" in result.output
    assert "Enable the confined write tools?" in result.output
    assert "Default: no, which keeps normal operation" in result.output
    assert "Configure certified read-only mode?" in result.output
    assert "Default: no, which leaves all client instruction files unchanged" in result.output
    assert "Install the Datacron memory protocol" in result.output


def test_cli_setup_interactive_explains_write_boundaries(
    tmp_path: Path,
) -> None:
    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--client",
            "none",
            "--enable-write",
            "--no-index",
        ],
        input="\n\nno\nno\nno\n",
    )

    assert result.exit_code == 0, result.output
    assert (
        "Choose the only directories where Datacron write tools may change notes" in result.output
    )
    assert "_memory, _drafts, _journal under this vault" in result.output
    assert "Write-allowlisted directories" in result.output
    assert "Default: no, which limits the change to client configs" in result.output
    assert "Apply the write allowlist to this user account" in result.output


def test_cli_setup_reset_confirmation_is_explained(tmp_path: Path) -> None:
    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--client",
            "none",
            "--reset",
            "--no-index",
        ],
        input="no\n",
    )

    assert result.exit_code == 0, result.output
    assert "Reset removes this vault's Datacron configuration and generated index" in result.output
    assert "Default: no, which protects the current configuration and index" in result.output
    assert "Reset Datacron configuration and generated index for this vault?" in result.output
    assert "Reset cancelled; nothing changed." in result.output


def test_cli_setup_yes_does_not_print_interactive_explanations(tmp_path: Path) -> None:
    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--client",
            "none",
            "--yes",
            "--no-index",
        ],
    )

    assert result.exit_code == 0, result.output
    for lines in cli_module._SETUP_PROMPT_EXPLANATIONS.values():
        for line in lines:
            if "{" not in line:
                assert line not in result.output


def test_cli_setup_yes_client_none(tmp_path: Path) -> None:
    _write_note(tmp_path, "a.md", "# A\n\nhello world\n")
    result = _runner.invoke(app, ["setup", "--vault", str(tmp_path), "--client", "none", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Datacron setup complete." in result.output
    assert sidecar_vault_config(tmp_path).is_file()


def test_cli_setup_yes_without_vault_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes must never silently adopt the cwd when no vault source exists."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    result = _runner.invoke(app, ["setup", "--client", "none", "--yes"])
    assert result.exit_code != 0
    assert "Non-interactive setup (--yes) needs an explicit vault" in result.output
    assert not sidecar_vault_config(empty).exists()


def test_cli_setup_yes_uses_env_vault_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DATACRON_VAULT_ROOT is an acceptable explicit source for --yes."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "a.md", "# A\n\nhello world\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("DATACRON_VAULT_ROOT", str(vault))
    reset_settings_cache()
    monkeypatch.chdir(elsewhere)
    result = _runner.invoke(app, ["setup", "--client", "none", "--yes"])
    assert result.exit_code == 0, result.output
    assert sidecar_vault_config(vault).is_file()
    assert not sidecar_vault_config(elsewhere).exists()


def test_cli_setup_yes_accepts_cwd_with_existing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing .datacron/VAULT.yaml in the cwd is an explicit-enough source."""
    _write_note(tmp_path, "a.md", "# A\n\nhello world\n")
    initialize_vault(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = _runner.invoke(app, ["setup", "--client", "none", "--yes"])
    assert result.exit_code == 0, result.output


def test_cli_setup_refuses_user_profile_root_even_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user profile root is rejected as a vault even with an explicit --vault."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    result = _runner.invoke(app, ["setup", "--vault", str(fake_home), "--client", "none", "--yes"])
    assert result.exit_code != 0
    assert "user profile root" in result.output
    assert not sidecar_vault_config(fake_home).exists()


def test_cli_setup_yes_refuses_env_vault_root_at_user_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATACRON_VAULT_ROOT must not bypass the user profile root guard."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("DATACRON_VAULT_ROOT", str(fake_home))
    reset_settings_cache()
    result = _runner.invoke(app, ["setup", "--client", "none", "--yes"])
    assert result.exit_code != 0
    assert "user profile root" in result.output
    assert not sidecar_vault_config(fake_home).exists()


def test_cli_setup_accepts_explicit_user_profile_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard rejects only the profile root, not a dedicated directory below it."""
    fake_home = tmp_path / "home"
    vault = fake_home / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    result = _runner.invoke(
        app,
        ["setup", "--vault", str(vault), "--client", "none", "--yes", "--no-index"],
    )
    assert result.exit_code == 0, result.output
    assert sidecar_vault_config(vault).is_file()


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
