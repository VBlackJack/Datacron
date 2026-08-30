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

import inspect
import json
import tomllib
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp import MCPError
from mcp.client import Client
from mcp.server import MCPServer
from mcp.types import INVALID_PARAMS

import datacron.mcp.tools.organization as organization_tools
from datacron.core.batch_transaction import (
    BatchApplyResult,
    BatchConflictError,
    BatchMemberResult,
)
from datacron.core.config import Settings
from datacron.core.durability import DurabilityStatus, RecoveryRequiredError
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.paths import PathConfinementError, sidecar_index_db, sidecar_vault_config
from datacron.core.scope import SingleTenantVaultScope
from datacron.core.vault import FilesystemVaultReader, JsonIdStore
from datacron.core.vault_writer import FilesystemVaultWriter, VaultLockBusyError
from datacron.indexing.reconcile import ReconcileStats
from datacron.mcp.security_manifest import MUTATING_TOOL_NAMES
from datacron.mcp.server import (
    SERVER_INSTRUCTIONS,
    DatacronMCPServer,
    _startup_recover_operations,
    build_app,
    create_server,
)


def test_mcp_v2_dependency_and_public_surface_are_explicit() -> None:
    pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert "mcp>=2,<3" in pyproject["project"]["dependencies"]
    assert version("mcp").startswith("2.")

    from mcp.client import Client
    from mcp.server import MCPServer

    assert Client is not None
    assert MCPServer is not None


def test_mcp_transport_sources_use_no_v1_or_private_sdk_surface() -> None:
    repository_root = Path(__file__).parents[3]
    production_paths = (
        "src/datacron/mcp/server.py",
        "src/datacron/mcp/identity.py",
        "src/datacron/mcp/resources.py",
        "src/datacron/mcp/tools/registry.py",
        "src/datacron/mcp/tools/advisory.py",
        "src/datacron/eval/transport.py",
    )
    forbidden_fragments = (
        "mcp.server.fastmcp",
        "mcp.client.session",
        ".isError",
        ".structuredContent",
        ".inputSchema",
        ".outputSchema",
    )

    for relative_path in production_paths:
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        for forbidden_fragment in forbidden_fragments:
            assert forbidden_fragment not in source, relative_path

    private_server_name = "_mcp" + "_server"
    property_source = (
        repository_root / "tests/properties/test_operational_capabilities.py"
    ).read_text(encoding="utf-8")
    assert private_server_name not in property_source


def test_datacron_mcp_server_uses_no_sdk_private_manager_or_context_access() -> None:
    source = inspect.getsource(DatacronMCPServer)

    for forbidden_access in ("_tool_manager", "_resource_manager", "get_context"):
        assert forbidden_access not in source


