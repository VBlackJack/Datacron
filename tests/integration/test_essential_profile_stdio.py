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
"""The tool profiles over a real stdio session, as an MCP client sees them."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Implementation

from datacron.core.config import ESSENTIAL_INSTRUCTIONS_MAX_CHARS
from datacron.core.memory_protocol import PROTOCOL_BLOCK
from datacron.mcp.security_manifest import ESSENTIAL_TOOL_NAMES, STANDARD_TOOL_NAMES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DEMO_VAULT = Path(__file__).parents[1] / "fixtures" / "demo-vault"
_ENTRY_COMMAND = "from datacron.cli import mcp_entry; mcp_entry()"
_SUBPROCESS_TIMEOUT_SECONDS = 60


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    shutil.copytree(_DEMO_VAULT, target)
    return target


def _environment(vault: Path, log_dir: Path, profile: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["DATACRON_VAULT_ROOT"] = str(vault)
    env["DATACRON_READ_PATHS"] = str(vault)
    env["DATACRON_WRITE_PATHS"] = str(vault)
    env["DATACRON_LOG_DIR"] = str(log_dir)
    env["DATACRON_LOG_LEVEL"] = "WARNING"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("DATACRON_TOOL_PROFILE", None)
    if profile is not None:
        env["DATACRON_TOOL_PROFILE"] = profile
    return env


def _client(vault: Path, tmp_path: Path, profile: str | None) -> Client:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", _ENTRY_COMMAND],
        env=_environment(vault, tmp_path / "logs", profile),
    )
    return Client(
        stdio_client(params),
        mode="auto",
        client_info=Implementation(name="essential-profile-test", version="1.0"),
    )


async def test_essential_lists_exactly_the_essential_set_and_the_short_instructions(
    vault: Path, tmp_path: Path
) -> None:
    async with _client(vault, tmp_path, "essential") as client:
        tools = await client.list_tools()
        instructions = client.instructions

    assert {tool.name for tool in tools.tools} == ESSENTIAL_TOOL_NAMES
    assert len(tools.tools) == 5
    assert instructions is not None
    assert len(instructions) < ESSENTIAL_INSTRUCTIONS_MAX_CHARS
    assert "`remember`" in instructions
    assert "`recall`" in instructions
    assert "`what_changed`" in instructions
    assert "append_journal" not in instructions


@pytest.mark.parametrize("profile", [None, "standard"])
async def test_standard_lists_exactly_the_twenty_three_tools(
    vault: Path, tmp_path: Path, profile: str | None
) -> None:
    async with _client(vault, tmp_path, profile) as client:
        tools = await client.list_tools()
        instructions = client.instructions

    assert {tool.name for tool in tools.tools} == STANDARD_TOOL_NAMES
    assert len(tools.tools) == 23
    assert instructions == PROTOCOL_BLOCK


async def test_the_verbs_work_end_to_end_over_stdio(vault: Path, tmp_path: Path) -> None:
    async with _client(vault, tmp_path, "essential") as client:
        saved = await client.call_tool(
            "remember",
            {"content": "Stdio memory lands in the note.", "about": "welcome.md"},
        )
        recalled = await client.call_tool("recall", {"question": "welcome", "limit": 2})
        changed = await client.call_tool("what_changed", {"since": "1h"})

    assert saved.is_error is False
    assert saved.structured_content is not None
    assert saved.structured_content["indexed"] is True
    assert saved.structured_content["rel_path"] == "welcome.md"
    assert "Stdio memory lands in the note." in (vault / "welcome.md").read_text(encoding="utf-8")
    assert recalled.is_error is False
    assert recalled.structured_content is not None
    assert recalled.structured_content["returned"] >= 1
    assert changed.is_error is False
    assert changed.structured_content is not None
    assert [group["rel_path"] for group in changed.structured_content["notes"]] == ["welcome.md"]


async def test_an_unknown_profile_refuses_to_start_naming_the_variable_and_the_values(
    vault: Path, tmp_path: Path
) -> None:
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _ENTRY_COMMAND],
        env=_environment(vault, tmp_path / "logs", "bogus"),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "DATACRON_TOOL_PROFILE" in output
    assert "essential" in output
    assert "standard" in output
