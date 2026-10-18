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
"""Unit tests for Dual-Vault separation (DATACRON_VAULT_ROOT + DATACRON_MEMORY_ROOT)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from datacron.bootstrap import initialize_vault
from datacron.cli import app as cli_app
from datacron.core.config import Settings
from datacron.core.operation_log import OperationContext
from datacron.core.paths import (
    PathConfinementError,
    sidecar_dir,
    sidecar_index_db,
    sidecar_vault_config,
)
from datacron.core.vault import ULID_SIDECAR_FILENAME, FilesystemVaultReader
from datacron.indexing.reconcile import reconcile
from datacron.mcp.health import build_health
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _get_note_impl, _list_notes_impl
from datacron.mcp.tools.search import _search_regex_impl, _search_text_impl
from datacron.mcp.tools.session import session_context
from datacron.migration import MigrationError, migrate_vault_to_dual
from datacron.setup_wizard import CLIENT_CLAUDE_CODE, CLIENT_NONE, SetupPlan, run_setup


def test_dual_vault_setup_places_sidecar_and_memory_in_memory_root(tmp_path: Path) -> None:
    """Setup with memory_vault_path keeps the docs vault free of .datacron/."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Runbooks").mkdir(parents=True)
    (docs_vault / "Runbooks" / "deploy.md").write_text(
        "# Deploy\n\nDeployment runbook.\n",
        encoding="utf-8",
    )
    (docs_vault / "_memory").mkdir(parents=True)
    (docs_vault / "_memory" / "residual.md").write_text(
        "# Residual\n\nShould be masked when memory_root is active.\n",
        encoding="utf-8",
    )

    custom_write_paths = [
        memory_vault / "_memory",
        docs_vault / "Runbooks",
    ]
    plan = SetupPlan(
        vault_path=docs_vault,
        memory_vault_path=memory_vault,
        enable_write=True,
        write_paths=custom_write_paths,
        client=CLIENT_CLAUDE_CODE,
    )
    result = asyncio.run(run_setup(plan))

    assert result.memory_vault_path == memory_vault.resolve()
    assert not (docs_vault / ".datacron").exists()
    assert (memory_vault / ".datacron" / "VAULT.yaml").is_file()
    assert sidecar_index_db(docs_vault, memory_root=memory_vault).is_file()
    assert (memory_vault / "_memory").is_dir()
    assert result.index_error is None
    assert result.indexed_notes == 1

    assert result.stdio_config is not None
    snippet = json.loads(result.stdio_config)
    env = snippet["mcpServers"]["datacron"]["env"]
    assert env["DATACRON_VAULT_ROOT"] == str(docs_vault.resolve())
    assert env["DATACRON_MEMORY_ROOT"] == str(memory_vault.resolve())
    assert str(docs_vault.resolve()) in env["DATACRON_READ_PATHS"]
    assert str(memory_vault.resolve()) in env["DATACRON_READ_PATHS"]
    assert str((memory_vault / "_memory").resolve()) in env["DATACRON_WRITE_PATHS"]
    assert str((docs_vault / "Runbooks").resolve()) in env["DATACRON_WRITE_PATHS"]


