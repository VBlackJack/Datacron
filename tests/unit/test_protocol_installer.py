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
from datacron import setup_wizard
from datacron.cli import app
from datacron.installers import mcp_clients, protocol
from datacron.installers.claude_desktop import MCPServerInvocation
from datacron.installers.protocol import (
    PROTOCOL_ALL,
    PROTOCOL_BLOCK,
    PROTOCOL_CLIENT_IDS,
    PROTOCOL_MARKER_BEGIN,
    PROTOCOL_MARKER_END,
    ProtocolInstallOutcome,
    install_memory_protocol,
    uninstall_memory_protocol,
)

_RUNNER = CliRunner()
_CURSOR_RULE_RELATIVE_PATH = Path(".cursor") / "rules" / "datacron.mdc"
_CURSOR_RULE_FRONTMATTER = "---\ndescription: Datacron memory protocol\nalwaysApply: true\n---"
_VSCODE_RULE_RELATIVE_PATH = Path(".copilot") / "instructions" / "datacron.instructions.md"


def _canonical_cursor_rule_bytes() -> bytes:
    return f"{_CURSOR_RULE_FRONTMATTER}\n{PROTOCOL_BLOCK}\n".encode()


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
        ("gemini-cli", Path(".gemini/GEMINI.md")),
        ("codex-cli", Path(".codex/AGENTS.md")),
        ("windsurf", Path(".codeium/windsurf/memories/global_rules.md")),
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


def test_cursor_install_returns_manual_instructions_without_creating_home_file(
    fake_home: Path,
) -> None:
    outcome = install_memory_protocol("cursor")[0]

    assert outcome.successful is True
    assert outcome.changed is False
    assert outcome.skipped is True
    assert outcome.instruction_path is None
    assert outcome.manual_instructions is not None
    assert "Settings > Rules" in outcome.manual_instructions
    assert PROTOCOL_BLOCK in outcome.manual_instructions
    assert not (fake_home / ".cursor").exists()


def test_vscode_user_rule_is_always_on_idempotent_and_reversible(fake_home: Path) -> None:
    path = fake_home / _VSCODE_RULE_RELATIVE_PATH

    first = install_memory_protocol("vscode")[0]
    installed = path.read_bytes()
    second = install_memory_protocol("vscode")[0]

    assert first.successful is True
    assert first.changed is True
    assert first.instruction_path == path
    assert second.changed is False
    assert installed.startswith(b"---\n")
    assert b'name: Datacron memory protocol\n' in installed
    assert b'applyTo: "**"\n' in installed
    assert installed.count(PROTOCOL_MARKER_BEGIN.encode()) == 1
    assert path.read_bytes() == installed

    removed = uninstall_memory_protocol("vscode")[0]
    absent = uninstall_memory_protocol("vscode")[0]

    assert removed.changed is True
    assert not path.exists()
    assert absent.changed is False
    assert absent.detail == "already absent"


def test_vscode_user_rule_refuses_to_overwrite_foreign_file(fake_home: Path) -> None:
    path = fake_home / _VSCODE_RULE_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    foreign = b'---\napplyTo: "**"\n---\nKeep this rule.\n'
    path.write_bytes(foreign)

    outcome = install_memory_protocol("vscode")[0]

    assert outcome.successful is False
    assert outcome.changed is False
    assert "refusing to overwrite" in outcome.detail
    assert path.read_bytes() == foreign


def test_windsurf_global_rule_limit_fails_without_rewriting(fake_home: Path) -> None:
    path = fake_home / ".codeium" / "windsurf" / "memories" / "global_rules.md"
    path.parent.mkdir(parents=True)
    original = ("x" * 5900).encode()
    path.write_bytes(original)

    outcome = install_memory_protocol("windsurf")[0]

    assert outcome.successful is False
    assert outcome.changed is False
    assert "6000 characters" in outcome.detail
    assert path.read_bytes() == original
    assert not (fake_home / ".cursorrules").exists()


