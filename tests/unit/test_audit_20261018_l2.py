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
"""Regressions for the 2026-10-18 audit, installer and setup lot.

F-1: a setup rerun on a Dual-Vault install without --memory-vault wrote an
entry the server refused to start with, and created a sidecar in the docs vault.
F-2: the Windows installer passed --no-write on every run whose write box was
unticked, so any upgrade removed write access. F-4, F-5, F-6, F-7 and F-12 are
the cheaper installer and writer findings of the same lot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from datacron import setup_wizard
from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.core.config import SIDECAR_DIR_NAME, Settings, reset_settings_cache
from datacron.installers.claude_desktop import (
    MCPServerInvocation,
    install_claude_desktop_config,
)
from datacron.installers.env_merge import merge_server_env
from datacron.installers.mcp_clients import ClientTarget
from datacron.mcp.server import build_app
from datacron.setup_wizard import CLIENT_ALL, SetupPlan, default_write_paths, run_setup

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_INSTALLER: Final[Path] = _REPO_ROOT / "packaging" / "windows" / "datacron-installer.iss"
_runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cursor_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    extra: dict[str, object] | None = None,
) -> Path:
    """Write one Cursor config holding a Datacron entry, and route setup to it."""
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    for variable in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(variable, str(profile))
    monkeypatch.setattr(Path, "home", lambda: profile)
    config = profile / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    entry: dict[str, object] = {"command": "old", "args": [], "env": env, **(extra or {})}
    config.write_text(json.dumps({"mcpServers": {"datacron": entry}}), encoding="utf-8")
    target = ClientTarget("cursor", "Cursor", "user", config, "json-mcpservers")
    monkeypatch.setattr(setup_wizard, "discover_targets", lambda **_: [target])
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command="datacron-mcp", args=()),
    )
    return config


def _written_env(config: Path) -> dict[str, str]:
    env: dict[str, str] = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["datacron"][
        "env"
    ]
    return env


def _dual_vault(tmp_path: Path) -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    docs.mkdir()
    memory = tmp_path / "memory"
    initialize_vault(memory)
    (memory / "_memory").mkdir()
    return docs.resolve(), memory.resolve()


def _dual_vault_env(docs: Path, memory: Path) -> dict[str, str]:
    return {
        "DATACRON_VAULT_ROOT": str(docs),
        "DATACRON_MEMORY_ROOT": str(memory),
        "DATACRON_READ_PATHS": os.pathsep.join([str(docs), str(memory)]),
    }


def _server_builds(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the MCP app from exactly the environment a client entry holds."""
    for key in list(os.environ):
        if key.startswith("DATACRON_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    try:
        settings = Settings()
        assert settings.vault_root is not None
        build_app(vault_root=settings.vault_root, settings=settings)
    finally:
        reset_settings_cache()


# ---------------------------------------------------------------------------
# F-1: Dual-Vault rerun without --memory-vault
# ---------------------------------------------------------------------------


def test_dual_vault_rerun_without_memory_vault_keeps_a_startable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rerun kept DATACRON_MEMORY_ROOT but rewrote the read allowlist to docs only."""
    docs, memory = _dual_vault(tmp_path)
    config = _cursor_entry(tmp_path, monkeypatch, _dual_vault_env(docs, memory))

    asyncio.run(run_setup(SetupPlan(vault_path=docs, client=CLIENT_ALL, build_index=False)))

    env = _written_env(config)
    assert Path(env["DATACRON_MEMORY_ROOT"]) == memory
    read_paths = {Path(part) for part in env["DATACRON_READ_PATHS"].split(os.pathsep)}
    assert memory in read_paths, "the memory root must stay inside the read allowlist"
    _server_builds(env, monkeypatch)


def test_dual_vault_rerun_does_not_create_a_sidecar_in_the_docs_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dual-Vault promises the docs vault is never given a .datacron directory."""
    docs, memory = _dual_vault(tmp_path)
    _cursor_entry(tmp_path, monkeypatch, _dual_vault_env(docs, memory))

    result = asyncio.run(
        run_setup(SetupPlan(vault_path=docs, client=CLIENT_ALL, build_index=False))
    )

    assert not (docs / SIDECAR_DIR_NAME).exists()
    assert result.memory_vault_path == memory
    assert any(str(memory) in warning for warning in result.warnings)


def test_dual_vault_rerun_with_enable_write_allowlists_the_memory_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI derives --yes defaults before setup learns the memory vault."""
    docs, memory = _dual_vault(tmp_path)
    config = _cursor_entry(tmp_path, monkeypatch, _dual_vault_env(docs, memory))

    asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=docs,
                client=CLIENT_ALL,
                build_index=False,
                enable_write=True,
                write_paths=default_write_paths(docs),
            )
        )
    )

    written = _written_env(config)["DATACRON_WRITE_PATHS"]
    write_paths = {Path(part) for part in written.split(os.pathsep)}
    assert memory / "_memory" in write_paths
    assert docs / "_memory" not in write_paths
    _server_builds(_written_env(config), monkeypatch)


def test_single_vault_returns_a_dual_vault_install_to_one_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--single-vault is the supported way back; the memory root leaves the entry."""
    docs, memory = _dual_vault(tmp_path)
    config = _cursor_entry(tmp_path, monkeypatch, _dual_vault_env(docs, memory))

    result = asyncio.run(
        run_setup(
            SetupPlan(vault_path=docs, client=CLIENT_ALL, build_index=False, single_vault=True)
        )
    )

    env = _written_env(config)
    assert "DATACRON_MEMORY_ROOT" not in env
    assert env["DATACRON_READ_PATHS"] == str(docs)
    assert result.memory_vault_path is None
    assert (docs / SIDECAR_DIR_NAME).is_dir()
    _server_builds(env, monkeypatch)


def test_single_vault_cannot_be_combined_with_a_memory_vault(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--single-vault"):
        asyncio.run(
            run_setup(
                SetupPlan(
                    vault_path=tmp_path,
                    memory_vault_path=tmp_path / "memory",
                    single_vault=True,
                    client="none",
                    build_index=False,
                )
            )
        )


def test_cli_setup_single_vault_conflicts_with_memory_vault(tmp_path: Path) -> None:
    result = _runner.invoke(
        app,
        [
            "setup",
            "--yes",
            "--client",
            "none",
            "--no-index",
            "--vault",
            str(tmp_path / "docs"),
            "--memory-vault",
            str(tmp_path / "memory"),
            "--single-vault",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output, "refused as a conflict, not as an unknown flag"


def test_merge_keeps_a_preserved_memory_root_readable() -> None:
    """Each merged entry stays consistent even when another caller forgot the memory root."""
    docs = str(Path("/vaults/docs").resolve())
    memory = str(Path("/vaults/memory").resolve())
    merged = merge_server_env(
        {
            "DATACRON_VAULT_ROOT": docs,
            "DATACRON_MEMORY_ROOT": memory,
            "DATACRON_READ_PATHS": os.pathsep.join([docs, memory]),
        },
        {"DATACRON_VAULT_ROOT": docs, "DATACRON_READ_PATHS": docs},
    )

    assert merged["DATACRON_MEMORY_ROOT"] == memory
    assert merged["DATACRON_READ_PATHS"] == os.pathsep.join([docs, memory])


def test_merge_drops_the_old_memory_vault_when_the_vault_changes() -> None:
    """A new vault must not be bound to the previous vault's memory, readable and writable."""
    old_docs = str(Path("/vaults/a").resolve())
    old_memory = str(Path("/vaults/a-memory").resolve())
    new_docs = str(Path("/vaults/b").resolve())
    merged = merge_server_env(
        {
            "DATACRON_VAULT_ROOT": old_docs,
            "DATACRON_MEMORY_ROOT": old_memory,
            "DATACRON_READ_PATHS": os.pathsep.join([old_docs, old_memory]),
            "DATACRON_WRITE_PATHS": str(Path(old_memory) / "_memory"),
        },
        {"DATACRON_VAULT_ROOT": new_docs, "DATACRON_READ_PATHS": new_docs},
    )

    assert "DATACRON_MEMORY_ROOT" not in merged
    assert merged["DATACRON_READ_PATHS"] == new_docs
    assert "DATACRON_WRITE_PATHS" not in merged


def test_setup_on_another_vault_does_not_inherit_the_old_memory_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs, memory = _dual_vault(tmp_path)
    env = _dual_vault_env(docs, memory)
    env["DATACRON_WRITE_PATHS"] = str(memory / "_memory")
    config = _cursor_entry(tmp_path, monkeypatch, env)
    other = tmp_path / "other"
    other.mkdir()

    result = asyncio.run(
        run_setup(SetupPlan(vault_path=other, client=CLIENT_ALL, build_index=False))
    )

    written = _written_env(config)
    assert "DATACRON_MEMORY_ROOT" not in written
    assert written["DATACRON_READ_PATHS"] == str(other.resolve())
    assert "DATACRON_WRITE_PATHS" not in written
    assert result.memory_vault_path is None
    _server_builds(written, monkeypatch)


# ---------------------------------------------------------------------------
# F-12: the Claude Desktop writer merges like every other client writer
# ---------------------------------------------------------------------------


def test_claude_desktop_writer_keeps_foreign_keys_and_variables(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    vault = tmp_path / "vault"
    vault.mkdir()
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "datacron": {
                        "command": "old",
                        "args": [],
                        "disabled": False,
                        "autoApprove": ["search_text"],
                        "env": {"HTTPS_PROXY": "http://proxy.invalid:3128"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    install_claude_desktop_config(vault, config_path=config, command="datacron-mcp")

    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["datacron"]
    assert entry["command"] == "datacron-mcp"
    assert entry["autoApprove"] == ["search_text"]
    assert entry["disabled"] is False
    assert entry["env"]["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert entry["env"]["DATACRON_VAULT_ROOT"] == str(vault.resolve())


# ---------------------------------------------------------------------------
# Installer script (static: the .iss is compiled by build_installer.py)
# ---------------------------------------------------------------------------


def _source() -> str:
    return _INSTALLER.read_text(encoding="utf-8")


def _routine(name: str) -> str:
    body = _source()
    start = re.search(rf"^(?:function|procedure) {name}\b", body, re.M)
    assert start is not None, name
    following = re.search(r"^(?:function|procedure) ", body[start.end() :], re.M)
    end = len(body) if following is None else start.end() + following.start()
    return body[start.start() : end]


def test_installer_write_choice_is_tri_state() -> None:
    """F-2: without a change by the operator, setup receives no write flag."""
    choice = _routine("WriteChoice")
    assert "WriteChoiceKeep" in choice
    assert "CommandLineSwitchPresent('ENABLEWRITE')" in choice
    assert "CommandLineSwitchPresent('DISABLEWRITE')" in choice
    assert "InitialWriteEnabled and not WritePage.Values[0]" in choice

    setup = _routine("RunDatacronSetup")
    assert "if WriteToolsRequested then" not in setup
    assert re.search(
        r"if Choice = WriteChoiceEnable then\s+Parameters := Parameters \+ ' --enable-write'\s+"
        r"else if Choice = WriteChoiceDisable then\s+Parameters := Parameters \+ ' --no-write';",
        setup,
    ), "--no-write is passed only for an explicit opt-out"


def test_installer_write_box_is_prefilled_from_the_recorded_choice() -> None:
    body = _source()
    wizard = _routine("InitializeWizard")
    assert "WritePage.Values[0] := InitialWriteEnabled;" in wizard
    assert "InstallerWriteValue" in wizard
    setup = _routine("RunDatacronSetup")
    assert "RegWriteStringValue(HKCU, InstallerStateKey, InstallerWriteValue, '1')" in setup
    assert "RegWriteStringValue(HKCU, InstallerStateKey, InstallerWriteValue, '0')" in setup
    assert "english.WriteSwitchConflict=" in body
    assert "french.WriteSwitchConflict=" in body


def test_installer_silent_mode_falls_back_to_the_recorded_memory_vault() -> None:
    """F-1: a silent upgrade without /MEMORYVAULT= kept running as Single-Vault."""
    selected = _routine("SelectedMemoryVaultPath")
    assert "Result := Trim(PreviousMemoryVaultPath);" in selected
    assert "CommandLineSwitchPresent('SINGLEVAULT')" in selected
    assert re.search(
        r"NormalizePathEntry\(CommandLineValue\('VAULT'\)\),\s+"
        r"NormalizePathEntry\(PreviousVaultPath\)\s+\) = 0",
        selected,
    ), "only the recorded docs vault inherits the recorded memory vault"
    assert selected.index("NormalizePathEntry") < selected.index(
        "Result := Trim(PreviousMemoryVaultPath);"
    )
    setup = _routine("RunDatacronSetup")
    assert "else if SingleVaultRequested then" in setup
    assert "' --single-vault'" in setup


def test_installer_quotes_a_drive_root_without_escaping_the_closing_quote() -> None:
    """F-4: "G:\\" reads as an escaped quote; the trailing backslash is doubled."""
    quoting = _routine("QuotedArgument")
    assert "Value[Length(Value)] = '\\'" in quoting
    assert "'\"' + Value + '\\\"'" in quoting
    setup = _routine("RunDatacronSetup")
    assert "AddQuotes(" not in setup, "every command-line path goes through QuotedArgument"


def test_installer_companion_shortcut_names_the_vault() -> None:
    """F-5: the Companion shortcut opened whatever vault the environment named."""
    body = _source()
    assert 'Parameters: "{code:CompanionShortcutParameters}"' in body
    assert "Result := 'companion' + VaultArguments;" in _routine("CompanionShortcutParameters")
    arguments = _routine("VaultArguments")
    assert "if EffectiveMemoryVaultPath <> '' then" in arguments


def test_installer_shortcuts_follow_the_single_vault_answer() -> None:
    """After a Single-Vault run the shortcuts pass --vault alone, like setup and the registry."""
    effective = _routine("EffectiveMemoryVaultPath")
    assert "if PathsPrepared or (MemoryVaultPath <> '') then" in effective
    prepare = _routine("PrepareToInstall")
    assert prepare.index("MemoryVaultPath := NormalizePathEntry") < prepare.index(
        "PathsPrepared := True;"
    )
    setup = _routine("RunDatacronSetup")
    assert "RegDeleteValue(HKCU, InstallerStateKey, InstallerMemoryVaultValue);" in setup


def test_installer_setup_messages_are_custom_messages() -> None:
    """F-6: the two setup failure messages were English in every language."""
    body = _source()
    assert "'Could not start datacron.exe.'" not in body
    assert "'datacron setup returned exit code '" not in body
    for name in ("SetupStartFailed", "SetupExitCode"):
        assert f"english.{name}=" in body
        assert f"french.{name}=" in body
    assert "FmtMessage(CustomMessage('SetupExitCode')" in body


def test_installer_removes_the_user_write_allowlist_it_created() -> None:
    """F-7: uninstall left DATACRON_WRITE_PATHS granting write access to later clients."""
    setup = _routine("RunDatacronSetup")
    assert "HadWriteEnv := RegValueExists(HKCU, UserEnvironmentKey, WriteEnvName);" in setup
    assert "RecordWriteEnvReceipt(HadWriteEnv);" in setup
    remover = _routine("RemoveWriteEnvAdded")
    assert remover.index("InstallerWriteEnvValue") < remover.index("RegDeleteValue")
    assert "CurrentValue <> AddedValue" in remover
    assert "RemoveWriteEnvAdded;" in _routine("CurUninstallStepChanged")