def test_server_instructions_include_memory_protocol() -> None:
    assert "create_note_ai" in SERVER_INSTRUCTIONS
    assert "delete_note_section" in SERVER_INSTRUCTIONS
    assert "rename_note_section" in SERVER_INSTRUCTIONS
    assert "patch_note_preamble" in SERVER_INSTRUCTIONS
    assert "current write selector" in SERVER_INSTRUCTIONS
    assert "heading-like lines in fenced code" in SERVER_INSTRUCTIONS
    assert "1-based heading_occurrence" in SERVER_INSTRUCTIONS
    assert "exact expected_hash" in SERVER_INSTRUCTIONS
    assert "document order" in SERVER_INSTRUCTIONS
    assert "chunk_id" in SERVER_INSTRUCTIONS
    assert "Setext" in SERVER_INSTRUCTIONS
    assert "heading-like lines in fenced code" in SERVER_INSTRUCTIONS
    assert "closing-ATX" in SERVER_INSTRUCTIONS
    assert "dominant-EOL" in SERVER_INSTRUCTIONS
    assert "INIT.md" in SERVER_INSTRUCTIONS
    assert "sandbox-wrapped" in SERVER_INSTRUCTIONS
    assert "apply_organization_manifest" in SERVER_INSTRUCTIONS
    assert "mode='validate'" in SERVER_INSTRUCTIONS
    assert "confirmation_token" in SERVER_INSTRUCTIONS
    assert "committed_index_incomplete" in SERVER_INSTRUCTIONS
    assert "crash-consistent" in SERVER_INSTRUCTIONS
    assert "multi-path visibility is not instantaneous" in SERVER_INSTRUCTIONS
    assert "Stop other Datacron clients and servers first" in SERVER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_compact_profile_only_changes_search_text_description(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    standard_app = build_app(
        settings=Settings(
            read_paths=[vault],
            vault_root=vault,
            tool_description_profile="standard",
        ),
        vault_root=vault,
    )
    compact_app = build_app(
        settings=Settings(
            read_paths=[vault],
            vault_root=vault,
            tool_description_profile="compact",
        ),
        vault_root=vault,
    )

    standard = {
        tool.name: tool.model_dump() for tool in await create_server(standard_app).list_tools()
    }
    compact = {
        tool.name: tool.model_dump() for tool in await create_server(compact_app).list_tools()
    }

    assert standard["search_text"]["description"] == (
        "First stop for any question about the user's notes, projects, decisions, "
        "or past work - search before saying you do not know. Full-text BM25 search "
        "over the FTS5 index. Returns ranked sandbox-wrapped snippets with **term** "
        "highlighting. Requires `datacron index` to have been run first. By default, "
        "explicitly superseded notes are demoted; set include_superseded=true to "
        "inspect historical notes."
    )
    assert compact["search_text"]["description"] == (
        "Use this tool first for every technical, procedural, project, product, decision, "
        "configuration, release, incident, or past-work query, even when the prompt is "
        "terse or seems answerable from general knowledge. Search before answering, "
        "refusing, or asking for clarification; use get_note after a hit. Full-text BM25 "
        "search over the FTS5 index. Returns ranked sandbox-wrapped snippets with **term** "
        "highlighting. Requires `datacron index` to have been run first. By default, "
        "explicitly superseded notes are demoted; set include_superseded=true to inspect "
        "historical notes."
    )

    standard["search_text"].pop("description")
    compact["search_text"].pop("description")
    assert standard == compact


@pytest.mark.asyncio
async def test_default_production_reader_never_persists_ulid_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "generated.md").write_text("# Generated\n", encoding="utf-8")
    (vault / "migrated.md").write_text("# Migrated\n", encoding="utf-8")
    sidecar = vault / ".datacron" / "ulids.json"
    migrated = sidecar.with_name("ulids.json.migrated")
    migrated.parent.mkdir()
    migrated_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    migrated.write_text(
        '{"migrated.md": "01HQXR7K9YZ8M2N3PQRSTV4WX5"}\n',
        encoding="utf-8",
    )
    write_calls: list[dict[str, str]] = []

    def fail_on_write(_store: JsonIdStore, data: dict[str, str]) -> None:
        write_calls.append(dict(data))
        raise AssertionError("default MCP reader attempted to persist ULID mappings")

    monkeypatch.setattr(JsonIdStore, "_write_sync", fail_on_write)
    app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
        durability_status=DurabilityStatus(
            backend="test-supported",
            directory_flush_supported=True,
        ),
    )

    generated_first = await app.vault_reader.read_note(vault / "generated.md")
    generated_second = await app.vault_reader.read_note(vault / "generated.md")
    migrated_note = await app.vault_reader.read_note(vault / "migrated.md")

    assert app.write_policy.writes_allowed is True
    assert generated_first.id == generated_second.id
    assert migrated_note.id == migrated_id
    assert write_calls == []
    assert not sidecar.exists()
    assert not sidecar.with_suffix(".json.tmp").exists()


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
        tool.name: tool.annotations.model_dump(exclude_none=True, by_alias=True)
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
        "patch_note_preamble",
        "patch_note_section",
        "delete_note_section",
        "rename_note_section",
        "apply_organization_manifest",
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
async def test_organization_manifest_tool_requires_effective_writes(tmp_path: Path) -> None:
    """Do not advertise the batch workflow when no write root is configured."""
    vault = tmp_path / "vault"
    vault.mkdir()
    inactive_app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
    )
    writable_app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
    )
    restricted_settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
    )
    restricted_app = build_app(
        settings=restricted_settings,
        vault_root=vault,
        scope=SingleTenantVaultScope(vault, restricted_settings),
    )

    inactive_names = {tool.name for tool in await create_server(inactive_app).list_tools()}
    writable_names = {tool.name for tool in await create_server(writable_app).list_tools()}
    restricted_names = {tool.name for tool in await create_server(restricted_app).list_tools()}

    assert "apply_organization_manifest" not in inactive_names
    assert "apply_organization_manifest" in writable_names
    assert "apply_organization_manifest" not in restricted_names