def test_cursor_install_migrates_both_obsolete_paths_safely(fake_home: Path) -> None:
    modern = fake_home / ".cursor" / "rules" / "datacron.mdc"
    modern.parent.mkdir(parents=True)
    modern.write_text(f"{PROTOCOL_BLOCK}\n", encoding="utf-8")
    legacy = fake_home / ".cursorrules"
    legacy.write_text(f"Keep this user rule.\n\n{PROTOCOL_BLOCK}\n", encoding="utf-8")

    outcome = install_memory_protocol("cursor")[0]

    assert outcome.successful is True
    assert outcome.changed is True
    assert outcome.skipped is True
    assert not modern.exists()
    assert legacy.is_file()
    assert legacy.read_text(encoding="utf-8") == "Keep this user rule.\n"


def test_cursor_install_preflights_all_markers_before_migration(fake_home: Path) -> None:
    modern = fake_home / ".cursor" / "rules" / "datacron.mdc"
    modern.parent.mkdir(parents=True)
    modern_bytes = f"{PROTOCOL_BLOCK}\n".encode()
    modern.write_bytes(modern_bytes)
    legacy = fake_home / ".cursorrules"
    legacy_bytes = f"user text\n{PROTOCOL_MARKER_BEGIN}\nunterminated\n".encode()
    legacy.write_bytes(legacy_bytes)

    outcome = install_memory_protocol("cursor")[0]

    assert outcome.successful is False
    assert outcome.changed is False
    assert outcome.skipped is False
    assert "markers" in outcome.detail
    assert modern.read_bytes() == modern_bytes
    assert legacy.read_bytes() == legacy_bytes


def test_cursor_uninstall_deletes_block_only_file(fake_home: Path) -> None:
    modern = fake_home / ".cursor" / "rules" / "datacron.mdc"
    modern.parent.mkdir(parents=True)
    modern.write_text(f"{PROTOCOL_BLOCK}\n", encoding="utf-8")

    outcome = uninstall_memory_protocol("cursor")[0]

    assert outcome.successful is True
    assert outcome.changed is True
    assert not modern.exists()


def test_cursor_project_install_writes_canonical_lf_rule_idempotently(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    rule_path = project_dir / _CURSOR_RULE_RELATIVE_PATH

    first = install_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]
    first_bytes = rule_path.read_bytes()
    second = install_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]

    assert first.successful is True
    assert first.changed is True
    assert first.skipped is False
    assert first.instruction_path == rule_path
    assert first.detail == "installed"
    assert first_bytes == _canonical_cursor_rule_bytes()
    assert first_bytes.startswith(b"---\n")
    assert not first_bytes.startswith(codecs.BOM_UTF8)
    assert b"\r\n" not in first_bytes
    assert second.successful is True
    assert second.changed is False
    assert second.detail == "already installed"
    assert rule_path.read_bytes() == first_bytes


def test_cursor_project_install_refreshes_owned_rule_without_touching_sibling(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    rule_path = project_dir / _CURSOR_RULE_RELATIVE_PATH
    sibling = rule_path.with_name("team.mdc")
    rule_path.parent.mkdir(parents=True)
    rule_path.write_text(
        f"---\r\nalwaysApply: false\r\n---\r\n"
        f"{PROTOCOL_MARKER_BEGIN}\r\nEdited body\r\n{PROTOCOL_MARKER_END}\r\n",
        encoding="utf-8-sig",
        newline="",
    )
    sibling_bytes = b"---\nteam-owned: true\n---\n"
    sibling.write_bytes(sibling_bytes)

    outcome = install_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]

    assert outcome.successful is True
    assert outcome.changed is True
    assert rule_path.read_bytes() == _canonical_cursor_rule_bytes()
    assert sibling.read_bytes() == sibling_bytes


def test_cursor_project_install_refuses_foreign_file_byte_for_byte(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    rule_path = project_dir / _CURSOR_RULE_RELATIVE_PATH
    rule_path.parent.mkdir(parents=True)
    foreign_bytes = codecs.BOM_UTF8 + b"---\r\nalwaysApply: true\r\n---\r\nForeign rule.\r\n"
    rule_path.write_bytes(foreign_bytes)

    outcome = install_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]

    assert outcome.successful is False
    assert outcome.changed is False
    assert outcome.instruction_path == rule_path
    assert "refusing to overwrite" in outcome.detail
    assert rule_path.read_bytes() == foreign_bytes


