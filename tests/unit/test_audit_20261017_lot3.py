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
"""Regression tests for Audit 2026-10-17 Lot 3 (Dual-Vault migration, graph health, ripgrep)."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path

import pytest

from datacron.bootstrap import initialize_vault
from datacron.core.config import MEMORY_SUBFOLDER_NAME, SIDECAR_DIR_NAME, Settings
from datacron.core.paths import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    IO_REPARSE_TAG_SYMLINK,
    sidecar_index_db,
)
from datacron.graph_health import analyze_graph_health, analyze_vault_graph
from datacron.i18n import t
from datacron.indexing.reconcile import reconcile
from datacron.indexing.ripgrep import search_vault_regex
from datacron.mcp.server import build_app
from datacron.mcp.tools.search import _search_regex_impl
from datacron.migration import (
    SymlinkDetectedError,
    migrate_memory_to_dedicated_vault,
)


def _make_fake_link_stat(real_stat: os.stat_result) -> os.stat_result:
    """Construct a stat_result carrying a symlink mode."""
    st_mode = (real_stat.st_mode & ~0o170000) | stat.S_IFLNK
    return os.stat_result((st_mode, *real_stat[1:]))


def _path_keys(target: Path) -> frozenset[str]:
    """Spellings of target, computed before os.lstat is patched.

    Patching datacron.migration.os.lstat replaces os.lstat process-wide, and
    Path.resolve() calls it on POSIX, so the fake must never resolve.
    """
    return frozenset(
        os.path.normcase(os.path.abspath(candidate)) for candidate in (target, target.resolve())
    )


def _matches(path: str | os.PathLike[str], keys: frozenset[str]) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(path))) in keys


def test_migrate_memory_rejects_symlinks_inside_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """migrate_memory_to_dedicated_vault refuses symlinks or reparse points inside _memory/."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / MEMORY_SUBFOLDER_NAME / "rules").mkdir(parents=True)
    linked_note = docs_vault / MEMORY_SUBFOLDER_NAME / "rules" / "escape.md"
    linked_note.write_text("# Escape\n", encoding="utf-8")
    initialize_vault(docs_vault)

    real_lstat = os.lstat
    linked_note_keys = _path_keys(linked_note)

    def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        st = real_lstat(path)
        if _matches(path, linked_note_keys):
            return _make_fake_link_stat(st)
        return st

    monkeypatch.setattr("datacron.migration.os.lstat", fake_lstat)

    with pytest.raises(SymlinkDetectedError):
        asyncio.run(migrate_memory_to_dedicated_vault(docs_vault, memory_vault))

    assert not (memory_vault / MEMORY_SUBFOLDER_NAME / "rules" / "escape.md").exists()


