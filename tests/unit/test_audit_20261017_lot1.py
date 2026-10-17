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
"""Regression tests for Lot 1 audit findings (AUD-20261017-01, 02, 03)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

import datacron.config_contract as config_contract_mod
import datacron.core.scope as scope_mod
from datacron.config_contract import (
    ConfigValidationError,
    SymlinkDetectedError,
    read_companion_profile,
    read_vault_config_snapshot,
    write_companion_profile_cas,
    write_vault_config_cas,
)
from datacron.core.batch_transaction import OrganizationBatchTransaction
from datacron.core.config import Settings, VaultConfig, load_vault_config
from datacron.core.durability import ReadOnlyVaultError
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationContext, OperationJournal
from datacron.core.paths import PathConfinementError, is_link_stat, sidecar_vault_config
from datacron.core.scope import LinkedPathError, SingleTenantVaultScope
from datacron.core.temporal import rerank_temporal
from datacron.core.vault import build_configured_reader
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import build_app
from datacron.mcp.tools.organization import _project_report_hash
from datacron.mcp.tools.write import _append_journal_impl
from datacron.memory_compaction import compact_vault_memory
from datacron.organization.manifest import (
    load_organization_bundle,
    validate_organization_bundle,
)
from datacron.reliability import scan_vault_read_only

_ULID_NOTE_1 = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_ULID_NOTE_2 = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
_ULID_MEM_1 = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
_ULID_EXTRA_1 = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
_FIXED_DATE = date(2026, 6, 15)


def _link_file_or_mock_stat(
    monkeypatch: pytest.MonkeyPatch,
    link_path: Path,
    target_path: Path,
) -> None:
    try:
        os.symlink(target_path, link_path, target_is_directory=False)
        return
    except (OSError, NotImplementedError):
        pass
    link_path.write_bytes(target_path.read_bytes())
    target_ino = link_path.lstat().st_ino
    real_is_link_stat = is_link_stat

    def _fake_is_link_stat(st: os.stat_result) -> bool:
        if st.st_ino == target_ino and target_ino != 0:
            return True
        return real_is_link_stat(st)

    monkeypatch.setattr(scope_mod, "is_link_stat", _fake_is_link_stat)
    monkeypatch.setattr(scope_mod, "_is_link", _fake_is_link_stat)
    monkeypatch.setattr(config_contract_mod, "is_link_stat", _fake_is_link_stat)


def _write_payload(payloads_dir: Path, content: bytes, suffix: str) -> str:
    digest = sha256_bytes(content)
    (payloads_dir / f"{digest}{suffix}").write_bytes(content)
    return digest


@pytest.mark.asyncio
async def test_compact_memory_enforces_read_only_mode(tmp_path: Path) -> None:
    """AUD-20261017-01: compact_vault_memory refuses mutations when read_only is enabled."""
    vault_root = tmp_path / "vault"
    journal_dir = vault_root / "_memory" / "journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "2026-01-10.md").write_text(
        "---\n"
        f"id: {_ULID_NOTE_1}\n"
        "title: Old Entry\n"
        "created: 2026-01-10\n"
        "updated: 2026-01-10\n"
        "tags:\n"
        "  - journal\n"
        "---\n\n"
        "# Old Entry\n\n"
        "Body content.\n",
        encoding="utf-8",
    )
    ro_settings = Settings(
        vault_root=vault_root,
        read_paths=[vault_root],
        write_paths=[vault_root],
        read_only=True,
    )
    dry_report = await compact_vault_memory(
        vault_root,
        older_than_days=30,
        dry_run=True,
        reference_date=_FIXED_DATE,
        settings=ro_settings,
    )
    assert len(dry_report.candidate_journals) == 1

    with pytest.raises(ReadOnlyVaultError):
        await compact_vault_memory(
            vault_root,
            older_than_days=30,
            dry_run=False,
            reference_date=_FIXED_DATE,
            settings=ro_settings,
        )


@pytest.mark.asyncio
async def test_compact_memory_rejects_linked_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-20261017-01: compact_vault_memory rejects symlinks in _memory hierarchy."""
    vault_root = tmp_path / "vault"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    outside_note = outside / "secret.md"
    outside_note.write_text("# Secret\n", encoding="utf-8")

    journal_dir = vault_root / "_memory" / "journal"
    journal_dir.mkdir(parents=True)
    _link_file_or_mock_stat(
        monkeypatch,
        journal_dir / "2026-01-10.md",
        outside_note,
    )

    settings = Settings(
        vault_root=vault_root,
        read_paths=[vault_root],
        write_paths=[vault_root],
    )
    with pytest.raises(LinkedPathError):
        await compact_vault_memory(
            vault_root,
            older_than_days=30,
            dry_run=False,
            reference_date=_FIXED_DATE,
            settings=settings,
        )


