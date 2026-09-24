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
"""Installers edit files that belong to the user's editor; they must not damage them."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from datacron import setup_wizard
from datacron.cli import app
from datacron.core.durability import atomic_durable_write
from datacron.installers import mcp_clients
from datacron.installers.claude_desktop import MCPServerInvocation
from datacron.installers.foreign_files import BACKUP_SUFFIX, replace_foreign_file
from datacron.installers.mcp_clients import ClientTarget, InstallOutcome, MCPClientError

_runner = CliRunner()


def test_an_unchanged_file_is_not_rewritten_so_its_backup_survives(tmp_path: Path) -> None:
    """The second identical run overwrote the backup with the comment-free rewrite."""
    config = tmp_path / "config.toml"
    config.write_text("# my comment\n[a]\nb = 1\n", encoding="utf-8")

    assert replace_foreign_file(config, b"[a]\nb = 1\n", error=MCPClientError) is True
    backup = config.with_name(config.name + BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8").startswith("# my comment")

    assert replace_foreign_file(config, b"[a]\nb = 1\n", error=MCPClientError) is False
    assert backup.read_text(encoding="utf-8").startswith("# my comment")


def test_a_linked_config_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "mcp.json"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("creating a symlink needs a privilege this host does not grant")

    with pytest.raises(MCPClientError, match="is a link to"):
        replace_foreign_file(link, b'{"x": 1}\n', error=MCPClientError)

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "{}\n"


def test_rerunning_setup_keeps_the_existing_entry_keys(tmp_path: Path) -> None:
    """An upgrade with the write box unchecked turned a writable vault read-only."""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "datacron": {
                        "command": "old-datacron-mcp",
                        "args": [],
                        "env": {
                            "DATACRON_VAULT_ROOT": "C:/vault",
                            "DATACRON_WRITE_PATHS": "C:/vault/_memory",
                            "HTTPS_PROXY": "http://proxy:3128",
                        },
                        "disabled": False,
                        "autoApprove": ["search_text"],
                    },
                    "other": {"command": "other"},
                }
            }
        ),
        encoding="utf-8",
    )

    mcp_clients._merge_json(
        config,
        "mcpServers",
        mcp_clients._stdio_entry(
            "new-datacron-mcp",
            [],
            {"DATACRON_VAULT_ROOT": "C:/vault", "DATACRON_READ_PATHS": "C:/vault"},
        ),
    )

    servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    entry = servers["datacron"]
    assert entry["command"] == "new-datacron-mcp"
    assert entry["env"] == {
        "DATACRON_VAULT_ROOT": "C:/vault",
        "DATACRON_READ_PATHS": "C:/vault",
        "DATACRON_WRITE_PATHS": "C:/vault/_memory",
        "HTTPS_PROXY": "http://proxy:3128",
    }
    assert entry["autoApprove"] == ["search_text"]
    assert entry["disabled"] is False
    assert servers["other"] == {"command": "other"}


def test_a_config_saved_with_a_bom_is_read(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_bytes(b"\xef\xbb\xbf" + b'{"mcpServers": {}}\n')

    mcp_clients._merge_json(config, "mcpServers", mcp_clients._stdio_entry("cmd", [], {}))

    assert "datacron" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_private_file_stays_private_when_replaced(tmp_path: Path) -> None:
    """A 0600 ~/.claude.json came back 0644 after setup."""
    target = tmp_path / "private.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o600)

    atomic_durable_write(target, b'{"a": 1}\n')

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_setup_fails_when_a_requested_client_could_not_be_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer runs setup hidden and trusts the exit code."""
    target = ClientTarget("cursor", "Cursor", "user", tmp_path / "mcp.json", "json-mcpservers")

    def fake_install(targets: Any, *, command: str, args: Any, env: dict[str, str]) -> Any:
        return [
            InstallOutcome(
                "cursor", "Cursor", "user", target.config_path, installed=False, detail="bad"
            )
        ]

    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command="datacron-mcp", args=()),
    )
    monkeypatch.setattr(setup_wizard, "discover_targets", lambda **_kwargs: [target])
    monkeypatch.setattr(setup_wizard, "install_targets", fake_install)

    result = _runner.invoke(
        app,
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--client",
            "all",
            "--scope",
            "user",
            "--yes",
            "--no-index",
        ],
    )

    assert result.exit_code == 1, result.output