def test_dual_vault_reads_docs_and_memory_while_masking_residual_docs_memory(
    tmp_path: Path,
) -> None:
    """Reader and scope serve docs from vault_root and _memory from memory_root."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Architecture").mkdir(parents=True)
    (docs_vault / "Architecture" / "overview.md").write_text(
        "# Overview\n\nSystem architecture.\n",
        encoding="utf-8",
    )
    (docs_vault / "_memory").mkdir(parents=True)
    (docs_vault / "_memory" / "masked.md").write_text(
        "# Masked\n\nResidual note in docs vault.\n",
        encoding="utf-8",
    )
    initialize_vault(memory_vault)
    (memory_vault / "_memory").mkdir(parents=True, exist_ok=True)
    (memory_vault / "_memory" / "session.md").write_text(
        "# Session\n\nActive memory note.\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory"],
    )
    app = build_app(settings=settings, vault_root=docs_vault)
    writable_reader = FilesystemVaultReader(
        docs_vault,
        memory_root=memory_vault,
        read_only=False,
    )

    async def _exercise() -> list[str]:
        notes = await writable_reader.list_notes()
        return sorted(note.rel_path for note in notes)

    rel_paths = asyncio.run(_exercise())
    assert rel_paths == ["Architecture/overview.md", "_memory/session.md"]
    assert (memory_vault / ".datacron" / ULID_SIDECAR_FILENAME).is_file()
    assert not (docs_vault / ".datacron").exists()

    with pytest.raises(PathConfinementError):
        app.scope.authorize_path(docs_vault / "_memory" / "masked.md", "read")


def test_dual_vault_writes_to_memory_and_allowlisted_docs_subfolders(
    tmp_path: Path,
) -> None:
    """Writer routes _memory/ to memory_root and allows writes to allowlisted docs folders."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Runbooks").mkdir(parents=True)
    (docs_vault / "Architecture").mkdir(parents=True)
    (docs_vault / "Architecture" / "locked.md").write_text(
        "# Locked\n\nRead-only architecture doc.\n",
        encoding="utf-8",
    )
    initialize_vault(memory_vault)
    (memory_vault / "_memory").mkdir(parents=True, exist_ok=True)

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory", docs_vault / "Runbooks"],
    )
    app = build_app(settings=settings, vault_root=docs_vault)

    async def _exercise() -> dict[str, object]:
        db_path = sidecar_index_db(docs_vault, settings=settings)
        await app.store.open(db_path)
        try:
            await app.vault_writer.write_note_atomic(
                "_memory/decisions.md",
                "---\nid: 01J00000000000000000000001\n---\n# Decisions\n\nUse dual vault.\n",
                overwrite=False,
                note_id="01J00000000000000000000001",
                operation=OperationContext(
                    op="create",
                    tool="create_note_ai",
                    actor="test",
                    parameters={"rel_path": "_memory/decisions.md"},
                ),
            )
            await app.vault_writer.write_note_atomic(
                "Runbooks/incident.md",
                "---\nid: 01J00000000000000000000002\n---\n# Incident\n\nStep 1.\n",
                overwrite=False,
                note_id="01J00000000000000000000002",
                operation=OperationContext(
                    op="create",
                    tool="create_note_ai",
                    actor="test",
                    parameters={"rel_path": "Runbooks/incident.md"},
                ),
            )
            with pytest.raises(PathConfinementError):
                await app.vault_writer.write_note_atomic(
                    "Architecture/ unauthorized.md",
                    "# Unauthorized\n",
                    overwrite=False,
                )
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
            return await build_health(app)
        finally:
            await app.store.close()

    health = asyncio.run(_exercise())

    assert (memory_vault / "_memory" / "decisions.md").is_file()
    assert not (docs_vault / "_memory").exists()
    assert (docs_vault / "Runbooks" / "incident.md").is_file()
    assert not (docs_vault / ".datacron").exists()
    assert sidecar_dir(docs_vault, settings=settings) == (memory_vault / ".datacron").resolve()
    assert sidecar_vault_config(docs_vault, settings=settings).is_file()
    assert health["status"] == "healthy"
    index_health = cast("dict[str, object]", health["index"])
    assert index_health["notes_count"] == 3
    assert index_health["consistent_with_vault"] is True


