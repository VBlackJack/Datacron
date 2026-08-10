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
from typing import cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from datacron.core.config import Settings
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.paths import PathConfinementError, sidecar_index_db, sidecar_vault_config
from datacron.core.vault_writer import FilesystemVaultWriter, VaultLockBusyError
from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES
from datacron.mcp.server import (
    SERVER_INSTRUCTIONS,
    _startup_recover_operations,
    build_app,
    create_server,
)


def test_server_instructions_include_memory_protocol() -> None:
    assert "create_note_ai" in SERVER_INSTRUCTIONS
    assert "delete_note_section" in SERVER_INSTRUCTIONS
    assert "rename_note_section" in SERVER_INSTRUCTIONS
    assert "current write selector" in SERVER_INSTRUCTIONS
    assert "heading-like lines in fenced code" in SERVER_INSTRUCTIONS
    assert "INIT.md" in SERVER_INSTRUCTIONS
    assert "sandbox-wrapped" in SERVER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_rename_note_section_tool_annotations_describe_local_effects(
    tmp_path: Path,
) -> None:
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
    for name in (
        "set_frontmatter",
        "patch_note_section",
        "delete_note_section",
        "rename_note_section",
    ):
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
async def test_rename_note_section_and_structured_tool_schemas_are_2020_12_compatible(
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
        "delete_note_section",
        "rename_note_section",
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
    set_frontmatter_properties = tools["set_frontmatter"].inputSchema["properties"]
    assert set(create_properties["origin"]["enum"]) == {"ai", "human", "merged"}
    assert set(create_properties["confidence"]["enum"]) == {
        "high",
        "medium",
        "low",
        "needs_verification",
    }
    assert "rejected" in create_properties
    assert "rejected" in set_frontmatter_properties
    contradiction_properties = tools["contradiction_scan"].inputSchema["properties"]
    assert contradiction_properties["mode"]["enum"] == ["scan", "confirm"]
    assert contradiction_properties["detail"]["enum"] == ["summary", "full"]
    health_properties = tools["get_health"].inputSchema["properties"]
    assert health_properties["detail"]["enum"] == ["summary", "full"]
    assert health_properties["detail"]["default"] == "summary"
    assert health_properties["limit"]["default"] == 0
    health_description = tools["get_health"].description or ""
    assert "limit <= 0 selects the server ceiling" in health_description
    assert "settings.max_result_count" in health_description
    assert "opaque baseline identifiers derived from raw keys" in health_description
    assert "not hashes of sanitized published keys" in health_description
    assert "candidate_paths are sanitized display metadata" in health_description
    assert "Findings do not include line numbers" in health_description
    assert "actionable" not in health_description
    delete_tool = tools["delete_note_section"]
    rename_tool = tools["rename_note_section"]
    assert len(tools) == 16
    assert set(delete_tool.inputSchema["properties"]) == {
        "rel_path",
        "heading",
        "expected_hash",
        "heading_level",
    }
    assert delete_tool.inputSchema["required"] == ["rel_path", "heading"]
    assert delete_tool.inputSchema["properties"]["rel_path"]["type"] == "string"
    assert delete_tool.inputSchema["properties"]["heading"]["type"] == "string"
    assert delete_tool.inputSchema["properties"]["expected_hash"]["default"] is None
    assert delete_tool.inputSchema["properties"]["heading_level"]["default"] is None
    delete_output_schema = delete_tool.outputSchema
    patch_output_schema = tools["patch_note_section"].outputSchema
    assert delete_output_schema is not None
    assert patch_output_schema is not None
    assert set(delete_output_schema["properties"]) == {
        "deleted",
        "content_hash",
        "indexed",
    }
    assert delete_output_schema["required"] == ["deleted", "content_hash", "indexed"]
    assert tools["patch_note_section"].inputSchema["required"] == [
        "rel_path",
        "heading",
        "new_content",
    ]
    assert patch_output_schema["required"] == [
        "patched",
        "content_hash",
        "indexed",
    ]
    assert set(rename_tool.inputSchema["properties"]) == {
        "rel_path",
        "heading",
        "new_heading",
        "expected_hash",
        "heading_level",
    }
    assert rename_tool.inputSchema["required"] == ["rel_path", "heading", "new_heading"]
    assert rename_tool.inputSchema["properties"]["rel_path"]["type"] == "string"
    assert rename_tool.inputSchema["properties"]["heading"]["type"] == "string"
    assert rename_tool.inputSchema["properties"]["new_heading"]["type"] == "string"
    assert rename_tool.inputSchema["properties"]["expected_hash"]["default"] is None
    assert rename_tool.inputSchema["properties"]["heading_level"]["default"] is None
    rename_output_schema = rename_tool.outputSchema
    assert rename_output_schema is not None
    assert set(rename_output_schema["properties"]) == {
        "renamed",
        "content_hash",
        "indexed",
    }
    assert rename_output_schema["required"] == ["renamed", "content_hash", "indexed"]


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
async def test_rename_note_section_write_descriptions_lead_with_usage_trigger(
    tmp_path: Path,
) -> None:
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
    for name in ("create_note_ai", "set_frontmatter"):
        assert "'option -- reason'" in (descriptions[name] or "")
    patch_description = descriptions["patch_note_section"]
    assert patch_description is not None
    assert "refuses a level-1 heading that contains subsections" in patch_description
    delete_description = descriptions["delete_note_section"]
    assert delete_description is not None
    assert "H2-H6" in delete_description
    assert "lifecycle invalidation" in delete_description
    rename_description = descriptions["rename_note_section"]
    assert rename_description is not None
    assert "ATX H2-H6" in rename_description
    assert "Setext" in rename_description
    assert "frontmatter title" in rename_description
    assert "current write selector" in rename_description
    assert "collisions recognized by the same selector" in rename_description
    assert "heading-like lines in fenced code" in rename_description


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
            raise OSError("unexpected disk failure")

        monkeypatch.setattr(app.vault_writer, "recover_operations", _broken_recover)

        # Only classified recovery blocks and lock contention are downgraded.
        with pytest.raises(OSError, match="unexpected disk failure"):
            await _startup_recover_operations(app)

    @pytest.mark.asyncio
    async def test_irreconcilable_pending_does_not_abort_startup(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
        app = build_app(settings=settings, vault_root=vault)
        writer = cast(
            "FilesystemVaultWriter",
            vars(app.vault_writer)["_delegate"],
        )
        record = OperationRecord(
            operation_id="startup-blocked-operation",
            timestamp="2026-08-10T00:00:00+00:00",
            op="patch_section",
            tool="patch_note_section",
            note_id=None,
            rel_path="missing.md",
            before_hash=sha256_bytes(b"before\n"),
            after_hash=sha256_bytes(b"after\n"),
            actor="startup-test",
            parameters={},
            history_stored=True,
        )
        writer._operation_journal.write_pending(record)

        await _startup_recover_operations(app)

        assert writer.recovery_blocked[0].operation_id == record.operation_id
        assert "Startup operation-log recovery blocked" in caplog.text
