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
"""Tests for :mod:`datacron.mcp.server`."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from datacron.core.config import Settings
from datacron.core.paths import PathConfinementError, sidecar_index_db, sidecar_vault_config
from datacron.core.vault_writer import OperationRecoveryError, VaultLockBusyError
from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES
from datacron.mcp.server import (
    SERVER_INSTRUCTIONS,
    _startup_recover_operations,
    build_app,
    create_server,
)


def test_server_instructions_include_memory_protocol() -> None:
    assert "create_note_ai" in SERVER_INSTRUCTIONS
    assert "INIT.md" in SERVER_INSTRUCTIONS
    assert "sandbox-wrapped" in SERVER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_tool_annotations_describe_local_effects(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
    )

    annotations = {
        tool.name: tool.annotations.model_dump(exclude_none=True)
        for tool in await create_server(app).list_tools()
        if tool.annotations is not None
    }

    read_names = {
        "list_notes",
        "get_note",
        "search_text",
        "search_regex",
        "get_backlinks",
        "get_health",
        "get_note_history",
        "audit_query",
        "contradiction_scan",
    }
    for name in read_names:
        assert annotations[name] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    for name in ("create_note_ai", "append_journal"):
        assert annotations[name] == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    for name in ("set_frontmatter", "patch_note_section"):
        assert annotations[name] == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    assert annotations["revert_note"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.asyncio
async def test_structured_tool_schemas_are_json_schema_2020_12_compatible(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
    )
    tools = {tool.name: tool for tool in await create_server(app).list_tools()}
    structured_names = {
        "list_notes",
        "get_note",
        "search_text",
        "contradiction_scan",
        "get_health",
        "create_note_ai",
        "append_journal",
        "set_frontmatter",
        "patch_note_section",
        "revert_note",
    }

    for name in structured_names:
        schema = tools[name].outputSchema
        assert schema is not None
        assert schema.get("additionalProperties") is not True
        assert schema.get("properties")
        Draft202012Validator.check_schema(schema)

    assert tools["get_note"].inputSchema["properties"]["format"]["enum"] == [
        "full",
        "map",
        "chunk",
    ]
    create_properties = tools["create_note_ai"].inputSchema["properties"]
    assert set(create_properties["origin"]["enum"]) == {"ai", "human", "merged"}
    assert set(create_properties["confidence"]["enum"]) == {
        "high",
        "medium",
        "low",
        "needs_verification",
    }
    contradiction_properties = tools["contradiction_scan"].inputSchema["properties"]
    assert contradiction_properties["mode"]["enum"] == ["scan", "confirm"]
    assert contradiction_properties["detail"]["enum"] == ["summary", "full"]


@pytest.mark.asyncio
async def test_missing_resource_uses_invalid_params(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
    )

    with pytest.raises(McpError) as error:
        await create_server(app).read_resource("datacron://vault/missing")

    assert error.value.error.code == INVALID_PARAMS


@pytest.mark.asyncio
async def test_write_tool_descriptions_lead_with_usage_trigger(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
    )

    descriptions = {
        tool.name: tool.description
        for tool in await create_server(app).list_tools()
        if tool.name in MUTATING_TOOL_NAMES
    }

    assert descriptions.keys() == MUTATING_TOOL_NAMES
    for description in descriptions.values():
        assert description is not None
        assert description.startswith(("Call this", "Use this"))
    set_frontmatter_description = descriptions["set_frontmatter"]
    assert set_frontmatter_description is not None
    assert (
        "Prefer invalidating an outdated fact (invalid_at + invalidated_by) over deleting or "
        "rewriting it: history stays queryable." in set_frontmatter_description
    )


class TestBuildAppReadPaths:
    def test_read_paths_allow_vault_inside_allowed_root(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        vault = allowed / "vault"
        vault.mkdir(parents=True)
        settings = Settings(read_paths=[allowed], vault_root=vault)

        app = build_app(settings=settings, vault_root=vault)

        assert app.vault_root == vault.resolve()

    def test_read_paths_reject_vault_outside_allowed_root(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        settings = Settings(read_paths=[allowed], vault_root=outside)

        with pytest.raises(PathConfinementError, match="outside the allowed read roots"):
            build_app(settings=settings, vault_root=outside)

    def test_empty_read_paths_keep_vault_root_as_implicit_boundary(self, tmp_path: Path) -> None:
        vault = tmp_path / "outside-any-allowlist"
        vault.mkdir()
        settings = Settings(read_paths=[], vault_root=vault)

        app = build_app(settings=settings, vault_root=vault)

        assert app.vault_root == vault.resolve()


class TestBuildAppQueryExpansion:
    @pytest.mark.asyncio
    async def test_default_store_uses_vault_query_expansion(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        sidecar_vault_config(vault).parent.mkdir(parents=True)
        sidecar_vault_config(vault).write_text(
            """
query_expansion:
  supervision:
    - monitoring
""".lstrip(),
            encoding="utf-8",
        )
        (vault / "monitoring.md").write_text(
            "# Monitoring\n\nOSCARE monitoring guide.\n",
            encoding="utf-8",
        )
        settings = Settings(read_paths=[vault], vault_root=vault)
        app = build_app(settings=settings, vault_root=vault)
        await app.store.open(sidecar_index_db(vault))

        try:
            note = await app.vault_reader.read_note(vault / "monitoring.md")
            await app.store.upsert_note(note, app.chunker.chunk(note))
            results = await app.store.search("supervision", limit=5)
        finally:
            await app.store.close()

        assert {result.chunk.note_rel_path for result in results} == {"monitoring.md"}


class TestStartupRecovery:
    """Startup recovery must not stall tool registration on a contended lock."""

    @pytest.mark.asyncio
    async def test_contended_oplog_lock_does_not_block_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
        app = build_app(settings=settings, vault_root=vault)

        async def _busy_recover() -> int:
            raise VaultLockBusyError(
                "vault lock 'oplog' busy -- another datacron writer is holding it"
            )

        monkeypatch.setattr(app.vault_writer, "recover_operations", _busy_recover)

        # Returns normally: the lifespan can now answer initialize and register
        # tools even while another writer still holds the oplog lock.
        await _startup_recover_operations(app)

    @pytest.mark.asyncio
    async def test_unrelated_recovery_error_still_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
        app = build_app(settings=settings, vault_root=vault)

        async def _broken_recover() -> int:
            raise OperationRecoveryError("history is corrupt")

        monkeypatch.setattr(app.vault_writer, "recover_operations", _broken_recover)

        # Only lock contention is downgraded; genuine recovery failures still abort.
        with pytest.raises(OperationRecoveryError):
            await _startup_recover_operations(app)
