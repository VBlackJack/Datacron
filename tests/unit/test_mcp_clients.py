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
"""Tests for AI MCP client detection and Datacron registration."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from datacron.installers import mcp_clients
from datacron.installers.mcp_clients import (
    SCOPE_PROJECT,
    SCOPE_USER,
    discover_targets,
    discover_unregistration_targets,
    install_targets,
    unregister_targets,
)

_COMMAND = "datacron-mcp"
_ENV = {"DATACRON_VAULT_ROOT": "/vault", "DATACRON_READ_PATHS": "/vault"}


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home`` and client detection into an isolated tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        mcp_clients,
        "config_path_for_platform",
        lambda: home / "Claude" / "claude_desktop_config.json",
    )
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    return home


def _install_single(target: mcp_clients.ClientTarget) -> mcp_clients.InstallOutcome:
    outcomes = install_targets([target], command=_COMMAND, args=[], env=dict(_ENV))
    assert len(outcomes) == 1
    return outcomes[0]


def _unregister_single(target: mcp_clients.ClientTarget) -> mcp_clients.UnregisterOutcome:
    outcomes = unregister_targets([target])
    assert len(outcomes) == 1
    return outcomes[0]


def test_discover_finds_nothing_when_absent(fake_home: Path, tmp_path: Path) -> None:
    targets = discover_targets(scopes=(SCOPE_USER, SCOPE_PROJECT), project_dir=tmp_path)
    assert targets == []


def test_discover_detects_present_clients(fake_home: Path, tmp_path: Path) -> None:
    (fake_home / ".cursor").mkdir()
    (fake_home / ".codex").mkdir()
    (fake_home / ".claude.json").write_text("{}", encoding="utf-8")

    targets = discover_targets(scopes=(SCOPE_USER, SCOPE_PROJECT), project_dir=tmp_path)
    found = {(t.client_id, t.scope) for t in targets}

    assert ("cursor", SCOPE_USER) in found
    assert ("cursor", SCOPE_PROJECT) in found
    assert ("codex-cli", SCOPE_USER) in found
    assert ("claude-code", SCOPE_PROJECT) in found
    # Windsurf was not made present.
    assert not any(client == "windsurf" for client, _ in found)


def test_install_json_mcpservers_merges_and_preserves(fake_home: Path, tmp_path: Path) -> None:
    cursor_cfg = fake_home / ".cursor" / "mcp.json"
    cursor_cfg.parent.mkdir()
    cursor_cfg.write_text(
        json.dumps({"mcpServers": {"other": {"command": "keep"}}}), encoding="utf-8"
    )

    target = next(
        t for t in discover_targets(scopes=(SCOPE_USER,), project_dir=tmp_path, include=("cursor",))
    )
    outcome = _install_single(target)

    assert outcome.installed is True
    data = json.loads(cursor_cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"]["command"] == "keep"  # preserved
    assert data["mcpServers"]["datacron"]["command"] == _COMMAND
    assert data["mcpServers"]["datacron"]["env"] == _ENV


def test_install_vscode_uses_servers_key_and_type(fake_home: Path, tmp_path: Path) -> None:
    mcp_clients._vscode_user_dir().parent.mkdir(parents=True)  # mark VS Code present
    target = next(
        t for t in discover_targets(scopes=(SCOPE_USER,), project_dir=tmp_path, include=("vscode",))
    )
    outcome = _install_single(target)

    assert outcome.installed is True
    data = json.loads(target.config_path.read_text(encoding="utf-8"))
    assert "servers" in data
    assert data["servers"]["datacron"]["type"] == "stdio"
    assert data["servers"]["datacron"]["command"] == _COMMAND


def test_install_codex_writes_toml_and_preserves(fake_home: Path, tmp_path: Path) -> None:
    codex_cfg = fake_home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir()
    codex_cfg.write_text('model = "gpt-5"\n', encoding="utf-8")

    target = next(
        t
        for t in discover_targets(
            scopes=(SCOPE_USER,), project_dir=tmp_path, include=("codex-cli",)
        )
    )
    outcome = _install_single(target)

    assert outcome.installed is True
    data = tomllib.loads(codex_cfg.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-5"  # preserved
    assert data["mcp_servers"]["datacron"]["command"] == _COMMAND


def test_install_reports_error_on_malformed_json(fake_home: Path, tmp_path: Path) -> None:
    cursor_cfg = fake_home / ".cursor" / "mcp.json"
    cursor_cfg.parent.mkdir()
    cursor_cfg.write_text("{ not valid json", encoding="utf-8")

    target = next(
        t for t in discover_targets(scopes=(SCOPE_USER,), project_dir=tmp_path, include=("cursor",))
    )
    outcome = _install_single(target)

    assert outcome.installed is False
    assert "JSON" in outcome.detail


@pytest.mark.parametrize(
    ("fmt", "servers_key", "payload"),
    [
        (
            "json-mcpservers",
            "mcpServers",
            b'{"mcpServers":{"datacron":{"command":"datacron-mcp"}}}',
        ),
        (
            "json-servers",
            "servers",
            b'{"servers":{"datacron":{"type":"stdio","command":"datacron-mcp"}}}',
        ),
        (
            "toml",
            "mcp_servers",
            b'[mcp_servers.datacron]\ncommand = "datacron-mcp"\n',
        ),
    ],
)
def test_unregister_removes_last_server_and_preserves_empty_container(
    tmp_path: Path,
    fmt: str,
    servers_key: str,
    payload: bytes,
) -> None:
    config_path = tmp_path / ("config.toml" if fmt == "toml" else "config.json")
    config_path.write_bytes(payload)
    target = mcp_clients.ClientTarget("cursor", "Cursor", SCOPE_USER, config_path, fmt)

    outcome = _unregister_single(target)

    assert outcome.successful is True
    assert outcome.changed is True
    assert outcome.detail == ""
    config = (
        tomllib.loads(config_path.read_text(encoding="utf-8"))
        if fmt == "toml"
        else json.loads(config_path.read_text(encoding="utf-8"))
    )
    assert servers_key in config
    assert config[servers_key] == {}


def test_unregister_preserves_other_servers_and_config(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "datacron": {"command": "datacron-mcp"},
                    "other": {"command": "keep"},
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    target = mcp_clients.ClientTarget(
        "cursor", "Cursor", SCOPE_USER, config_path, "json-mcpservers"
    )

    outcome = _unregister_single(target)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert outcome.successful is True
    assert outcome.changed is True
    assert config["mcpServers"] == {"other": {"command": "keep"}}
    assert config["theme"] == "dark"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"theme":"dark"}',
        b'{"mcpServers":{"other":{"command":"keep"}}}',
    ],
)
def test_unregister_noop_does_not_rewrite_existing_json(tmp_path: Path, payload: bytes) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_bytes(payload)
    before_bytes = config_path.read_bytes()
    before_mtime = config_path.stat().st_mtime_ns
    target = mcp_clients.ClientTarget(
        "cursor", "Cursor", SCOPE_USER, config_path, "json-mcpservers"
    )

    outcome = _unregister_single(target)

    assert outcome.successful is True
    assert outcome.changed is False
    assert outcome.detail == "already unregistered"
    assert config_path.read_bytes() == before_bytes
    assert config_path.stat().st_mtime_ns == before_mtime


def test_unregister_missing_file_is_noop_and_does_not_create_it(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.json"
    target = mcp_clients.ClientTarget(
        "cursor", "Cursor", SCOPE_USER, config_path, "json-mcpservers"
    )

    outcome = _unregister_single(target)

    assert outcome.successful is True
    assert outcome.changed is False
    assert outcome.detail == "already unregistered"
    assert not config_path.exists()


def test_unregistration_discovery_ignores_client_presence(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = fake_home / ".cursor" / "mcp.json"
    config_path.parent.mkdir()
    config_path.write_text('{"mcpServers":{"datacron":{}}}', encoding="utf-8")
    monkeypatch.setattr(mcp_clients, "_is_present", lambda _client_id: False)

    install_targets_found = discover_targets(
        scopes=(SCOPE_USER,), project_dir=tmp_path, include=("cursor",)
    )
    unregister_targets_found = discover_unregistration_targets(
        scopes=(SCOPE_USER, SCOPE_PROJECT),
        project_dir=None,
        include=("cursor",),
    )

    assert install_targets_found == []
    assert len(unregister_targets_found) == 1
    assert unregister_targets_found[0].config_path == config_path
    assert unregister_targets_found[0].scope == SCOPE_USER


def test_unregistration_discovery_skips_missing_configs(fake_home: Path, tmp_path: Path) -> None:
    targets = discover_unregistration_targets(
        scopes=(SCOPE_USER, SCOPE_PROJECT),
        project_dir=tmp_path,
        include=("cursor",),
    )
    assert targets == []


@pytest.mark.parametrize(
    ("bad_fmt", "bad_payload"),
    [
        ("json-mcpservers", b"{ not valid json"),
        ("toml", b"[mcp_servers.datacron\ninvalid"),
    ],
)
def test_unregister_reports_malformed_config_and_continues(
    tmp_path: Path,
    bad_fmt: str,
    bad_payload: bytes,
) -> None:
    bad_path = tmp_path / ("bad.toml" if bad_fmt == "toml" else "bad.json")
    bad_path.write_bytes(bad_payload)
    good_path = tmp_path / "good.json"
    good_path.write_text('{"mcpServers":{"datacron":{}}}', encoding="utf-8")
    targets = [
        mcp_clients.ClientTarget("broken", "Broken", SCOPE_USER, bad_path, bad_fmt),
        mcp_clients.ClientTarget("cursor", "Cursor", SCOPE_USER, good_path, "json-mcpservers"),
    ]

    outcomes = unregister_targets(targets)

    assert len(outcomes) == 2
    assert outcomes[0].successful is False
    assert outcomes[0].changed is False
    assert bad_path.read_bytes() == bad_payload
    assert outcomes[1].successful is True
    assert outcomes[1].changed is True
    assert json.loads(good_path.read_text(encoding="utf-8"))["mcpServers"] == {}