@pytest.mark.asyncio
async def test_injected_scope_blocks_pending_organization_recovery_on_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
    )
    app = build_app(
        settings=settings,
        vault_root=vault,
        scope=SingleTenantVaultScope(vault, settings),
    )
    delegate = cast("Any", cast("Any", app.vault_writer)._delegate)

    async def pending() -> bool:
        return True

    monkeypatch.setattr(delegate, "has_pending_organization_batches", pending)

    with pytest.raises(RecoveryRequiredError, match="canonical single-tenant vault scope"):
        await app.vault_writer.write_note_atomic(
            "blocked.md",
            "# Blocked\n",
            overwrite=False,
        )
    with pytest.raises(RecoveryRequiredError, match="canonical single-tenant vault scope"):
        await app.vault_writer.recover_operations()


@pytest.mark.asyncio
async def test_injected_scope_never_delegates_explicit_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
    app = build_app(
        settings=settings,
        vault_root=vault,
        scope=SingleTenantVaultScope(vault, settings),
    )
    delegate = cast("Any", cast("Any", app.vault_writer)._delegate)
    delegated = False

    async def recover() -> int:
        nonlocal delegated
        delegated = True
        return 1

    monkeypatch.setattr(delegate, "recover_operations", recover)

    assert await app.vault_writer.recover_operations() == 0
    assert delegated is False


@pytest.mark.asyncio
async def test_ordinary_writer_refuses_pending_batch_without_recovering_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
    app = build_app(settings=settings, vault_root=vault)
    delegate = cast("Any", cast("Any", app.vault_writer)._delegate)
    transaction = cast("Any", delegate)._batch_transaction
    recovered = False

    def recover() -> Any:
        nonlocal recovered
        recovered = True
        raise AssertionError("ordinary note writes must not recover organization batches")

    monkeypatch.setattr(transaction, "has_pending_batches", lambda: True)
    monkeypatch.setattr(transaction, "recover", recover)

    with pytest.raises(RecoveryRequiredError, match="must be recovered explicitly"):
        await delegate.write_note_atomic(
            "blocked.md",
            "# Blocked\n",
            overwrite=False,
        )

    assert recovered is False
    assert not (vault / "blocked.md").exists()


@pytest.mark.asyncio
async def test_organization_cache_invalidation_reloads_out_of_band_ulid_sidecar(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".datacron" / "ulids.json"
    sidecar.parent.mkdir(parents=True)
    old_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    new_id = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
    later_id = "01HQXR7K9YZ8M2N3PQRSTV4WX7"
    sidecar.write_text(json.dumps({"a.md": old_id}), encoding="utf-8")
    reader = FilesystemVaultReader(tmp_path)
    assert await reader.id_store.snapshot() == {"a.md": old_id}

    sidecar.write_text(json.dumps({"a.md": new_id}), encoding="utf-8")
    await reader.invalidate_alias_cache()

    assert await reader.id_store.get("a.md") == new_id
    assert await reader.id_store.snapshot() == {"a.md": new_id}
    await reader.id_store.set("later.md", later_id)
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "a.md": new_id,
        "later.md": later_id,
    }


def _organization_finalize_case() -> tuple[Any, BatchApplyResult]:
    bundle = SimpleNamespace(
        manifest_sha256="a" * 64,
        payload_set_sha256="b" * 64,
        total_payload_bytes=7,
        manifest=SimpleNamespace(operations=(), config=None),
    )
    result = BatchApplyResult(
        batch_id="a" * 64,
        manifest_sha256="a" * 64,
        confirmation_token="c" * 64,
        projected_report_sha256="d" * 64,
        payload_set_sha256="b" * 64,
        scope_digest="f" * 64,
        config_before_sha256="0" * 64,
        members=(
            BatchMemberResult(
                operation_id=f"organization-{'a' * 64}-0000",
                kind="create_exact",
                source_rel_path=None,
                target_rel_path="memory/result.md",
                before_hash=None,
                after_hash="e" * 64,
                note_id="01J00000000000000000000001",
            ),
        ),
    )
    return bundle, result


