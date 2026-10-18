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
"""Regressions for the 2026-10-18 audit, write path and compaction lot (L5)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from datacron.bootstrap import initialize_vault
from datacron.core.config import Settings
from datacron.core.durability import DurabilityStatus, DurabilityUnavailableError
from datacron.core.frontmatter import parse
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationContext
from datacron.core.paths import PathConfinementError, resolve_vault_rel_target
from datacron.core.scope import SingleTenantVaultScope
from datacron.core.vault_writer import FilesystemVaultWriter, WriteConflictError
from datacron.memory_compaction import compact_vault_memory
from datacron.organization.manifest import (
    load_organization_bundle,
    validate_organization_bundle,
)

_NOTE_ID: Final[str] = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_CONFIRMATION_TOKEN: Final[str] = "a" * 64
_REPORT_SHA256: Final[str] = "b" * 64
_REFERENCE_DATE: Final[date] = date(2026, 3, 15)
_COMPACTION_DAYS: Final[int] = 30


def _operation() -> OperationContext:
    return OperationContext(op="create", tool="create_note_ai", actor="test", parameters={})


def _dual_vault(tmp_path: Path) -> tuple[Path, Path, Settings]:
    docs = tmp_path / "docs"
    memory = tmp_path / "memory"
    docs.mkdir()
    memory.mkdir()
    settings = Settings(
        vault_root=docs,
        memory_root=memory,
        read_paths=[docs, memory],
        write_paths=[docs, memory],
    )
    return docs, memory, settings


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a junction on Windows, a symlink elsewhere; skip when neither works."""
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"directory link creation unavailable: {exc}")


# A-1: a creation at a path whose journalled bytes are gone is not refused forever.


async def test_a1_recreate_after_manual_delete(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await writer.write_note_atomic("a.md", "first\n", overwrite=False, operation=_operation())
    (vault / "a.md").unlink()

    await writer.write_note_atomic("a.md", "second\n", overwrite=False, operation=_operation())

    assert (vault / "a.md").read_bytes() == b"second\n"


async def test_a1_guard_still_refuses_divergent_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await writer.write_note_atomic("a.md", "first\n", overwrite=False, operation=_operation())
    (vault / "a.md").write_bytes(b"edited by hand\n")

    with pytest.raises(WriteConflictError):
        await writer.write_note_atomic("a.md", "third\n", overwrite=True, operation=_operation())
    with pytest.raises(WriteConflictError):
        await writer.mutate_note_atomic("a.md", lambda text: text + "x\n", operation=_operation())
    assert (vault / "a.md").read_bytes() == b"edited by hand\n"


async def test_a1_recreate_at_manifest_move_source(tmp_path: Path) -> None:
    docs, memory, settings = _dual_vault(tmp_path)
    (memory / "_memory" / "facts").mkdir(parents=True)
    (memory / "_memory" / "subjects" / "x").mkdir(parents=True)
    (memory / ".datacron").mkdir()
    (memory / ".datacron" / "VAULT.yaml").write_bytes(
        b"schema_version: 1\nhistory_mode: full\norganization:\n  scope: _memory\n"
        b"  rules:\n    - tag: memory/fact\n      folder: _memory/subjects/x\n"
        b"      naming: kebab-case\n"
    )
    source = "_memory/facts/a.md"
    target = "_memory/subjects/x/a.md"
    source_bytes = (
        f"---\nid: {_NOTE_ID}\ntitle: A fact\ntags:\n  - memory/fact\n---\n\n# A fact\n"
    ).encode()
    writer = FilesystemVaultWriter(docs, settings)
    await writer.write_note_atomic(
        source,
        source_bytes.decode(),
        overwrite=False,
        note_id=_NOTE_ID,
        operation=_operation(),
    )
    (memory / ".datacron" / "ulids.json").write_text(
        json.dumps({source: _NOTE_ID}, indent=2, sort_keys=True)
    )
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "payloads").mkdir(parents=True)
    digest = sha256_bytes(source_bytes)
    (bundle_dir / "payloads" / f"{digest}.md").write_bytes(source_bytes)
    identity = {"id": _NOTE_ID, "aliases": []}
    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "move_replace_exact",
                        "source": source,
                        "target": target,
                        "expected_sha256": digest,
                        "payload_sha256": digest,
                        "expected": identity,
                        "result": identity,
                    }
                ],
            }
        )
    )
    scope = SingleTenantVaultScope(docs, settings)
    bundle = load_organization_bundle(bundle_dir / "manifest.json", vault_root=docs)
    validated = validate_organization_bundle(bundle, vault_root=docs, scope=scope)
    await writer.apply_organization_manifest(
        validated,
        confirmation_token=_CONFIRMATION_TOKEN,
        projected_report_sha256=_REPORT_SHA256,
        precommit_validator=lambda: None,
        operation=OperationContext(
            op="apply_organization_manifest",
            tool="apply_organization_manifest",
            actor="test",
            parameters={},
        ),
    )
    assert not (memory / source).exists()

    await writer.write_note_atomic(
        source, "---\ntitle: new\n---\nnew\n", overwrite=False, operation=_operation()
    )

    assert (memory / source).read_bytes() == b"---\ntitle: new\n---\nnew\n"
    assert (memory / target).read_bytes() == source_bytes


