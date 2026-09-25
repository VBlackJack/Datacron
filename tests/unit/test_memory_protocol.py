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
"""Distribution checks distinguish installed content from observed behavior."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.memory_protocol import CONTRACT_HASH, CONTRACT_TEXT, PROTOCOL_BLOCK
from datacron.installers.protocol import install_memory_protocol
from datacron.installers.protocol_status import protocol_status
from datacron.mcp.server import SERVER_INSTRUCTIONS


def test_shared_kernel_hash_and_client_ceiling() -> None:
    assert SERVER_INSTRUCTIONS == PROTOCOL_BLOCK
    assert sha256(CONTRACT_TEXT.encode()).hexdigest() == CONTRACT_HASH
    assert len(PROTOCOL_BLOCK) < 6000


def test_status_missing_current_outdated_and_preserves_other_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".codex" / "AGENTS.md"
    target.parent.mkdir()
    target.write_bytes(b"Personal instructions\r\n")
    assert protocol_status("codex-cli")["clients"][0]["distribution"] == "missing"
    install_memory_protocol("codex-cli")
    before = target.read_bytes()
    result = protocol_status("codex-cli")
    assert result["clients"][0]["distribution"] == "current"
    assert result["clients"][0]["behavior"] == "unverified"
    assert target.read_bytes() == before
    assert before.startswith(b"Personal instructions\r\n")
    target.write_text(target.read_text().replace("Enrich people", "Skip people"))
    assert protocol_status("codex-cli")["clients"][0]["distribution"] == "outdated"


def test_manual_and_server_only_status_are_not_compliance() -> None:
    assert protocol_status("cursor")["clients"][0]["distribution"] == "manual"
    assert protocol_status("claude-desktop")["clients"][0]["distribution"] == "manual"


def test_status_cli_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["protocol", "status", "--client", "codex-cli"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["clients"][0]["distribution"] == "missing"
    assert not (tmp_path / ".codex").exists()


def test_status_reports_duplicated_markers_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two protocol blocks in one file make it invalid, not a status traceback.

    The marker scan refuses them with ProtocolInstallError, a RuntimeError the
    inspection did not catch, so `datacron protocol status` crashed.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".codex" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text("Personal instructions\n", encoding="utf-8")
    install_memory_protocol("codex-cli")
    block = target.read_text(encoding="utf-8")
    target.write_text(block + "\n" + block, encoding="utf-8")

    result = CliRunner().invoke(app, ["protocol", "status", "--client", "codex-cli"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["clients"][0]["distribution"] == "invalid"
