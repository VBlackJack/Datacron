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

from datacron.bootstrap import initialize_vault
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