def test_cursor_project_install_refuses_malformed_owned_markers(tmp_path: Path) -> None:
    rule_path = tmp_path / _CURSOR_RULE_RELATIVE_PATH
    rule_path.parent.mkdir(parents=True)
    malformed_bytes = f"{PROTOCOL_MARKER_BEGIN}\nunterminated\n".encode()
    rule_path.write_bytes(malformed_bytes)

    outcome = install_memory_protocol("cursor", project_dir=tmp_path, scope="project")[0]

    assert outcome.successful is False
    assert outcome.changed is False
    assert "markers" in outcome.detail
    assert rule_path.read_bytes() == malformed_bytes


def test_cursor_project_uninstall_deletes_owned_rule_and_leaves_foreign_file(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    rule_path = project_dir / _CURSOR_RULE_RELATIVE_PATH
    install_memory_protocol("cursor", project_dir=project_dir, scope="project")

    removed = uninstall_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]

    assert removed.successful is True
    assert removed.changed is True
    assert removed.skipped is False
    assert removed.detail == "removed"
    assert not rule_path.exists()

    rule_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_bytes = b"---\nalwaysApply: true\n---\nForeign rule.\n"
    rule_path.write_bytes(foreign_bytes)
    foreign = uninstall_memory_protocol("cursor", project_dir=project_dir, scope="project")[0]

    assert foreign.successful is True
    assert foreign.changed is False
    assert foreign.skipped is True
    assert "foreign file left unchanged" in foreign.detail
    assert rule_path.read_bytes() == foreign_bytes


def test_cursor_project_all_is_not_gated_on_client_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_detection(**_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("Cursor project rules must not depend on local detection")

    monkeypatch.setattr(protocol, "detect_clients", forbidden_detection)

    outcome = install_memory_protocol(PROTOCOL_ALL, project_dir=tmp_path, scope="project")[0]

    assert outcome.client_id == "cursor"
    assert outcome.successful is True
    assert (tmp_path / _CURSOR_RULE_RELATIVE_PATH).is_file()


def test_non_cursor_project_scope_is_an_explicit_skip(tmp_path: Path) -> None:
    outcome = install_memory_protocol("codex-cli", project_dir=tmp_path, scope="project")[0]

    assert outcome.client_id == "codex-cli"
    assert outcome.successful is True
    assert outcome.changed is False
    assert outcome.skipped is True
    assert outcome.detail == "no project-scope protocol target for codex-cli"
    assert not (tmp_path / ".codex").exists()


def test_cursor_project_scope_requires_project_directory() -> None:
    with pytest.raises(ValueError, match="project directory"):
        install_memory_protocol("cursor", scope="project")


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
        lambda **_: ("claude-desktop", "codex-cli", "windsurf", "vscode"),
    )

    outcomes = install_memory_protocol(PROTOCOL_ALL)

    assert outcomes[0].client_id == "claude-desktop"
    assert outcomes[0].skipped is True
    assert "server instructions" in outcomes[0].detail
    assert outcomes[1].instruction_path == fake_home / ".codex" / "AGENTS.md"
    assert outcomes[1].changed is True
    assert outcomes[2].instruction_path == (
        fake_home / ".codeium" / "windsurf" / "memories" / "global_rules.md"
    )
    assert outcomes[2].changed is True
    assert outcomes[3].instruction_path == fake_home / _VSCODE_RULE_RELATIVE_PATH
    assert outcomes[3].changed is True


def test_protocol_clients_cover_every_mcp_client() -> None:
    assert PROTOCOL_CLIENT_IDS == mcp_clients.ALL_CLIENT_IDS


def test_windows_installer_manages_user_protocol_lifecycle() -> None:
    installer = (
        Path(__file__).parents[2] / "packaging" / "windows" / "datacron-installer.iss"
    ).read_text(encoding="utf-8")

    assert "protocol install --client all --scope user" in installer
    assert "protocol uninstall --client all --scope user" in installer


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
    captured: list[tuple[str, Path | None, str]] = []

    def fake_install(
        client: str,
        *,
        project_dir: Path | None = None,
        scope: str = "user",
    ) -> list[ProtocolInstallOutcome]:
        captured.append((client, project_dir, scope))
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
    assert captured == [("codex-cli", None, "user")]
    assert "Codex CLI" in result.output
    assert "installed" in result.output


