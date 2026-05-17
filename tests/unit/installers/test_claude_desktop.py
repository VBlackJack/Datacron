# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.installers.claude_desktop`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacron.installers.claude_desktop import (
    DATACRON_SERVER_KEY,
    ClaudeDesktopConfigError,
    config_path_for_platform,
    install_claude_desktop_config,
)


@pytest.fixture
def custom_config_path(tmp_path: Path) -> Path:
    """A writable config-file location under tmp_path (not the real user dir)."""
    return tmp_path / "Claude" / "claude_desktop_config.json"


class TestConfigPathForPlatform:
    def test_macos(self) -> None:
        path = config_path_for_platform("darwin")
        # macOS uses spaces in the directory name; assert the leaf only to
        # keep the test portable across home directories.
        assert path.name == "claude_desktop_config.json"
        assert "Application Support" in str(path)
        assert path.parts[-2] == "Claude"

    def test_windows_with_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPDATA", "C:\\Users\\test\\AppData\\Roaming")
        path = config_path_for_platform("win32")
        assert path.name == "claude_desktop_config.json"
        assert path.parts[-2] == "Claude"

    def test_windows_missing_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APPDATA", raising=False)
        with pytest.raises(ClaudeDesktopConfigError, match="APPDATA"):
            config_path_for_platform("win32")

    def test_linux_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        path = config_path_for_platform("linux")
        assert path.parts[-3:] == (".config", "Claude", "claude_desktop_config.json")

    def test_linux_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        path = config_path_for_platform("linux")
        assert str(path).startswith(str(tmp_path / "xdg"))

    def test_unknown_platform(self) -> None:
        with pytest.raises(ClaudeDesktopConfigError, match="Unsupported platform"):
            config_path_for_platform("plan9")


class TestInstall:
    def test_creates_config_when_absent(self, tmp_path: Path, custom_config_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        assert not custom_config_path.exists()

        result = install_claude_desktop_config(vault, config_path=custom_config_path)
        assert result == custom_config_path
        assert custom_config_path.is_file()

        data = json.loads(custom_config_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"][DATACRON_SERVER_KEY]
        assert entry["command"] == "datacron-mcp"
        assert entry["args"] == []
        assert entry["env"]["DATACRON_VAULT_ROOT"] == str(vault.resolve())
        assert entry["env"]["DATACRON_READ_PATHS"] == str(vault.resolve())

    def test_preserves_existing_servers(self, tmp_path: Path, custom_config_path: Path) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {
                            "command": "node",
                            "args": ["/path/fs-server.js"],
                        }
                    },
                    "theme": "dark",
                }
            ),
            encoding="utf-8",
        )
        vault = tmp_path / "v"
        vault.mkdir()

        install_claude_desktop_config(vault, config_path=custom_config_path)
        data = json.loads(custom_config_path.read_text(encoding="utf-8"))

        # Other server preserved
        assert "filesystem" in data["mcpServers"]
        assert data["mcpServers"]["filesystem"]["command"] == "node"
        # Other top-level keys preserved
        assert data["theme"] == "dark"
        # Datacron added
        assert DATACRON_SERVER_KEY in data["mcpServers"]

    def test_overwrites_existing_datacron_entry(
        self, tmp_path: Path, custom_config_path: Path
    ) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        DATACRON_SERVER_KEY: {
                            "command": "old-binary",
                            "args": ["--stale"],
                            "env": {"DATACRON_VAULT_ROOT": "/old/path"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        vault = tmp_path / "newvault"
        vault.mkdir()

        install_claude_desktop_config(vault, config_path=custom_config_path)
        data = json.loads(custom_config_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"][DATACRON_SERVER_KEY]
        assert entry["command"] == "datacron-mcp"
        assert entry["args"] == []
        assert entry["env"]["DATACRON_VAULT_ROOT"] == str(vault.resolve())

    def test_extra_env_merged(self, tmp_path: Path, custom_config_path: Path) -> None:
        vault = tmp_path / "v"
        vault.mkdir()
        install_claude_desktop_config(
            vault,
            config_path=custom_config_path,
            extra_env={"DATACRON_LOG_LEVEL": "DEBUG"},
        )
        data = json.loads(custom_config_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"][DATACRON_SERVER_KEY]
        assert entry["env"]["DATACRON_LOG_LEVEL"] == "DEBUG"
        assert entry["env"]["DATACRON_VAULT_ROOT"] == str(vault.resolve())

    def test_invalid_json_rejected(self, tmp_path: Path, custom_config_path: Path) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ClaudeDesktopConfigError, match="not valid JSON"):
            install_claude_desktop_config(tmp_path, config_path=custom_config_path)

    def test_non_object_root_rejected(self, tmp_path: Path, custom_config_path: Path) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text("[]", encoding="utf-8")
        with pytest.raises(ClaudeDesktopConfigError, match="not a JSON object"):
            install_claude_desktop_config(tmp_path, config_path=custom_config_path)

    def test_non_object_mcp_servers_rejected(
        self, tmp_path: Path, custom_config_path: Path
    ) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text(
            json.dumps({"mcpServers": ["not", "a", "dict"]}),
            encoding="utf-8",
        )
        with pytest.raises(ClaudeDesktopConfigError, match="not an object"):
            install_claude_desktop_config(tmp_path, config_path=custom_config_path)

    def test_write_is_atomic_no_leftover_temp(
        self, tmp_path: Path, custom_config_path: Path
    ) -> None:
        vault = tmp_path / "v"
        vault.mkdir()
        install_claude_desktop_config(vault, config_path=custom_config_path)
        # After a successful install, the parent directory must contain
        # the config file and nothing else with a .tmp suffix.
        leftovers = [p for p in custom_config_path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_empty_file_treated_as_empty_config(
        self, tmp_path: Path, custom_config_path: Path
    ) -> None:
        custom_config_path.parent.mkdir(parents=True)
        custom_config_path.write_text("   \n", encoding="utf-8")
        vault = tmp_path / "v"
        vault.mkdir()
        install_claude_desktop_config(vault, config_path=custom_config_path)
        data = json.loads(custom_config_path.read_text(encoding="utf-8"))
        assert DATACRON_SERVER_KEY in data["mcpServers"]
