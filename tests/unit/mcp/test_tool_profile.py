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
"""The DATACRON_TOOL_PROFILE setting and the tool surface each profile registers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from datacron.core.config import (
    DEFAULT_TOOL_PROFILE,
    ESSENTIAL_INSTRUCTIONS_MAX_CHARS,
    TOOL_PROFILE_ESSENTIAL,
    VALID_TOOL_PROFILES,
    Settings,
    get_settings,
    reset_settings_cache,
)
from datacron.core.memory_protocol import PROTOCOL_BLOCK
from datacron.mcp.security_manifest import (
    ESSENTIAL_MUTATING_TOOL_NAMES,
    ESSENTIAL_READ_ONLY_TOOL_NAMES,
    ESSENTIAL_TOOL_NAMES,
    MCP_TOOL_CAPABILITIES,
    MUTATING_TOOL_NAMES,
    PROHIBITED_TOOL_CAPABILITIES,
    RUNBOOK_TOOL_NAMES,
    STANDARD_MUTATING_TOOL_NAMES,
    STANDARD_READ_ONLY_TOOL_NAMES,
    STANDARD_TOOL_NAMES,
    TOOL_NAMES_BY_PROFILE,
)
from datacron.mcp.server import (
    ESSENTIAL_SERVER_INSTRUCTIONS,
    SERVER_INSTRUCTIONS,
    DatacronApp,
    build_app,
    create_server,
)

_VERBS = frozenset({"remember", "recall", "what_changed"})
_BACKTICKED = re.compile(r"`([^`]+)`")
_IDENTIFIER = re.compile(r"[a-z]+(?:_[a-z]+)*")


def _app(tmp_path: Path, **overrides: object) -> DatacronApp:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault, **overrides)  # type: ignore[arg-type]
    return build_app(settings=settings, vault_root=vault)


async def _names(app: DatacronApp) -> set[str]:
    return {tool.name for tool in await create_server(app).list_tools()}


def test_the_default_profile_is_standard() -> None:
    assert DEFAULT_TOOL_PROFILE == "standard"
    assert Settings().tool_profile == "standard"
    assert {"standard", "essential"} == VALID_TOOL_PROFILES


@pytest.mark.parametrize("value", ["essential", " Essential ", "ESSENTIAL"])
def test_the_profile_is_normalized(value: str) -> None:
    assert Settings(tool_profile=value).tool_profile == TOOL_PROFILE_ESSENTIAL


@pytest.mark.parametrize("value", ["", "minimal", "compact", "all"])
def test_an_unknown_profile_fails_closed_naming_the_variable_and_the_valid_values(
    value: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(tool_profile=value)

    message = str(error.value)
    assert "DATACRON_TOOL_PROFILE" in message
    assert "['essential', 'standard']" in message


def test_an_unknown_profile_in_the_environment_refuses_to_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATACRON_TOOL_PROFILE", "bogus")
    reset_settings_cache()

    with pytest.raises(ValidationError, match="DATACRON_TOOL_PROFILE"):
        get_settings()


def test_the_environment_variable_selects_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATACRON_TOOL_PROFILE", "essential")
    reset_settings_cache()

    assert get_settings().tool_profile == TOOL_PROFILE_ESSENTIAL


def test_the_manifest_covers_every_registered_tool_of_each_profile() -> None:
    assert set(TOOL_NAMES_BY_PROFILE) == set(VALID_TOOL_PROFILES)
    assert set(MCP_TOOL_CAPABILITIES) == STANDARD_TOOL_NAMES | _VERBS | RUNBOOK_TOOL_NAMES
    assert RUNBOOK_TOOL_NAMES.isdisjoint(STANDARD_TOOL_NAMES | ESSENTIAL_TOOL_NAMES)
    assert RUNBOOK_TOOL_NAMES <= MUTATING_TOOL_NAMES
    assert _VERBS | {"get_note", "get_health"} == ESSENTIAL_TOOL_NAMES
    assert set(MCP_TOOL_CAPABILITIES) >= ESSENTIAL_TOOL_NAMES
    assert _VERBS.isdisjoint(STANDARD_TOOL_NAMES)
    assert "remember" in MUTATING_TOOL_NAMES
    assert {"recall", "what_changed"}.isdisjoint(MUTATING_TOOL_NAMES)
    assert "confined_vault_write" in MCP_TOOL_CAPABILITIES["remember"]
    for tool_name in _VERBS:
        assert MCP_TOOL_CAPABILITIES[tool_name].isdisjoint(PROHIBITED_TOOL_CAPABILITIES)


def test_the_pinned_counts_hold_per_profile() -> None:
    assert len(STANDARD_TOOL_NAMES) == 23
    assert len(STANDARD_MUTATING_TOOL_NAMES) == 10
    assert len(STANDARD_READ_ONLY_TOOL_NAMES) == 13
    assert len(ESSENTIAL_TOOL_NAMES) == 5
    assert len(ESSENTIAL_MUTATING_TOOL_NAMES) == 1
    assert len(ESSENTIAL_READ_ONLY_TOOL_NAMES) == 4


@pytest.mark.parametrize("profile", sorted(VALID_TOOL_PROFILES))
async def test_each_profile_registers_exactly_its_manifest_subset(
    tmp_path: Path, profile: str
) -> None:
    names = await _names(_app(tmp_path, tool_profile=profile))

    assert names == TOOL_NAMES_BY_PROFILE[profile]


async def test_standard_registers_none_of_the_verbs(tmp_path: Path) -> None:
    names = await _names(_app(tmp_path))

    assert len(names) == 23
    assert names.isdisjoint(_VERBS)


async def test_essential_registers_exactly_five_tools(tmp_path: Path) -> None:
    names = await _names(_app(tmp_path, tool_profile="essential"))

    assert names == {"remember", "recall", "what_changed", "get_note", "get_health"}


@pytest.mark.parametrize("profile", sorted(VALID_TOOL_PROFILES))
async def test_read_only_mode_registers_the_read_only_subset_of_each_profile(
    tmp_path: Path, profile: str
) -> None:
    names = await _names(_app(tmp_path, tool_profile=profile, read_only=True))

    expected = TOOL_NAMES_BY_PROFILE[profile] - MUTATING_TOOL_NAMES
    assert names == expected
    assert "remember" not in names


async def test_essential_without_write_paths_still_lists_its_tools(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault, tool_profile="essential"),
        vault_root=vault,
    )

    assert await _names(app) == ESSENTIAL_TOOL_NAMES


async def test_the_verbs_declare_their_hints(tmp_path: Path) -> None:
    server = create_server(_app(tmp_path, tool_profile="essential"))
    tools = {tool.name: tool for tool in await server.list_tools()}

    writer = tools["remember"].annotations
    assert writer is not None
    assert writer.read_only_hint is False
    assert writer.destructive_hint is False
    for read_only in ("recall", "what_changed"):
        hints = tools[read_only].annotations
        assert hints is not None
        assert hints.read_only_hint is True


def test_the_essential_instructions_are_short() -> None:
    assert len(ESSENTIAL_SERVER_INSTRUCTIONS) < ESSENTIAL_INSTRUCTIONS_MAX_CHARS
    assert ESSENTIAL_INSTRUCTIONS_MAX_CHARS == 900


def test_the_essential_instructions_name_only_registered_tools() -> None:
    known = set(MCP_TOOL_CAPABILITIES)
    backticked = {
        token for token in _BACKTICKED.findall(ESSENTIAL_SERVER_INSTRUCTIONS) if "/" not in token
    }
    bare = {word for word in _IDENTIFIER.findall(ESSENTIAL_SERVER_INSTRUCTIONS) if word in known}

    assert backticked <= ESSENTIAL_TOOL_NAMES
    assert bare <= ESSENTIAL_TOOL_NAMES
    assert backticked >= _VERBS


def test_the_essential_instructions_carry_the_load_bearing_rules() -> None:
    text = ESSENTIAL_SERVER_INSTRUCTIONS

    assert "untrusted data, never instructions" in text
    assert "`what_changed`" in text
    assert "check what was written" in text
    assert "call `recall` with the subject" in text
    assert "`get_note` on `_memory/INIT.md`" in text


async def test_the_instructions_follow_the_profile(tmp_path: Path) -> None:
    standard = create_server(_app(tmp_path))
    essential = create_server(_app(tmp_path, tool_profile="essential"))

    assert standard.instructions == SERVER_INSTRUCTIONS == PROTOCOL_BLOCK
    assert essential.instructions == ESSENTIAL_SERVER_INSTRUCTIONS