def test_cli_protocol_install_renders_manual_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_install(
        client: str,
        *,
        project_dir: Path | None = None,
        scope: str = "user",
    ) -> list[ProtocolInstallOutcome]:
        assert client == "cursor"
        assert project_dir is None
        assert scope == "user"
        return [
            ProtocolInstallOutcome(
                client_id="cursor",
                display_name="Cursor",
                instruction_path=None,
                successful=True,
                changed=False,
                skipped=True,
                detail="manual setup required; no user-global rules file",
                manual_instructions="Open Settings > Rules.\nPaste the protocol block.",
            )
        ]

    monkeypatch.setattr(cli_module, "install_memory_protocol", fake_install)

    result = _RUNNER.invoke(app, ["protocol", "install", "--client", "cursor"])

    assert result.exit_code == 0, result.output
    assert "[skip] Cursor" in result.output
    assert "Open Settings > Rules." in result.output
    assert "Paste the protocol block." in result.output


def test_setup_protocol_flag_is_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path | None, str]] = []

    def fake_install(
        client: str,
        *,
        project_dir: Path | None = None,
        scope: str = "user",
    ) -> list[ProtocolInstallOutcome]:
        calls.append((client, project_dir, scope))
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
    assert calls == [(PROTOCOL_ALL, None, "user")]
    assert "No supported clients detected" in with_flag.output


def test_cli_cursor_project_install_and_uninstall_e2e(tmp_path: Path) -> None:
    project_dir = tmp_path / "code-project"
    resolved_rule_path = (project_dir / _CURSOR_RULE_RELATIVE_PATH).resolve()

    installed = _RUNNER.invoke(
        app,
        [
            "protocol",
            "install",
            "--client",
            "cursor",
            "--scope",
            "project",
            "--project",
            str(project_dir),
        ],
    )

    assert installed.exit_code == 0, installed.output
    assert str(resolved_rule_path) in installed.output
    assert "installed" in installed.output
    assert resolved_rule_path.read_bytes() == _canonical_cursor_rule_bytes()

    removed = _RUNNER.invoke(
        app,
        [
            "protocol",
            "uninstall",
            "--client",
            "cursor",
            "--scope",
            "project",
            "--project",
            str(project_dir),
        ],
    )

    assert removed.exit_code == 0, removed.output
    assert str(resolved_rule_path) in removed.output
    assert "removed" in removed.output
    assert not resolved_rule_path.exists()


def test_cli_cursor_project_defaults_to_resolved_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "code-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    rule_path = project_dir / _CURSOR_RULE_RELATIVE_PATH

    result = _RUNNER.invoke(
        app,
        ["protocol", "install", "--client", "cursor", "--scope", "project"],
    )

    assert result.exit_code == 0, result.output
    assert str(rule_path.resolve()) in result.output
    assert rule_path.read_bytes() == _canonical_cursor_rule_bytes()


def test_cli_cursor_both_scope_emits_manual_and_project_outcomes(
    fake_home: Path,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "code-project"
    rule_path = (project_dir / _CURSOR_RULE_RELATIVE_PATH).resolve()

    result = _RUNNER.invoke(
        app,
        [
            "protocol",
            "install",
            "--client",
            "cursor",
            "--scope",
            "both",
            "--project",
            str(project_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.count("Cursor:") == 2
    assert "[skip] Cursor: manual setup required" in result.output
    assert "Settings > Rules" in result.output
    assert str(rule_path) in result.output
    assert rule_path.is_file()
    assert not (fake_home / ".cursor").exists()


def test_setup_cursor_project_protocol_uses_cwd_not_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_project = tmp_path / "code-project"
    vault = tmp_path / "vault"
    code_project.mkdir()
    monkeypatch.chdir(code_project)
    monkeypatch.setattr(
        setup_wizard,
        "resolve_mcp_invocation",
        lambda: MCPServerInvocation(command="datacron-mcp", args=()),
    )
    monkeypatch.setattr(mcp_clients, "_is_present", lambda client: client == "cursor")

    result = _RUNNER.invoke(
        app,
        [
            "setup",
            "--vault",
            str(vault),
            "--client",
            "cursor",
            "--scope",
            "project",
            "--protocol",
            "--no-index",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (vault / ".cursor" / "mcp.json").is_file()
    assert (code_project / _CURSOR_RULE_RELATIVE_PATH).read_bytes() == (
        _canonical_cursor_rule_bytes()
    )
    assert not (vault / _CURSOR_RULE_RELATIVE_PATH).exists()


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