@pytest.mark.asyncio
async def test_committed_batch_reports_reconcile_failure_as_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, result = _organization_finalize_case()

    async def removed_ids(_result: BatchApplyResult) -> tuple[str, ...]:
        return ()

    async def fail_reconcile(
        _app: Any,
        *,
        removed_identity_ids: tuple[str, ...],
    ) -> ReconcileStats:
        assert removed_identity_ids == ()
        raise RuntimeError("synthetic reconcile failure")

    monkeypatch.setattr(organization_tools, "_reconcile_batch_locked", fail_reconcile)
    payload, stats, final_hash = await organization_tools._finalize_committed_batch(
        cast(
            "Any",
            SimpleNamespace(
                vault_writer=SimpleNamespace(
                    get_organization_removed_identity_ids=removed_ids,
                )
            ),
        ),
        cast("Any", bundle),
        result,
        None,
        started=0.0,
        mode="apply",
    )

    assert payload is not None
    assert payload["status"] == "committed_index_incomplete"
    assert payload["indexed"] is False
    assert payload["batch_id"] == result.batch_id
    assert payload["identity_sidecar_case_canonicalization_count"] == 0
    assert (
        payload["identity_sidecar_case_canonicalization_sha256"]
        == result.identity_sidecar_case_canonicalization_sha256
    )
    assert stats is None
    assert final_hash is None


@pytest.mark.asyncio
async def test_committed_batch_reports_final_planner_mismatch_as_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, result = _organization_finalize_case()
    reconcile_stats: ReconcileStats = {
        "checked_notes": 0,
        "indexed_notes_before": 0,
        "reindexed_notes": 0,
        "deleted_notes": 0,
        "skipped_notes": 0,
    }

    async def removed_ids(_result: BatchApplyResult) -> tuple[str, ...]:
        return ()

    async def finish_reconcile(
        _app: Any,
        *,
        removed_identity_ids: tuple[str, ...],
    ) -> ReconcileStats:
        assert removed_identity_ids == ()
        return reconcile_stats

    monkeypatch.setattr(organization_tools, "_reconcile_batch_locked", finish_reconcile)
    monkeypatch.setattr(organization_tools, "_current_report_hash", lambda _app: "f" * 64)
    payload, stats, final_hash = await organization_tools._finalize_committed_batch(
        cast(
            "Any",
            SimpleNamespace(
                vault_writer=SimpleNamespace(
                    get_organization_removed_identity_ids=removed_ids,
                )
            ),
        ),
        cast("Any", bundle),
        result,
        None,
        started=0.0,
        mode="apply",
    )

    assert payload is not None
    assert payload["status"] == "committed_report_mismatch"
    assert payload["indexed"] is True
    assert payload["final_report_sha256"] == "f" * 64
    assert payload["identity_sidecar_case_canonicalization_count"] == 0
    assert (
        payload["identity_sidecar_case_canonicalization_sha256"]
        == result.identity_sidecar_case_canonicalization_sha256
    )
    assert stats == reconcile_stats
    assert final_hash == "f" * 64