def test_dual_vault_default_write_paths_split_memory_and_docs(tmp_path: Path) -> None:
    """Default write_paths split _memory to memory_vault and drafts/journal to docs_vault."""
    docs_vault = tmp_path / "docs"
    memory_vault = tmp_path / "memory"
    result = asyncio.run(
        run_setup(
            SetupPlan(
                vault_path=docs_vault,
                memory_vault_path=memory_vault,
                enable_write=True,
                client=CLIENT_NONE,
            )
        )
    )
    assert result.write_paths == [
        (memory_vault / "_memory").resolve(),
        (docs_vault / "_drafts").resolve(),
        (docs_vault / "_journal").resolve(),
    ]
    assert (memory_vault / "_memory").is_dir()
    assert (docs_vault / "_drafts").is_dir()
    assert (docs_vault / "_journal").is_dir()
    assert not (docs_vault / "_memory").exists()
    assert not (docs_vault / ".datacron").exists()


def test_dual_vault_mcp_provenance_and_writability_metadata(tmp_path: Path) -> None:
    """MCP tools report source_vault ('docs'|'memory') and writable booleans per note."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Architecture").mkdir(parents=True)
    (docs_vault / "Architecture" / "spec.md").write_text(
        "---\nid: 01J00000000000000000000010\n---\n"
        "# Spec\n\n## Overview\n\nLocked architecture spec.\n",
        encoding="utf-8",
    )
    (docs_vault / "Runbooks").mkdir(parents=True)
    (docs_vault / "Runbooks" / "ops.md").write_text(
        "---\nid: 01J00000000000000000000011\n---\n# Ops\n\nEditable ops runbook.\n",
        encoding="utf-8",
    )
    initialize_vault(memory_vault)
    (memory_vault / "_memory").mkdir(parents=True, exist_ok=True)
    (memory_vault / "_memory" / "INIT.md").write_text(
        "---\nid: 01J00000000000000000000012\ntags:\n  - state/init\n---\n"
        "# Init\n\nAgent memory bootstrap.\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory", docs_vault / "Runbooks"],
    )
    app = build_app(settings=settings, vault_root=docs_vault)

    async def _exercise() -> None:
        db_path = sidecar_index_db(docs_vault, settings=settings)
        await app.store.open(db_path)
        try:
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)

            listed = await _list_notes_impl(app, folder=None, tags=None, frontmatter=None, limit=20)
            by_path = {item["rel_path"]: item for item in listed["notes"]}
            assert by_path["Architecture/spec.md"]["source_vault"] == "docs"
            assert by_path["Architecture/spec.md"]["writable"] is False
            assert by_path["Runbooks/ops.md"]["source_vault"] == "docs"
            assert by_path["Runbooks/ops.md"]["writable"] is True
            assert by_path["_memory/INIT.md"]["source_vault"] == "memory"
            assert by_path["_memory/INIT.md"]["writable"] is True

            mem_listed = await _list_notes_impl(
                app, folder="_memory", tags=None, frontmatter=None, limit=20
            )
            assert [n["rel_path"] for n in mem_listed["notes"]] == ["_memory/INIT.md"]

            spec_full = await _get_note_impl(
                app,
                id_or_path="Architecture/spec.md",
                fmt="full",
                heading_path=None,
                heading_occurrence=None,
                offset=0,
                limit=None,
            )
            assert spec_full["source_vault"] == "docs"
            assert spec_full["writable"] is False

            spec_map = await _get_note_impl(
                app,
                id_or_path="Architecture/spec.md",
                fmt="map",
                heading_path=None,
                heading_occurrence=None,
                offset=0,
                limit=None,
            )
            assert spec_map["source_vault"] == "docs"
            assert spec_map["writable"] is False

            mem_full = await _get_note_impl(
                app,
                id_or_path="_memory/INIT.md",
                fmt="full",
                heading_path=None,
                heading_occurrence=None,
                offset=0,
                limit=None,
            )
            assert mem_full["source_vault"] == "memory"
            assert mem_full["writable"] is True

            fts_mem = await _search_text_impl(
                app,
                query="bootstrap",
                limit=10,
                include_superseded=False,
                folder="_memory",
                tags=None,
                frontmatter=None,
                group_by_note=False,
            )
            assert len(fts_mem["results"]) == 1
            assert fts_mem["results"][0]["source_vault"] == "memory"
            assert fts_mem["results"][0]["writable"] is True

            rg_mem = await _search_regex_impl(
                app,
                pattern="Agent memory bootstrap",
                glob=None,
                limit=10,
            )
            assert len(rg_mem["results"]) == 1
            assert rg_mem["results"][0]["note_rel_path"] == "_memory/INIT.md"
            assert rg_mem["results"][0]["source_vault"] == "memory"
            assert rg_mem["results"][0]["writable"] is True

            ctx = await session_context(
                app,
                domain="all",
                note_paths=["_memory/INIT.md", "Architecture/spec.md"],
                max_tokens=2000,
                known_contract_hash=None,
            )
            ctx_by_path = {src["rel_path"]: src for src in ctx["sources"]}
            assert ctx_by_path["_memory/INIT.md"]["source_vault"] == "memory"
            assert ctx_by_path["_memory/INIT.md"]["writable"] is True
            assert ctx_by_path["Architecture/spec.md"]["source_vault"] == "docs"
            assert ctx_by_path["Architecture/spec.md"]["writable"] is False

            health = await build_health(app)
            durability = cast("dict[str, object]", health["durability"])
            assert durability["vault_mode"] == "dual"
            assert durability["memory_vault_configured"] is True
            assert durability["extra_read_vault_prefixes"] == []
        finally:
            await app.store.close()

    asyncio.run(_exercise())


def test_extra_read_vaults_multi_vault_mounts_read_search_and_refuse_writes(
    tmp_path: Path,
) -> None:
    """Mounted extra_read_vaults are readable, indexed, searchable, and strictly read-only."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    shared_vault = tmp_path / "shared-runbooks"
    docs_vault.mkdir(parents=True)
    (docs_vault / "local.md").write_text("# Local\n\nMain docs note.\n", encoding="utf-8")
    shared_vault.mkdir(parents=True)
    (shared_vault / "incident.md").write_text(
        "# Incident\n\nShared runbook playbook.\n",
        encoding="utf-8",
    )
    initialize_vault(memory_vault)
    (memory_vault / "_memory").mkdir(parents=True, exist_ok=True)

    settings = Settings.model_validate(
        {
            "vault_root": str(docs_vault),
            "memory_root": str(memory_vault),
            "extra_read_vaults": f"shared={shared_vault}",
            "read_paths": [docs_vault, memory_vault],
            "write_paths": [docs_vault, memory_vault, shared_vault],
        }
    )
    assert settings.extra_read_vaults == {"shared": shared_vault.resolve()}
    assert shared_vault.resolve() in settings.read_paths

    app = build_app(settings=settings, vault_root=docs_vault)

    async def _exercise() -> None:
        db_path = sidecar_index_db(docs_vault, settings=settings)
        await app.store.open(db_path)
        try:
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)

            listed = await _list_notes_impl(
                app, folder="shared", tags=None, frontmatter=None, limit=20
            )
            assert [n["rel_path"] for n in listed["notes"]] == ["shared/incident.md"]
            assert listed["notes"][0]["source_vault"] == "extra"
            assert listed["notes"][0]["source_mount"] == "shared"
            assert listed["notes"][0]["writable"] is False

            note_payload = await _get_note_impl(
                app,
                id_or_path="shared/incident.md",
                fmt="full",
                heading_path=None,
                heading_occurrence=None,
                offset=0,
                limit=None,
            )
            assert note_payload["source_vault"] == "extra"
            assert note_payload["source_mount"] == "shared"
            assert note_payload["writable"] is False

            fts_shared = await _search_text_impl(
                app,
                query="playbook",
                limit=10,
                include_superseded=False,
                folder="shared",
                tags=None,
                frontmatter=None,
                group_by_note=False,
            )
            assert [r["note_rel_path"] for r in fts_shared["results"]] == ["shared/incident.md"]
            assert fts_shared["results"][0]["writable"] is False

            rg_shared = await _search_regex_impl(
                app,
                pattern="Shared runbook playbook",
                glob=None,
                limit=10,
            )
            assert [r["note_rel_path"] for r in rg_shared["results"]] == ["shared/incident.md"]

            with pytest.raises(PathConfinementError):
                app.scope.authorize_rel_path("shared/incident.md", "write")

            health = await build_health(app)
            durability = cast("dict[str, object]", health["durability"])
            assert durability["extra_read_vault_prefixes"] == ["shared"]
        finally:
            await app.store.close()

    asyncio.run(_exercise())