def test_migrate_memory_rejects_directory_symlink_inside_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """migrate_memory_to_dedicated_vault refuses directory symlinks/junctions inside _memory/."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    linked_dir = docs_vault / MEMORY_SUBFOLDER_NAME / "linked-dir"
    linked_dir.mkdir(parents=True)
    (linked_dir / "note.md").write_text("# Note\n", encoding="utf-8")
    initialize_vault(docs_vault)

    real_lstat = os.lstat
    linked_dir_keys = _path_keys(linked_dir)

    def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        st = real_lstat(path)
        if _matches(path, linked_dir_keys):
            return _make_fake_link_stat(st)
        return st

    monkeypatch.setattr("datacron.migration.os.lstat", fake_lstat)

    with pytest.raises(SymlinkDetectedError):
        asyncio.run(migrate_memory_to_dedicated_vault(docs_vault, memory_vault))


def test_migrate_memory_rejects_home_root_and_localizes_missing_vault(tmp_path: Path) -> None:
    """migrate_memory_to_dedicated_vault rejects Path.home() and localizes missing vault errors."""
    valid_vault = tmp_path / "valid-vault"
    initialize_vault(valid_vault)
    home_root = Path.home().resolve()

    with pytest.raises(ValueError, match=t("cli.migrate_memory.error.unsafe_home_root")):
        asyncio.run(migrate_memory_to_dedicated_vault(home_root, tmp_path / "dst"))

    with pytest.raises(ValueError, match=t("cli.migrate_memory.error.unsafe_home_root")):
        asyncio.run(migrate_memory_to_dedicated_vault(valid_vault, home_root))

    missing_vault = tmp_path / "does-not-exist"
    expected_msg = t("cli.migrate_memory.error.vault_not_found", path=str(missing_vault.resolve()))
    with pytest.raises(NotADirectoryError) as exc_info:
        asyncio.run(migrate_memory_to_dedicated_vault(missing_vault, tmp_path / "dst2"))
    assert str(exc_info.value) == expected_msg


def test_migrate_memory_deletes_sidecar_before_memory_when_cleaning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When keep_source=False, .datacron is removed before _memory so DB locks preserve _memory."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / MEMORY_SUBFOLDER_NAME).mkdir(parents=True)
    (docs_vault / MEMORY_SUBFOLDER_NAME / "INIT.md").write_text(
        "# Init\n\nImportant memory.\n",
        encoding="utf-8",
    )
    initialize_vault(docs_vault)

    removed_order: list[str] = []
    real_rmtree = shutil.rmtree

    def tracking_rmtree(path: str | os.PathLike[str]) -> None:
        target = Path(path)
        if target.parent.resolve() == docs_vault.resolve():
            removed_order.append(target.name)
        real_rmtree(path)

    monkeypatch.setattr("datacron.migration.shutil.rmtree", tracking_rmtree)

    outcome = asyncio.run(
        migrate_memory_to_dedicated_vault(docs_vault, memory_vault, keep_source=False)
    )
    assert outcome.source_cleaned is True
    assert removed_order == [SIDECAR_DIR_NAME, MEMORY_SUBFOLDER_NAME]

    # Simulate a locked .datacron/index/datacron.db failing rmtree(.datacron): _memory must survive
    docs_locked = tmp_path / "docs-locked"
    memory_locked = tmp_path / "memory-locked"
    (docs_locked / MEMORY_SUBFOLDER_NAME).mkdir(parents=True)
    (docs_locked / MEMORY_SUBFOLDER_NAME / "INIT.md").write_text(
        "# Init\n\nMust survive locked sidecar removal.\n",
        encoding="utf-8",
    )
    initialize_vault(docs_locked)

    def failing_sidecar_rmtree(path: str | os.PathLike[str]) -> None:
        target = Path(path)
        if target.resolve() == (docs_locked / SIDECAR_DIR_NAME).resolve():
            raise PermissionError("datacron.db is held open by another process")
        real_rmtree(path)

    monkeypatch.setattr("datacron.migration.shutil.rmtree", failing_sidecar_rmtree)

    with pytest.raises(PermissionError, match=r"datacron\.db"):
        asyncio.run(
            migrate_memory_to_dedicated_vault(docs_locked, memory_locked, keep_source=False)
        )

    assert (docs_locked / MEMORY_SUBFOLDER_NAME / "INIT.md").is_file()


def test_graph_health_single_vault_vs_dual_vault_extra_mounts_and_unresolved_outgoing(
    tmp_path: Path,
) -> None:
    """analyze_graph_health handles Single-Vault, Dual-Vault, extra mounts, and broken links."""
    # 1. Single-Vault mode: links between docs/guide.md and _memory/rules/r.md are NOT cross-vault
    single_vault = tmp_path / "single-vault"
    (single_vault / "docs").mkdir(parents=True)
    (single_vault / MEMORY_SUBFOLDER_NAME / "rules").mkdir(parents=True)
    initialize_vault(single_vault)
    (single_vault / "docs" / "guide.md").write_text(
        "---\nid: 01J00000000000000000000901\ntitle: Guide\n---\n"
        "# Guide\n\nFollows [[Rule One]].\n",
        encoding="utf-8",
    )
    (single_vault / MEMORY_SUBFOLDER_NAME / "rules" / "r.md").write_text(
        "---\nid: 01J00000000000000000000902\ntitle: Rule One\n---\n# Rule One\n\nRule details.\n",
        encoding="utf-8",
    )
    (single_vault / "docs" / "draft.md").write_text(
        "---\nid: 01J00000000000000000000903\ntitle: Draft With Unresolved Link\n---\n"
        "# Draft With Unresolved Link\n\nPoints to [[Future Roadmap]].\n",
        encoding="utf-8",
    )
    (single_vault / "docs" / "true_orphan.md").write_text(
        "---\nid: 01J00000000000000000000904\ntitle: True Orphan\n---\n"
        "# True Orphan\n\nNo incoming or outgoing links.\n",
        encoding="utf-8",
    )

    single_settings = Settings(
        vault_root=single_vault,
        memory_root=None,
        read_paths=[single_vault],
        write_paths=[],
    )
    single_report = asyncio.run(analyze_graph_health(single_vault, settings=single_settings))
    assert single_report.cross_vault_link_count == 0
    assert single_report.cross_vault_links == []
    assert single_report.broken_link_count == 1
    assert single_report.broken_links[0].source_rel_path == "docs/draft.md"
    assert single_report.broken_links[0].target_alias == "Future Roadmap"

    orphan_rel_paths = [item.rel_path for item in single_report.orphan_notes]
    assert "docs/draft.md" not in orphan_rel_paths
    assert "docs/true_orphan.md" in orphan_rel_paths

    # 2. Dual-Vault mode + extra_read_vaults: emits cross_vault_links for memory and extra mounts
    docs_vault = tmp_path / "dual-docs"
    memory_vault = tmp_path / "dual-memory"
    shared_vault = tmp_path / "shared-vault"
    (docs_vault / "docs").mkdir(parents=True)
    shared_vault.mkdir(parents=True)
    initialize_vault(memory_vault)
    (memory_vault / MEMORY_SUBFOLDER_NAME / "rules").mkdir(parents=True, exist_ok=True)

    (docs_vault / "docs" / "guide.md").write_text(
        "---\nid: 01J00000000000000000000911\ntitle: Guide\n---\n"
        "# Guide\n\nSee [[Rule One]] and [[Shared Playbook]].\n",
        encoding="utf-8",
    )
    (memory_vault / MEMORY_SUBFOLDER_NAME / "rules" / "r.md").write_text(
        "---\nid: 01J00000000000000000000912\ntitle: Rule One\n---\n# Rule One\n\nMemory rule.\n",
        encoding="utf-8",
    )
    (shared_vault / "playbook.md").write_text(
        "---\nid: 01J00000000000000000000913\ntitle: Shared Playbook\n---\n"
        "# Shared Playbook\n\nShared content.\n",
        encoding="utf-8",
    )

    dual_settings = Settings.model_validate(
        {
            "vault_root": str(docs_vault),
            "memory_root": str(memory_vault),
            "extra_read_vaults": f"shared={shared_vault}",
            "read_paths": [docs_vault, memory_vault, shared_vault],
            "write_paths": [memory_vault / MEMORY_SUBFOLDER_NAME],
        }
    )
    dual_report = asyncio.run(analyze_vault_graph(docs_vault, settings=dual_settings))
    assert dual_report.cross_vault_link_count == 2
    by_target = {link.target_rel_path: link for link in dual_report.cross_vault_links}
    assert by_target["_memory/rules/r.md"].source_vault == "docs"
    assert by_target["_memory/rules/r.md"].target_vault == "memory"
    assert by_target["shared/playbook.md"].source_vault == "docs"
    assert by_target["shared/playbook.md"].target_vault == "extra:shared"


def test_search_vault_regex_dual_vault_ignores_residual_source_memory(tmp_path: Path) -> None:
    """search_vault_regex and _search_regex_impl ignore residual <vault_root>/_memory files."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Guides").mkdir(parents=True)
    (docs_vault / "Guides" / "intro.md").write_text(
        "---\nid: 01J00000000000000000000951\n---\n# Intro\n\nDocs guide.\n",
        encoding="utf-8",
    )
    # Residual _memory file left in docs_vault (e.g. after migrate-memory --keep-source)
    (docs_vault / MEMORY_SUBFOLDER_NAME).mkdir(parents=True)
    (docs_vault / MEMORY_SUBFOLDER_NAME / "INIT.md").write_text(
        "---\nid: 01J00000000000000000000952\n---\n"
        "# Init\n\nRESIDUAL_ONLY_MARKER and SHARED_MARKER_TOKEN from docs vault.\n",
        encoding="utf-8",
    )

    initialize_vault(memory_vault)
    (memory_vault / MEMORY_SUBFOLDER_NAME).mkdir(parents=True, exist_ok=True)
    (memory_vault / MEMORY_SUBFOLDER_NAME / "INIT.md").write_text(
        "---\nid: 01J00000000000000000000952\n---\n"
        "# Init\n\nDEDICATED_MEMORY_MARKER and SHARED_MARKER_TOKEN from memory vault.\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / MEMORY_SUBFOLDER_NAME],
    )
    app = build_app(settings=settings, vault_root=docs_vault)

    async def _exercise() -> None:
        db_path = sidecar_index_db(docs_vault, settings=settings)
        await app.store.open(db_path)
        try:
            await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)

            # 1. Residual-only marker in <vault_root>/_memory must never match
            residual_hits = await search_vault_regex(
                "RESIDUAL_ONLY_MARKER",
                docs_vault,
                store=app.store,
                settings=settings,
            )
            assert residual_hits == []

            # 2. Shared marker present in both returns 1 hit from memory_vault
            shared_hits = await search_vault_regex(
                "SHARED_MARKER_TOKEN",
                docs_vault,
                store=app.store,
                settings=settings,
            )
            assert len(shared_hits) == 1
            assert shared_hits[0].chunk.note_rel_path == "_memory/INIT.md"
            assert "from memory vault" in shared_hits[0].snippet

            # 3. MCP _search_regex_impl also returns only the dedicated memory match
            mcp_res = await _search_regex_impl(
                app,
                pattern="SHARED_MARKER_TOKEN",
                glob=None,
                limit=10,
            )
            assert len(mcp_res["results"]) == 1
            assert mcp_res["results"][0]["note_rel_path"] == "_memory/INIT.md"
            assert "from memory vault" in mcp_res["results"][0]["snippet"]
        finally:
            await app.store.close()

    asyncio.run(_exercise())
    assert FILE_ATTRIBUTE_REPARSE_POINT > 0
    assert IO_REPARSE_TAG_SYMLINK > 0
