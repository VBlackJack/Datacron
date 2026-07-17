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
"""Tests for installing the marked memory protocol into client instructions."""

from __future__ import annotations

import codecs
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import datacron.cli as cli_module
from datacron.cli import app
from datacron.installers import mcp_clients, protocol
from datacron.installers.protocol import (
    PROTOCOL_ALL,
    PROTOCOL_BLOCK,
    PROTOCOL_MARKER_BEGIN,
    PROTOCOL_MARKER_END,
    ProtocolInstallOutcome,
    install_memory_protocol,
    uninstall_memory_protocol,
)

_RUNNER = CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect client instruction paths into an isolated home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.mark.parametrize(
    ("client", "relative_path"),
    [
        ("claude-code", Path(".claude/CLAUDE.md")),
        ("cursor", Path(".cursor/rules/datacron.mdc")),
        ("gemini-cli", Path(".gemini/GEMINI.md")),
        ("codex-cli", Path(".codex/AGENTS.md")),
    ],
)
def test_install_and_uninstall_are_idempotent_and_reversible(
    fake_home: Path,
    client: str,
    relative_path: Path,
) -> None:
    path = fake_home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    original = codecs.BOM_UTF8 + b"# User instructions\r\n\r\nKeep this text."
    path.write_bytes(original)

    first = install_memory_protocol(client)
    installed = path.read_bytes()
    second = install_memory_protocol(client)

    assert first[0].changed is True
    assert second[0].changed is False
    assert path.read_bytes() == installed
    assert installed.startswith(original)
    assert installed.startswith(codecs.BOM_UTF8)
    decoded = installed[len(codecs.BOM_UTF8) :].decode("utf-8")
    assert decoded.count(PROTOCOL_MARKER_BEGIN) == 1
    assert decoded.count(PROTOCOL_MARKER_END) == 1
    assert "\r\n## Datacron memory protocol\r\n" in decoded

    removed = uninstall_memory_protocol(client)
    absent = uninstall_memory_protocol(client)

    assert removed[0].changed is True
    assert absent[0].changed is False
    assert path.read_bytes() == original


def test_cursor_uses_existing_legacy_instruction_file(fake_home: Path) -> None:
    legacy = fake_home / ".cursorrules"
    legacy.write_text("legacy rules", encoding="utf-8")

    outcomes = install_memory_protocol("cursor")

    assert outcomes[0].instruction_path == legacy
    assert PROTOCOL_MARKER_BEGIN in legacy.read_text(encoding="utf-8")
    assert not (fake_home / ".cursor" / "rules" / "datacron.mdc").exists()


def test_install_replaces_only_existing_marked_block(fake_home: Path) -> None:
    path = fake_home / ".codex" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"before\n{PROTOCOL_MARKER_BEGIN}\nold protocol\n{PROTOCOL_MARKER_END}\nafter\n",
        encoding="utf-8",
    )

    outcome = install_memory_protocol("codex-cli")[0]
    updated = path.read_text(encoding="utf-8")

    assert outcome.changed is True
    assert updated.startswith("before\n")
    assert updated.endswith("\nafter\n")
    assert "old protocol" not in updated
    assert PROTOCOL_BLOCK in updated


def test_malformed_markers_fail_without_rewriting(fake_home: Path) -> None:
    path = fake_home / ".gemini" / "GEMINI.md"
    path.parent.mkdir(parents=True)
    original = f"user text\n{PROTOCOL_MARKER_BEGIN}\nunterminated\n".encode()
    path.write_bytes(original)

    outcome = install_memory_protocol("gemini-cli")[0]

    assert outcome.successful is False
    assert "markers" in outcome.detail
    assert path.read_bytes() == original


def test_all_uses_shared_detection_and_skips_claude_desktop(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        protocol,
        "detect_clients",
        lambda **_: ("claude-desktop", "codex-cli"),
    )

    outcomes = install_memory_protocol(PROTOCOL_ALL)

    assert outcomes[0].client_id == "claude-desktop"
    assert outcomes[0].skipped is True
    assert "server instructions" in outcomes[0].detail
    assert outcomes[1].instruction_path == fake_home / ".codex" / "AGENTS.md"
    assert outcomes[1].changed is True


def test_shared_detection_reports_present_instruction_client(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_home / ".codex").mkdir()
    monkeypatch.setattr(shutil, "which", lambda _binary: None)

    detected = mcp_clients.detect_clients(include=("codex-cli", "gemini-cli"))

    assert detected == ("codex-cli",)


def test_uninstall_missing_file_is_noop_and_does_not_create_it(fake_home: Path) -> None:
    path = fake_home / ".claude" / "CLAUDE.md"

    outcome = uninstall_memory_protocol("claude-code")[0]

    assert outcome.successful is True
    assert outcome.changed is False
    assert not path.exists()


def test_cli_protocol_install_forwards_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_install(client: str) -> list[ProtocolInstallOutcome]:
        captured.append(client)
        return [
            ProtocolInstallOutcome(
                client_id="codex-cli",
                display_name="Codex CLI",
                instruction_path=Path("/fake/AGENTS.md"),
                successful=True,
                changed=True,
                skipped=False,
                detail="installed",
            )
        ]

    monkeypatch.setattr(cli_module, "install_memory_protocol", fake_install)

    result = _RUNNER.invoke(app, ["protocol", "install", "--client", "codex-cli"])

    assert result.exit_code == 0, result.output
    assert captured == ["codex-cli"]
    assert "Codex CLI" in result.output
    assert "installed" in result.output


def test_setup_protocol_flag_is_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_install(client: str) -> list[ProtocolInstallOutcome]:
        calls.append(client)
        return []

    monkeypatch.setattr(cli_module, "install_memory_protocol", fake_install)
    base_args = [
        "setup",
        "--vault",
        str(tmp_path),
        "--client",
        "none",
        "--no-index",
        "--yes",
    ]

    without_flag = _RUNNER.invoke(app, base_args)
    with_flag = _RUNNER.invoke(app, [*base_args, "--protocol"])

    assert without_flag.exit_code == 0, without_flag.output
    assert with_flag.exit_code == 0, with_flag.output
    assert calls == [PROTOCOL_ALL]
    assert "No supported clients detected" in with_flag.output


def test_cli_protocol_rejects_unknown_client() -> None:
    result = _RUNNER.invoke(app, ["protocol", "install", "--client", "bogus"])

    assert result.exit_code == 1
    assert "Unknown protocol client" in result.output


def test_protocol_block_has_single_marked_source() -> None:
    lines = PROTOCOL_BLOCK.splitlines()

    assert lines[0] == PROTOCOL_MARKER_BEGIN
    assert lines[-1] == PROTOCOL_MARKER_END
    assert 14 <= len(lines) <= 18
    assert "search_text" in PROTOCOL_BLOCK
    assert "create_note_ai" in PROTOCOL_BLOCK