@pytest.mark.asyncio
async def test_validate_refuses_unrepresentable_receipt_before_returning_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], write_paths=[vault], vault_root=vault),
        vault_root=vault,
    )
    bundle = SimpleNamespace(
        manifest_sha256="a" * 64,
        payload_set_sha256="b" * 64,
        total_payload_bytes=0,
        manifest=SimpleNamespace(operations=(), config=None),
    )
    validated = SimpleNamespace(
        manifest_sha256="a" * 64,
        payload_set_sha256="b" * 64,
        scope_digest="c" * 64,
        operations=(),
        config_payload=None,
        identity_sidecar_after_bytes=None,
        total_payload_bytes=0,
    )
    preview = SimpleNamespace(
        validated=validated,
        config_before_sha256="d" * 64,
        projected_report_sha256="e" * 64,
        confirmation_token="f" * 64,
    )

    monkeypatch.setattr(organization_tools, "_load_expected_bundle", lambda *_args: bundle)
    monkeypatch.setattr(organization_tools, "_build_preview", lambda *_args: preview)

    async def reject_capacity(*_args: Any, **_kwargs: Any) -> None:
        raise BatchConflictError("pending receipt exceeds limit")

    monkeypatch.setattr(
        app.vault_writer,
        "validate_organization_manifest_capacity",
        reject_capacity,
    )
    response = await organization_tools._apply_organization_manifest_impl(
        app,
        manifest_path=str((tmp_path / "manifest.json").resolve()),
        expected_manifest_sha256="a" * 64,
        mode="validate",
    )

    assert "error" in response
    assert "confirmation_token" not in response


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
        "patch_note_preamble",
        "delete_note_section",
        "rename_note_section",
        "revert_note",
        "apply_organization_manifest",
    }

    for name in structured_names:
        schema = tools[name].output_schema
        assert schema is not None
        assert schema.get("additionalProperties") is not True
        assert schema.get("properties")
        Draft202012Validator.check_schema(schema)

    assert tools["get_note"].input_schema["properties"]["format"]["enum"] == [
        "full",
        "map",
        "chunk",
    ]
    create_properties = tools["create_note_ai"].input_schema["properties"]
    set_frontmatter_properties = tools["set_frontmatter"].input_schema["properties"]
    assert set(create_properties["origin"]["enum"]) == {"ai", "human", "merged"}
    assert set(create_properties["confidence"]["enum"]) == {
        "high",
        "medium",
        "low",
        "needs_verification",
    }
    assert "rejected" in create_properties
    assert "rejected" in set_frontmatter_properties
    contradiction_properties = tools["contradiction_scan"].input_schema["properties"]
    assert contradiction_properties["mode"]["enum"] == ["scan", "confirm"]
    assert contradiction_properties["detail"]["enum"] == ["summary", "full"]
    health_properties = tools["get_health"].input_schema["properties"]
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
    assert len(tools) == 18
    organization_tool = tools["apply_organization_manifest"]
    organization_description = organization_tool.description or ""
    assert "crash-consistently apply" in organization_description
    assert "Each file replacement is atomic" in organization_description
    assert "multi-path visibility is not instantaneous" in organization_description
    assert "stop other Datacron clients and servers first" in organization_description
    assert set(organization_tool.input_schema["properties"]) == {
        "manifest_path",
        "expected_manifest_sha256",
        "mode",
        "confirmation_token",
    }
    assert organization_tool.input_schema["required"] == [
        "manifest_path",
        "expected_manifest_sha256",
        "mode",
    ]
    assert organization_tool.input_schema["properties"]["mode"]["enum"] == [
        "validate",
        "apply",
    ]
    assert organization_tool.input_schema["properties"]["confirmation_token"]["default"] is None
    organization_output_schema = organization_tool.output_schema
    assert organization_output_schema is not None
    assert "confirmation_token" in organization_output_schema["properties"]
    assert "projected_report_sha256" in organization_output_schema["properties"]
    assert "final_report_sha256" in organization_output_schema["properties"]
    assert "derived_operation_count" in organization_output_schema["properties"]
    assert "identity_sidecar_replaced" in organization_output_schema["properties"]
    assert (
        "identity_sidecar_case_canonicalization_count" in organization_output_schema["properties"]
    )
    assert (
        "identity_sidecar_case_canonicalization_sha256" in organization_output_schema["properties"]
    )
    assert "committed_error_code" in organization_output_schema["properties"]
    preamble_tool = tools["patch_note_preamble"]
    assert set(preamble_tool.input_schema["properties"]) == {
        "rel_path",
        "new_content",
        "expected_hash",
    }
    assert preamble_tool.input_schema["required"] == [
        "rel_path",
        "new_content",
        "expected_hash",
    ]
    assert preamble_tool.input_schema["properties"]["rel_path"]["type"] == "string"
    assert preamble_tool.input_schema["properties"]["new_content"]["type"] == "string"
    assert preamble_tool.input_schema["properties"]["expected_hash"]["type"] == "string"
    preamble_output_schema = preamble_tool.output_schema
    assert preamble_output_schema is not None
    assert set(preamble_output_schema["properties"]) == {
        "patched",
        "content_hash",
        "indexed",
    }
    assert preamble_output_schema["required"] == ["patched", "content_hash", "indexed"]
    preamble_ref = preamble_output_schema["properties"]["patched"]["$ref"].split("/")[-1]
    assert preamble_output_schema["$defs"][preamble_ref]["required"] == ["rel_path"]
    assert set(delete_tool.input_schema["properties"]) == {
        "rel_path",
        "heading",
        "expected_hash",
        "heading_level",
        "heading_occurrence",
    }
    assert delete_tool.input_schema["required"] == ["rel_path", "heading"]
    assert delete_tool.input_schema["properties"]["rel_path"]["type"] == "string"
    assert delete_tool.input_schema["properties"]["heading"]["type"] == "string"
    assert delete_tool.input_schema["properties"]["expected_hash"]["default"] is None
    assert delete_tool.input_schema["properties"]["heading_level"]["default"] is None
    assert delete_tool.input_schema["properties"]["heading_occurrence"]["default"] is None
    delete_output_schema = delete_tool.output_schema
    patch_output_schema = tools["patch_note_section"].output_schema
    assert delete_output_schema is not None
    assert patch_output_schema is not None
    assert set(delete_output_schema["properties"]) == {
        "deleted",
        "content_hash",
        "indexed",
    }
    assert delete_output_schema["required"] == ["deleted", "content_hash", "indexed"]
    patch_tool = tools["patch_note_section"]
    assert set(patch_tool.input_schema["properties"]) == {
        "rel_path",
        "heading",
        "new_content",
        "expected_hash",
        "heading_level",
        "heading_occurrence",
    }
    assert patch_tool.input_schema["required"] == [
        "rel_path",
        "heading",
        "new_content",
    ]
    assert patch_output_schema["required"] == [
        "patched",
        "content_hash",
        "indexed",
    ]
    assert set(rename_tool.input_schema["properties"]) == {
        "rel_path",
        "heading",
        "new_heading",
        "expected_hash",
        "heading_level",
        "heading_occurrence",
    }
    assert rename_tool.input_schema["required"] == ["rel_path", "heading", "new_heading"]
    assert rename_tool.input_schema["properties"]["rel_path"]["type"] == "string"
    assert rename_tool.input_schema["properties"]["heading"]["type"] == "string"
    assert rename_tool.input_schema["properties"]["new_heading"]["type"] == "string"
    assert rename_tool.input_schema["properties"]["expected_hash"]["default"] is None
    assert rename_tool.input_schema["properties"]["heading_level"]["default"] is None
    assert rename_tool.input_schema["properties"]["heading_occurrence"]["default"] is None
    rename_output_schema = rename_tool.output_schema
    assert rename_output_schema is not None
    assert set(rename_output_schema["properties"]) == {
        "renamed",
        "content_hash",
        "indexed",
    }
    assert rename_output_schema["required"] == ["renamed", "content_hash", "indexed"]
    for tool_name, output_name, required in (
        (
            "patch_note_section",
            "patched",
            ["rel_path", "heading", "level"],
        ),
        (
            "delete_note_section",
            "deleted",
            ["rel_path", "heading", "level"],
        ),
        (
            "rename_note_section",
            "renamed",
            ["rel_path", "old_heading", "new_heading", "level"],
        ),
    ):
        tool = tools[tool_name]
        occurrence_schema = tool.input_schema["properties"]["heading_occurrence"]
        assert occurrence_schema["default"] is None
        assert occurrence_schema["anyOf"] == [{"type": "integer"}, {"type": "null"}]
        output_schema = tool.output_schema
        assert output_schema is not None
        reference = output_schema["properties"][output_name]["$ref"].split("/")[-1]
        selected_schema = output_schema["$defs"][reference]
        assert selected_schema["properties"]["heading_occurrence"]["type"] == "integer"
        assert selected_schema["required"] == required