def _write_journal(journal_dir: Path, stem: str, note_id: str, frontmatter: str = "") -> Path:
    path = journal_dir / f"{stem}.md"
    path.write_text(
        f"---\nid: {note_id}\ntitle: {stem}\n{frontmatter}---\n# {stem}\n\nEntry {stem}.\n",
        encoding="utf-8",
    )
    return path


def _compaction_vault(tmp_path: Path) -> tuple[Path, Path, Settings]:
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    docs_vault.mkdir(parents=True)
    initialize_vault(memory_vault)
    journal_dir = memory_vault / "_memory" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory"],
    )
    return docs_vault, journal_dir, settings


async def _compact(docs_vault: Path, settings: Settings) -> None:
    await compact_vault_memory(
        docs_vault,
        settings=settings,
        older_than_days=_COMPACTION_DAYS,
        dry_run=False,
        reference_date=_REFERENCE_DATE,
    )


async def test_a1_compaction_recreates_a_deleted_digest(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    _write_journal(journal_dir, "2026-01-10", "01J00000000000000000000201")
    await _compact(docs_vault, settings)
    digest = journal_dir.parent / "archive" / "journal-2026-01.md"
    assert digest.is_file()
    digest.unlink()
    _write_journal(journal_dir, "2026-01-25", "01J00000000000000000000202")

    await _compact(docs_vault, settings)

    assert digest.is_file()
    assert "2026-01-25" in digest.read_text(encoding="utf-8")


# A-2: a memory path spelled with another case lands where the journal and reader look.


async def test_a2_memory_first_segment_is_canonicalized(tmp_path: Path) -> None:
    docs, memory, settings = _dual_vault(tmp_path)
    target = resolve_vault_rel_target(docs, "_Memory/facts/x.md", settings=settings)
    assert target.relative_to(memory).parts == ("_memory", "facts", "x.md")

    writer = FilesystemVaultWriter(docs, settings)
    await writer.write_note_atomic(
        "_Memory/facts/x.md", "hello\n", overwrite=False, operation=_operation()
    )

    assert "_memory" in os.listdir(memory)
    assert "_Memory" not in os.listdir(memory)
    records = await writer.list_operations()
    assert [record.rel_path for record in records] == ["_memory/facts/x.md"]


def test_a2_padded_memory_spelling_is_not_rewritten(tmp_path: Path) -> None:
    docs, memory, settings = _dual_vault(tmp_path)
    target = resolve_vault_rel_target(docs, " _memory/x.md", settings=settings)
    assert target.relative_to(memory).parts[0] == " _memory"


# A-3: compaction edits only the keys it sets and keeps the rest of the frontmatter text.

_HAND_WRITTEN_KEYS: Final[str] = (
    "# keep me\nmeeting: 14:30\nversion: 1.10\nzip: 01234\nanswer: NO\ntags: [memory/journal]\n"
)


async def test_a3_compaction_preserves_source_frontmatter(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    source = _write_journal(
        journal_dir, "2026-01-10", "01J00000000000000000000201", _HAND_WRITTEN_KEYS
    )

    await _compact(docs_vault, settings)

    text = source.read_text(encoding="utf-8")
    for line in ("# keep me", "meeting: 14:30", "version: 1.10", "zip: 01234", "answer: NO"):
        assert f"\n{line}\n" in text
    assert text.index("meeting: 14:30") < text.index("version: 1.10") < text.index("zip: 01234")
    metadata, _ = parse(text)
    assert "archive/compacted" in metadata["tags"]
    assert metadata["compacted_into"] == "_memory/archive/journal-2026-01.md"


async def test_a3_compaction_preserves_digest_frontmatter(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    archive_dir = journal_dir.parent / "archive"
    archive_dir.mkdir()
    digest = archive_dir / "journal-2026-01.md"
    # A digest that exists before Datacron ever wrote it: no journal baseline.
    digest.write_bytes(
        b"---\nid: 01J00000000000000000000299\n# reviewed by hand\nreviewed: 14:30\n"
        b"version: 1.10\ntags: [type/archive]\n---\n# Journal Digest 2026-01\n"
    )
    _write_journal(journal_dir, "2026-01-25", "01J00000000000000000000202")

    await _compact(docs_vault, settings)

    text = digest.read_text(encoding="utf-8")
    assert "\n# reviewed by hand\nreviewed: 14:30\nversion: 1.10\n" in text
    assert "2026-01-25" in text
    metadata, _ = parse(text)
    assert metadata["id"] == "01J00000000000000000000299"
    assert "archive/journal-digest" in metadata["tags"]


_FLOW_NOTE_ID: Final[str] = "01J00000000000000000000203"
_FLOW_NOTE: Final[bytes] = (
    b"---\n{id: 01J00000000000000000000203, title: flow, tags: [journal]}\n---\n"
    b"# 2026-01-20\n\nFlow entry.\n"
)


async def test_a3_unmarkable_note_is_skipped_and_compaction_continues(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    _write_journal(journal_dir, "2026-01-10", "01J00000000000000000000201")
    flow_note = journal_dir / "2026-01-20.md"
    flow_note.write_bytes(_FLOW_NOTE)
    _write_journal(journal_dir, "2026-02-03", "01J00000000000000000000202")

    preview = await compact_vault_memory(
        docs_vault,
        settings=settings,
        older_than_days=_COMPACTION_DAYS,
        dry_run=True,
        reference_date=_REFERENCE_DATE,
    )
    assert [item.rel_path for item in preview.skipped_notes] == ["_memory/journal/2026-01-20.md"]

    report = await compact_vault_memory(
        docs_vault,
        settings=settings,
        older_than_days=_COMPACTION_DAYS,
        dry_run=False,
        reference_date=_REFERENCE_DATE,
    )

    assert [item.rel_path for item in report.skipped_notes] == ["_memory/journal/2026-01-20.md"]
    assert report.skipped_notes[0].reason
    assert report.compacted_count == 2
    assert report.digests_written == 2
    assert flow_note.read_bytes() == _FLOW_NOTE
    january = (journal_dir.parent / "archive" / "journal-2026-01.md").read_text(encoding="utf-8")
    january_meta, _ = parse(january)
    assert january_meta["compacted_notes"] == ["_memory/journal/2026-01-10.md"]
    assert _FLOW_NOTE_ID not in january
    assert (journal_dir.parent / "archive" / "journal-2026-02.md").is_file()


async def test_a3_unmarkable_digest_skips_only_its_month(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    archive_dir = journal_dir.parent / "archive"
    archive_dir.mkdir()
    january_digest = archive_dir / "journal-2026-01.md"
    digest_bytes = b"---\n{id: 01J00000000000000000000299, title: digest}\n---\n# Digest\n"
    january_digest.write_bytes(digest_bytes)
    january_note = _write_journal(journal_dir, "2026-01-10", "01J00000000000000000000201")
    january_before = january_note.read_bytes()
    _write_journal(journal_dir, "2026-02-03", "01J00000000000000000000202")

    report = await compact_vault_memory(
        docs_vault,
        settings=settings,
        older_than_days=_COMPACTION_DAYS,
        dry_run=False,
        reference_date=_REFERENCE_DATE,
    )

    assert {item.rel_path for item in report.skipped_notes} == {
        "_memory/archive/journal-2026-01.md",
        "_memory/journal/2026-01-10.md",
    }
    assert january_digest.read_bytes() == digest_bytes
    assert january_note.read_bytes() == january_before
    assert report.compacted_count == 1
    assert (archive_dir / "journal-2026-02.md").is_file()


# C-8: compacted notes are archived whatever archive tag list the vault declares.


async def test_c8_compaction_sets_archived_flag(tmp_path: Path) -> None:
    docs_vault, journal_dir, settings = _compaction_vault(tmp_path)
    source = _write_journal(journal_dir, "2026-01-10", "01J00000000000000000000201")

    await _compact(docs_vault, settings)

    source_meta, _ = parse(source.read_text(encoding="utf-8"))
    digest = journal_dir.parent / "archive" / "journal-2026-01.md"
    digest_meta, _ = parse(digest.read_text(encoding="utf-8"))
    assert source_meta["archived"] is True
    assert digest_meta["archived"] is True


# A-4: the link check walks the same spelling the write uses.


def test_a4_leading_space_spelling_does_not_follow_a_link(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "real").mkdir(parents=True)
    (vault / "notes").mkdir()
    _make_dir_link(vault / " notes", vault / "real")
    scope = SingleTenantVaultScope(
        vault, Settings(vault_root=vault, read_paths=[vault], write_paths=[vault])
    )

    with pytest.raises(PathConfinementError):
        scope.authorize_note_write_rel_path(" notes/a.md")
    assert scope.authorize_note_write_rel_path("notes/a.md") == (vault / "notes" / "a.md").resolve()


# A-10: the writer checks links on the lexical path, before resolving and creating folders.


async def test_a10_writer_refuses_a_link_before_creating_folders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "real").mkdir(parents=True)
    _make_dir_link(vault / "linked", vault / "real")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(PathConfinementError):
        await writer.write_note_atomic(
            "linked/new/a.md", "x\n", overwrite=False, operation=_operation()
        )
    assert not (vault / "real" / "new").exists()


# A-6: strict durability probes both roots in Dual-Vault mode.


async def test_a6_strict_mode_probes_the_docs_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs, _memory, settings = _dual_vault(tmp_path)
    strict = settings.model_copy(update={"durability": "strict"})
    resolved_docs = docs.resolve()

    def fake_probe(path: Path) -> DurabilityStatus:
        supported = path.expanduser().resolve() != resolved_docs
        return DurabilityStatus(backend="test", directory_flush_supported=supported)

    monkeypatch.setattr("datacron.core.vault_writer.probe_directory_durability", fake_probe)
    writer = FilesystemVaultWriter(docs, strict)

    with pytest.raises(DurabilityUnavailableError):
        await writer.write_note_atomic("a.md", "x\n", overwrite=False, operation=_operation())
    assert not (docs / "a.md").exists()


async def test_a6_unwritable_docs_root_does_not_block_memory_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs, memory, settings = _dual_vault(tmp_path)
    strict = settings.model_copy(update={"durability": "strict", "write_paths": [memory]})
    resolved_docs = docs.resolve()

    def fake_probe(path: Path) -> DurabilityStatus:
        supported = path.expanduser().resolve() != resolved_docs
        return DurabilityStatus(backend="test", directory_flush_supported=supported)

    monkeypatch.setattr("datacron.core.vault_writer.probe_directory_durability", fake_probe)
    writer = FilesystemVaultWriter(docs, strict)

    await writer.write_note_atomic(
        "_memory/facts/a.md", "x\n", overwrite=False, operation=_operation()
    )
    assert (memory / "_memory" / "facts" / "a.md").read_bytes() == b"x\n"


def test_a6_dual_vault_probe_runs_unpatched(tmp_path: Path) -> None:
    docs, _memory, settings = _dual_vault(tmp_path)
    writer = FilesystemVaultWriter(docs, settings)
    asyncio.run(writer.write_note_atomic("a.md", "x\n", overwrite=False, operation=_operation()))
    assert (docs / "a.md").read_bytes() == b"x\n"