def test_migrate_memory_cleanup_source_cleans_and_keep_source_preserves(
    tmp_path: Path,
) -> None:
    """migrate-memory verifies SHA-256, rebuilds index, and cleans source on --cleanup-source."""
    runner = CliRunner()

    # 1. --cleanup-source removes .datacron/ and _memory/ from docs_vault after verification
    docs_vault = tmp_path / "single-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Guides").mkdir(parents=True)
    (docs_vault / "Guides" / "setup.md").write_text(
        "# Setup Guide\n\nHow to configure.\n",
        encoding="utf-8",
    )
    (docs_vault / "_memory").mkdir(parents=True)
    (docs_vault / "_memory" / "INIT.md").write_text(
        "# Init\n\nMigrated memory content.\n",
        encoding="utf-8",
    )
    init_res = initialize_vault(docs_vault)
    expected_init_bytes = (docs_vault / "_memory" / "INIT.md").read_bytes()

    res = runner.invoke(
        cli_app,
        [
            "migrate-memory",
            "--vault",
            str(docs_vault),
            "--memory-vault",
            str(memory_vault),
            "--cleanup-source",
            "--client",
            "none",
        ],
    )
    assert res.exit_code == 0, res.output
    assert (memory_vault / "_memory" / "INIT.md").read_bytes() == expected_init_bytes
    assert (memory_vault / ".datacron" / "VAULT.yaml").is_file()
    assert sidecar_index_db(docs_vault, memory_root=memory_vault).is_file()
    assert not (docs_vault / "_memory").exists()
    assert not (docs_vault / ".datacron").exists()
    assert (docs_vault / "Guides" / "setup.md").is_file()

    # Verify CLI status and reindex with --memory-vault
    status_res = runner.invoke(
        cli_app,
        ["status", "--vault", str(docs_vault), "--memory-vault", str(memory_vault)],
    )
    assert status_res.exit_code == 0, status_res.output
    assert "memory_root:" in status_res.output
    assert f"vault_id:   {init_res.vault_id}" in status_res.output

    reindex_res = runner.invoke(
        cli_app,
        ["reindex", "--vault", str(docs_vault), "--memory-vault", str(memory_vault)],
    )
    assert reindex_res.exit_code == 0, reindex_res.output

    # 2. --keep-source preserves .datacron/ and _memory/ in source vault
    docs_keep = tmp_path / "keep-vault"
    memory_keep = tmp_path / "keep-memory"
    (docs_keep / "_memory").mkdir(parents=True)
    (docs_keep / "_memory" / "note.md").write_text("# Note\n\nKeep me.\n", encoding="utf-8")
    initialize_vault(docs_keep)

    keep_res = runner.invoke(
        cli_app,
        [
            "migrate-memory",
            "--vault",
            str(docs_keep),
            "--memory-vault",
            str(memory_keep),
            "--keep-source",
            "--client",
            "none",
        ],
    )
    assert keep_res.exit_code == 0, keep_res.output
    assert (memory_keep / "_memory" / "note.md").is_file()
    assert (docs_keep / "_memory" / "note.md").is_file()
    assert (docs_keep / ".datacron" / "VAULT.yaml").is_file()

    # 3. Overlapping roots are rejected
    with pytest.raises(MigrationError):
        asyncio.run(migrate_vault_to_dual(docs_keep, docs_keep / "sub-memory"))