@pytest.mark.asyncio
async def test_missing_resource_uses_invalid_params(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
    )

    with pytest.raises(MCPError) as error:
        await create_server(app).read_resource("datacron://vault/missing")

    assert error.value.error.code == INVALID_PARAMS


@pytest.mark.asyncio
async def test_unknown_tool_uses_invalid_params_without_private_manager(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
    )

    with pytest.raises(MCPError) as error:
        await create_server(app).call_tool("missing_tool", {})

    assert error.value.error.code == INVALID_PARAMS


@pytest.mark.asyncio
async def test_tool_removed_after_public_listing_still_uses_invalid_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public remove between list and lookup must not become a tool result."""
    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
    )
    server = create_server(app)

    async def transient_tool() -> dict[str, bool]:
        return {"called": True}

    server.add_tool(transient_tool, name="transient_tool")
    listed = await server.list_tools()
    assert "transient_tool" in {tool.name for tool in listed}
    sdk_call_tool = MCPServer.call_tool

    async def remove_then_lookup(
        current_server: MCPServer[Any],
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> Any:
        current_server.remove_tool(name)
        return await sdk_call_tool(current_server, name, arguments, context)

    monkeypatch.setattr(MCPServer, "call_tool", remove_then_lookup)
    async with Client(server, mode="auto") as client:
        with pytest.raises(MCPError) as error:
            await client.call_tool("transient_tool", {})

    assert error.value.code == INVALID_PARAMS


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
    preamble_description = descriptions["patch_note_preamble"]
    assert preamble_description is not None
    assert "strictly before the first ATX heading" in preamble_description
    assert "current write selector" in preamble_description
    assert "Setext" in preamble_description
    assert "heading-like lines in fenced code" in preamble_description
    assert "closing-ATX" in preamble_description
    assert "dominant-EOL" in preamble_description
    assert "exact expected_hash" in preamble_description
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
    for description in (patch_description, delete_description, rename_description):
        assert "1-based heading_occurrence" in description
        assert "heading_level" in description
        assert "exact expected_hash" in description
        assert "document order" in description
        assert "chunk_id" in description


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