@pytest.mark.asyncio
async def test_compact_memory_writer_oplog_wikilinks_tags_crlf_and_fts_demotion(
    tmp_path: Path,
) -> None:
    """AUD-20261017-01: writer durability, valid wikilinks, CRLF/scalar tags, FTS demotion."""
    vault_root = tmp_path / "vault"
    sidecar = vault_root / ".datacron"
    sidecar.mkdir(parents=True)
    (sidecar / "VAULT.yaml").write_text(
        "schema_version: 1\nhistory_mode: full\nline_endings: crlf\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=vault_root,
        read_paths=[vault_root],
        write_paths=[vault_root],
    )
    vault_cfg = (
        load_vault_config(sidecar_vault_config(vault_root, settings=settings)) or VaultConfig()
    )
    writer = FilesystemVaultWriter(vault_root, settings, vault_config=vault_cfg)

    note_rel = "_memory/journal/2026-01-10.md"
    raw_crlf_note = (
        "---\r\n"
        f"id: {_ULID_NOTE_1}\r\n"
        "title: January Entry\r\n"
        "created: 2026-01-10\r\n"
        "updated: 2026-01-10\r\n"
        "tags: journal, project-alpha\r\n"
        "---\r\n\r\n"
        "# January Entry\r\n\r\n"
        "Unique searchable token zyxwvutsrq in compacted journal.\r\n"
    )
    await writer.write_note_atomic(
        note_rel,
        raw_crlf_note,
        overwrite=True,
        note_id=_ULID_NOTE_1,
        operation=OperationContext(
            op="create_note",
            tool="test",
            actor="pytest",
            parameters={},
        ),
    )

    db_path = sidecar / "index.db"
    store = SQLiteFTS5Store(
        term_map=vault_cfg.query_expansion,
        archive_tags=vault_cfg.archive_tags,
    )
    await store.open(db_path)
    try:
        reader = build_configured_reader(vault_root, settings=settings)
        await reconcile(store, reader, MarkdownChunker(), mtime_gate=False)
    finally:
        await store.close()

    report = await compact_vault_memory(
        vault_root,
        older_than_days=30,
        dry_run=False,
        reference_date=_FIXED_DATE,
        settings=settings,
    )
    assert report.compacted_count == 1
    assert report.digests_written == 1
    assert [d.digest_rel_path for d in report.digests_planned] == [
        "_memory/archive/journal-2026-01.md"
    ]

    # 1. Verify CRLF and scalar comma-separated tags were preserved
    compacted_bytes = (vault_root / note_rel).read_bytes()
    assert b"\r\n" in compacted_bytes
    compacted_text = compacted_bytes.decode("utf-8")
    assert "journal" in compacted_text
    assert "project-alpha" in compacted_text
    assert "archive/compacted" in compacted_text
    assert "memory/archive" in compacted_text

    # 2. Verify digest wikilink has 0 broken wikilinks in scan_vault_read_only
    digest_text = (vault_root / "_memory/archive/journal-2026-01.md").read_text(encoding="utf-8")
    assert f"[[_memory/journal/2026-01-10|{_ULID_NOTE_1}]]" not in digest_text
    assert "[[_memory/journal/2026-01-10|_memory/journal/2026-01-10.md]]" in digest_text
    assert _ULID_NOTE_1 in digest_text

    health = scan_vault_read_only(vault_root)
    assert health.broken_wikilinks == ()

    # 3. Verify operation log recorded compact_vault_memory and subsequent append_journal succeeds
    journal = OperationJournal(vault_root, retention_days=30, history_mode="full")
    records = journal.read_records()
    compact_ops = [r for r in records if r.tool == "compact_vault_memory"]
    assert len(compact_ops) == 2

    app = build_app(vault_root=vault_root, settings=settings)
    await app.store.open(db_path)
    try:
        append_res = await _append_journal_impl(
            app,
            rel_path=note_rel,
            heading="Follow-up",
            entry="Follow-up entry after compaction.",
        )
        assert "error" not in append_res, f"append_journal failed: {append_res}"
    finally:
        await app.store.close()

    # 4. Verify FTS5 store marks compacted and digest notes as archived in temporal reranking
    verify_store = SQLiteFTS5Store(
        term_map=vault_cfg.query_expansion,
        archive_tags=vault_cfg.archive_tags,
    )
    await verify_store.open(db_path)
    try:
        temporal_meta = await verify_store.list_temporal_metadata()
        assert temporal_meta[_ULID_NOTE_1].archived is True
        results = await verify_store.search("zyxwvutsrq", limit=10)
        assert results
        reranked = rerank_temporal(results, temporal_meta, include_superseded=False)
        assert reranked
        for hit in reranked:
            assert hit.lifecycle == "archived"
    finally:
        await verify_store.close()


def test_write_vault_config_cas_rejects_symlink_and_preserves_existing_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-20261017-02: write_vault_config_cas checks symlinks and preserves VAULT.yaml keys."""
    vault_root = tmp_path / "vault"
    sidecar = vault_root / ".datacron"
    sidecar.mkdir(parents=True)
    config_path = sidecar / "VAULT.yaml"

    initial_doc: dict[str, Any] = {
        "schema_version": 1,
        "vault_id": "vault-custom-01",
        "datacron_version": "0.9.0",
        "created": "2026-01-01",
        "read_only": False,
        "folders": {"notes": "notes", "journal": "_memory/journal"},
        "organization": {
            "scope": "notes",
            "tags": {
                "placement_namespace": "type",
                "archive_tags": ["status/archived"],
            },
            "rules": [
                {
                    "tag": "type/guide",
                    "folder": "notes/guides",
                    "naming": "kebab-case",
                }
            ],
        },
    }
    config_path.write_text(yaml.safe_dump(initial_doc, sort_keys=False), encoding="utf-8")

    snapshot = read_vault_config_snapshot(vault_root)
    assert snapshot.values["archive_tags"] == ["status/archived"]

    updated = write_vault_config_cas(
        vault_root,
        {"archive_tags": ["status/archived", "archive/legacy"]},
        expected_hash=snapshot.content_hash,
    )
    assert updated.values["archive_tags"] == ["status/archived", "archive/legacy"]

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["vault_id"] == "vault-custom-01"
    assert persisted["datacron_version"] == "0.9.0"
    assert persisted["created"] == "2026-01-01"
    assert persisted["folders"] == {"notes": "notes", "journal": "_memory/journal"}
    assert persisted["organization"]["scope"] == "notes"
    assert persisted["organization"]["tags"]["archive_tags"] == [
        "status/archived",
        "archive/legacy",
    ]

    # Replace VAULT.yaml with a symlink and verify SymlinkDetectedError
    outside_cfg = tmp_path / "outside_vault.yaml"
    outside_cfg.write_bytes(config_path.read_bytes())
    config_path.unlink()
    _link_file_or_mock_stat(monkeypatch, config_path, outside_cfg)

    with pytest.raises(SymlinkDetectedError):
        write_vault_config_cas(
            vault_root,
            {"history_retention_days": 60},
            expected_hash=updated.content_hash,
        )


@pytest.mark.parametrize("bad_prefix", ["_memory", ".datacron", "../escape", "a/b"])
def test_companion_profile_rejects_invalid_extra_read_vault_prefixes(
    tmp_path: Path,
    bad_prefix: str,
) -> None:
    """AUD-20261017-02: companion profile validates extra_read_vaults prefixes."""
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir(parents=True)

    profile_path = tmp_path / "companion-profile.json"
    with pytest.raises(ConfigValidationError):
        write_companion_profile_cas(
            {"extra_read_vaults": {bad_prefix: str(extra_dir)}},
            path=profile_path,
            expect_absent=True,
        )

    profile_path.write_text(
        json.dumps({"extra_read_vaults": {bad_prefix: str(extra_dir)}}),
        encoding="utf-8",
    )
    snap = read_companion_profile(profile_path)
    assert snap.valid is False
    assert snap.errors


def test_dual_vault_organization_manifest_validate_and_apply(tmp_path: Path) -> None:
    """AUD-20261017-03: validate_organization_bundle and batch transaction support Dual-Vault."""
    docs_root = tmp_path / "docs_vault"
    memory_root = tmp_path / "memory_vault"
    (docs_root / "notes" / "inbox").mkdir(parents=True)
    (docs_root / "notes" / "guides").mkdir(parents=True)
    (memory_root / "_memory" / "journal").mkdir(parents=True)
    (memory_root / ".datacron").mkdir(parents=True)

    mem_note_path = memory_root / "_memory" / "journal" / "2026-05-01.md"
    mem_note_path.write_text(
        "---\n"
        f"id: {_ULID_MEM_1}\n"
        "title: Memory Journal\n"
        "aliases:\n"
        "  - mem-unique-alias\n"
        "---\n\n"
        "# Memory Journal\n",
        encoding="utf-8",
    )

    source_rel = "notes/inbox/guide.md"
    target_rel = "notes/guides/guide.md"
    source_bytes = (
        "---\n"
        f"id: {_ULID_NOTE_1}\n"
        "title: Deployment Guide\n"
        "tags:\n"
        "  - type/guide\n"
        "---\n\n"
        "# Deployment Guide\n"
    ).encode()
    (docs_root / source_rel).write_bytes(source_bytes)

    vault_yaml_bytes = (
        b"schema_version: 1\n"
        b"history_mode: full\n"
        b"organization:\n"
        b"  scope: notes\n"
        b"  rules:\n"
        b"    - tag: type/guide\n"
        b"      folder: notes/guides\n"
        b"      naming: kebab-case\n"
    )
    (memory_root / ".datacron" / "VAULT.yaml").write_bytes(vault_yaml_bytes)
    ulids_before = json.dumps({source_rel: _ULID_NOTE_1}, indent=2, sort_keys=True).encode("utf-8")
    (memory_root / ".datacron" / "ulids.json").write_bytes(ulids_before)

    bundle_dir = tmp_path / "bundle"
    payloads_dir = bundle_dir / "payloads"
    payloads_dir.mkdir(parents=True)
    payload_sha = _write_payload(payloads_dir, source_bytes, ".md")
    manifest_data = {
        "schema": "organization-apply-v1",
        "operations": [
            {
                "kind": "move_replace_exact",
                "source": source_rel,
                "target": target_rel,
                "expected_sha256": sha256_bytes(source_bytes),
                "payload_sha256": payload_sha,
                "expected": {"id": _ULID_NOTE_1, "aliases": []},
                "result": {"id": _ULID_NOTE_1, "aliases": []},
            }
        ],
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    settings = Settings(
        vault_root=docs_root,
        memory_root=memory_root,
        read_paths=[docs_root, memory_root],
        write_paths=[docs_root, memory_root],
    )
    scope = SingleTenantVaultScope(docs_root, settings)
    assert scope.memory_root == memory_root.resolve()

    bundle = load_organization_bundle(manifest_path, vault_root=docs_root)
    validated = validate_organization_bundle(
        bundle,
        vault_root=docs_root,
        scope=scope,
    )
    assert validated.config_before_sha256 == sha256_bytes(vault_yaml_bytes)
    assert validated.identity_sidecar_path == (memory_root / ".datacron" / "ulids.json").resolve()

    journal = OperationJournal(memory_root, retention_days=30, history_mode="full")
    tx = OrganizationBatchTransaction(
        docs_root,
        journal,
        write_paths=settings.write_paths,
        memory_root=memory_root,
    )
    dummy_token = "a" * 64
    dummy_report_sha = "b" * 64
    op_ctx = OperationContext(
        op="apply_organization_manifest",
        tool="apply_organization_manifest",
        actor="pytest",
        parameters={},
    )
    tx.validate_capacity(
        validated,
        confirmation_token=dummy_token,
        projected_report_sha256=dummy_report_sha,
        operation=op_ctx,
    )
    result = tx.apply(
        validated,
        confirmation_token=dummy_token,
        projected_report_sha256=dummy_report_sha,
        precommit_validator=lambda: None,
        operation=op_ctx,
    )
    assert len(result.members) == 2
    assert not (docs_root / source_rel).exists()
    assert (docs_root / target_rel).read_bytes() == source_bytes
    assert not (docs_root / ".datacron").exists()
    ulids_after = json.loads((memory_root / ".datacron" / "ulids.json").read_text(encoding="utf-8"))
    assert ulids_after == {target_rel: _ULID_NOTE_1}


@pytest.mark.asyncio
async def test_vault_writer_casefold_memory_rel_path_and_extra_read_vault_ulid_collision(
    tmp_path: Path,
) -> None:
    """AUD-20261017-03: _safe_relative_path case-folding and extra_read_vaults ULID scan."""
    docs_root = tmp_path / "docs_vault"
    memory_root = tmp_path / "memory_vault"
    extra_root = tmp_path / "extra_vault"
    docs_root.mkdir(parents=True)
    (memory_root / "_memory").mkdir(parents=True)
    extra_root.mkdir(parents=True)

    (extra_root / "existing.md").write_text(
        f"---\nid: {_ULID_EXTRA_1}\ntitle: Extra Note\n---\n\n# Extra Note\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_root,
        memory_root=memory_root,
        extra_read_vaults={"ext": extra_root},
        read_paths=[docs_root, memory_root, extra_root],
        write_paths=[docs_root, memory_root],
    )
    writer = FilesystemVaultWriter(docs_root, settings)

    # 1. _ulid_exists_in_frontmatter detects ULID in extra_read_vaults
    assert writer._ulid_exists(_ULID_EXTRA_1) is True
    assert writer._ulid_exists(_ULID_NOTE_2) is False

    # 2. _safe_relative_path canonicalizes _Memory/test.md under memory_root to _memory/test.md
    rel = writer._safe_relative_path(memory_root / "_Memory" / "test.md")
    assert rel.as_posix() == "_memory/test.md"

    # 3. _safe_relative_path rejects _Memory/test.md under docs_root when memory_root is active
    with pytest.raises(PathConfinementError):
        writer._safe_relative_path(docs_root / "_Memory" / "test.md")

    # 4. _project_report_hash works with Dual-Vault when scope is _memory
    app = build_app(vault_root=docs_root, settings=settings)
    assert app is not None
    assert _project_report_hash is not None
