# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.vault_writer`."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import datacron.core.vault_writer as vault_writer_module
from datacron.core.config import Settings, VaultConfig
from datacron.core.durability import RecoveryRequiredError
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationContext, OperationRecord
from datacron.core.paths import PathConfinementError, sidecar_dir, sidecar_index_db
from datacron.core.vault_writer import (
    FilesystemVaultWriter,
    UlidCollisionError,
    VaultLockBusyError,
    WriteConflictError,
    atomic_durable_write,
)

_RECOVERY_TIMESTAMP = "2026-08-10T00:00:00+00:00"


def _pending_record(
    *,
    operation_id: str,
    rel_path: str,
    before: bytes | None,
    after: bytes,
) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        timestamp=_RECOVERY_TIMESTAMP,
        op="patch_section",
        tool="patch_note_section",
        note_id=None,
        rel_path=rel_path,
        before_hash=sha256_bytes(before) if before is not None else None,
        after_hash=sha256_bytes(after),
        actor="recovery-test",
        parameters={},
        history_stored=before is not None,
    )


class _FakeLockHandle:
    """Minimal file-like stub exposing only what ``_lock_file`` touches."""

    def __init__(self, descriptor: int = 0) -> None:
        self._descriptor = descriptor

    def fileno(self) -> int:
        return self._descriptor

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0


def _patch_lock_primitive(monkeypatch: pytest.MonkeyPatch, *, busy: bool) -> None:
    """Replace the platform lock primitive so lock tests run cross-platform.

    ``busy=True`` makes every acquisition attempt report contention (EACCES,
    which both the ``msvcrt`` and ``fcntl`` branches treat as "held"); ``busy``
    ``False`` makes the first attempt succeed immediately.
    """
    busy_error = OSError(errno.EACCES, "resource temporarily unavailable")

    if sys.platform == "win32":

        def fake_locking(descriptor: int, mode: int, nbytes: int) -> None:
            if busy:
                raise busy_error

        monkeypatch.setattr(vars(vault_writer_module)["msvcrt"], "locking", fake_locking)
    else:

        def fake_flock(descriptor: int, operation: int) -> None:
            if busy:
                raise busy_error

        monkeypatch.setattr(vars(vault_writer_module)["fcntl"], "flock", fake_flock)


def _writer(vault: Path) -> FilesystemVaultWriter:
    return FilesystemVaultWriter(vault, Settings(write_paths=[vault]))


def _create_ulid_index(vault: Path, note_id: str | None = None) -> None:
    db_path = sidecar_index_db(vault)
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE ulid_paths (rel_path TEXT PRIMARY KEY, note_id TEXT UNIQUE NOT NULL)"
        )
        if note_id is not None:
            connection.execute(
                "INSERT INTO ulid_paths(rel_path, note_id) VALUES (?, ?)",
                ("existing.md", note_id),
            )


async def _write_note_with_id(
    writer: FilesystemVaultWriter,
    note_id: str,
    *,
    rel_path: str = "new.md",
) -> str:
    return await writer.write_note_atomic(
        rel_path,
        f"---\nid: {note_id}\n---\nnew\n",
        overwrite=False,
        note_id=note_id,
    )


async def test_write_outside_write_paths_is_rejected_without_creating_file(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    writer = _writer(vault)

    with pytest.raises(PathConfinementError):
        await writer.write_note_atomic("../outside.md", "# Outside\n", overwrite=False)

    assert not outside.exists()


async def test_empty_write_paths_reject_all_writes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[]))

    with pytest.raises(PathConfinementError, match="No write paths are configured"):
        await writer.write_note_atomic("note.md", "# Denied\n", overwrite=False)

    assert not (vault / "note.md").exists()


async def test_create_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"old\n")
    writer = _writer(vault)

    with pytest.raises(FileExistsError):
        await writer.write_note_atomic("note.md", "new\n", overwrite=False)

    assert target.read_text(encoding="utf-8") == "old\n"


async def test_overwrite_stores_content_addressed_history_before_replace(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "nested" / "note.md"
    target.parent.mkdir()
    target.write_bytes(b"old\n")
    writer = _writer(vault)

    await writer.write_note_atomic("nested/note.md", "new\n", overwrite=True)

    history = vault / ".datacron" / "history" / sha256_bytes(b"old\n")
    assert history.read_bytes() == b"old\n"
    assert target.read_text(encoding="utf-8") == "new\n"


async def test_atomic_write_leaves_complete_file_and_no_temp_residue(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = _writer(vault)

    await writer.write_note_atomic("note.md", "complete\n", overwrite=False)

    assert (vault / "note.md").read_text(encoding="utf-8") == "complete\n"
    assert list(vault.glob(".note.md.*.tmp")) == []


async def test_write_new_file_inside_write_path_succeeds(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = _writer(vault)

    await writer.write_note_atomic("folder/new.md", "# New\n", overwrite=False)

    assert (vault / "folder" / "new.md").read_text(encoding="utf-8") == "# New\n"


async def test_mutation_cas_uses_exact_disk_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"old\r\n")
    writer = _writer(vault)

    with pytest.raises(WriteConflictError, match="hash mismatch"):
        await writer.mutate_note_atomic(
            "note.md",
            lambda current: f"{current}new",
            expected_hash=sha256_bytes(b"old\n"),
        )

    assert target.read_bytes() == b"old\r\n"
    assert not (vault / ".datacron" / "history").exists()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"one\r\ntwo\r\n", b"one\r\ntwo\r\nthree\r\n"),
        (b"one\ntwo\n", b"one\ntwo\nthree\n"),
        (b"one\r\ntwo\r\nthree\n", b"one\r\ntwo\r\nthree\r\nfour\r\n"),
        (b"one\r\ntwo\nthree\n", b"one\ntwo\nthree\nfour\n"),
    ],
)
async def test_existing_note_emits_one_dominant_eol(
    tmp_path: Path,
    source: bytes,
    expected: bytes,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(source)
    writer = _writer(vault)
    addition = "three\n" if source.count(b"\n") == 2 else "four\n"

    returned_hash = await writer.mutate_note_atomic(
        "note.md",
        lambda current: f"{current}{addition}",
    )

    assert target.read_bytes() == expected
    assert returned_hash == sha256_bytes(expected)


async def test_new_note_uses_configured_crlf_policy(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault]),
        VaultConfig(line_endings="crlf"),
    )

    returned_hash = await writer.write_note_atomic(
        "note.md",
        "one\ntwo\n",
        overwrite=False,
    )

    assert (vault / "note.md").read_bytes() == b"one\r\ntwo\r\n"
    assert returned_hash == sha256_bytes(b"one\r\ntwo\r\n")


async def test_create_rejects_ulid_collision_from_index_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    _create_ulid_index(vault, note_id)
    writer = _writer(vault)

    with pytest.raises(UlidCollisionError, match=note_id):
        await _write_note_with_id(writer, note_id)

    assert not (sidecar_dir(vault) / "ulids.json").exists()
    assert not (vault / "new.md").exists()


async def test_create_rejects_ulid_collision_from_sidecar_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    identities = sidecar_dir(vault) / "ulids.json"
    identities.parent.mkdir(parents=True)
    identities.write_text(json.dumps({"existing.md": note_id}), encoding="utf-8")
    writer = _writer(vault)

    with pytest.raises(UlidCollisionError, match=note_id):
        await _write_note_with_id(writer, note_id)

    assert not sidecar_index_db(vault).exists()
    assert not (vault / "new.md").exists()


async def test_create_does_not_scan_vault_when_index_authority_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    _create_ulid_index(vault)
    writer = _writer(vault)

    def fail_scan(candidate: str) -> bool:
        pytest.fail(f"full vault scan must not run when an authority exists: {candidate}")

    monkeypatch.setattr(writer, "_ulid_exists_in_frontmatter", fail_scan)

    await _write_note_with_id(writer, note_id)

    assert (vault / "new.md").is_file()


async def test_create_rejects_frontmatter_ulid_collision_without_authority(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    (vault / "existing.md").write_bytes(f"---\nid: {note_id}\n---\nold\n".encode())
    writer = _writer(vault)

    with pytest.raises(UlidCollisionError, match=note_id):
        await _write_note_with_id(writer, note_id)

    assert not (vault / "new.md").exists()


async def test_fallback_ulid_scan_skips_non_utf8_markdown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "unreadable.md").write_bytes(b"\xff\xfe\xfa")
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    writer = _writer(vault)

    await _write_note_with_id(writer, note_id)

    assert (vault / "new.md").is_file()
    assert "Skipping unreadable note during fallback ULID scan" in caplog.text
    assert "unreadable.md" in caplog.text


def test_atomic_durable_write_orders_file_replace_and_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_fsync(file_descriptor: int) -> None:
        events.append("file_fsync")
        real_fsync(file_descriptor)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    def tracked_directory_fsync(path: Path) -> bool:
        events.append("directory_fsync")
        assert path == tmp_path
        return True

    monkeypatch.setattr("datacron.core.vault_writer.os.fsync", tracked_fsync)
    monkeypatch.setattr("datacron.core.vault_writer.os.replace", tracked_replace)
    monkeypatch.setattr("datacron.core.durability.flush_directory_entry", tracked_directory_fsync)

    returned_hash = atomic_durable_write(target, b"new")

    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert returned_hash == sha256_bytes(b"new")


def test_lock_file_returns_promptly_when_lock_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_lock_primitive(monkeypatch, busy=False)

    start = time.monotonic()
    vault_writer_module._lock_file(_FakeLockHandle(), "oplog", 5.0)

    # A free lock must be granted on the first attempt, well under the budget.
    assert time.monotonic() - start < 1.0


def test_lock_file_raises_bounded_timeout_when_lock_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_lock_primitive(monkeypatch, busy=True)
    timeout_seconds = 0.2

    start = time.monotonic()
    with pytest.raises(VaultLockBusyError) as excinfo:
        vault_writer_module._lock_file(_FakeLockHandle(), "oplog", timeout_seconds)
    elapsed = time.monotonic() - start

    # Bounded: it waits at least the timeout, then gives up instead of spinning
    # forever (the historical bug). Upper bound stays generous for slow CI.
    assert elapsed >= timeout_seconds
    assert elapsed < timeout_seconds + 5.0
    assert "oplog" in str(excinfo.value)
    assert "busy" in str(excinfo.value)


def test_advisory_lock_raises_when_same_lock_is_already_held(tmp_path: Path) -> None:
    writer = FilesystemVaultWriter(
        tmp_path,
        Settings(write_paths=[tmp_path], vault_lock_timeout_seconds=0.2),
    )

    with (
        writer._advisory_lock("oplog"),
        pytest.raises(VaultLockBusyError),
        writer._advisory_lock("oplog"),
    ):
        pass


@pytest.mark.parametrize(
    ("disk_bytes", "expected_recovered", "expected_records"),
    [
        (b"before\n", 0, 0),
        (b"after\n", 1, 1),
    ],
)
async def test_recovery_preserves_existing_before_and_after_reconciliation(
    tmp_path: Path,
    disk_bytes: bytes,
    expected_recovered: int,
    expected_records: int,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(disk_bytes)
    writer = _writer(vault)
    record = _pending_record(
        operation_id="operation-reconciled",
        rel_path="note.md",
        before=b"before\n",
        after=b"after\n",
    )
    writer._operation_journal.write_pending(record)

    recovered = await writer.recover_operations()

    assert recovered == expected_recovered
    assert len(await writer.list_operations()) == expected_records
    assert writer.recovery_blocked == ()
    assert not writer._operation_journal.pending_path(record.operation_id).exists()
    assert target.read_bytes() == disk_bytes


async def test_irreconcilable_pending_is_stable_quarantined_and_idempotent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"external\n")
    writer = _writer(vault)
    record = _pending_record(
        operation_id="operation-pending-divergent",
        rel_path="note.md",
        before=b"before\n",
        after=b"after\n",
    )
    writer._operation_journal.write_pending(record)
    pending_path = writer._operation_journal.pending_path(record.operation_id)
    pending_before = pending_path.read_bytes()
    operations_path = vault / ".datacron" / "oplog" / "operations.jsonl"

    outcomes = [await writer.recover_operations() for _attempt in range(3)]

    assert outcomes == [0, 0, 0]
    assert len(writer.recovery_blocked) == 1
    blocked = writer.recovery_blocked[0]
    assert blocked.operation_id == record.operation_id
    assert blocked.rel_path == "note.md"
    assert blocked.reason == "pending_disk_hash_mismatch"
    assert blocked.expected_before_hash == record.before_hash
    assert blocked.expected_after_hash == record.after_hash
    assert blocked.disk_hash == sha256_bytes(b"external\n")
    assert pending_path.read_bytes() == pending_before
    assert len(writer._operation_journal.pending_paths()) == 1
    assert not operations_path.exists()
    assert target.read_bytes() == b"external\n"
    assert caplog.text.count("operation recovery blocked") == 3


async def test_committed_divergence_uses_distinct_reason_without_mutation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"external\n")
    writer = _writer(vault)
    record = _pending_record(
        operation_id="operation-committed-divergent",
        rel_path="note.md",
        before=b"before\n",
        after=b"after\n",
    )
    writer._operation_journal.append_record(record)
    writer._operation_journal.write_pending(record)
    pending_path = writer._operation_journal.pending_path(record.operation_id)
    pending_before = pending_path.read_bytes()
    operations_path = vault / ".datacron" / "oplog" / "operations.jsonl"
    operations_before = operations_path.read_bytes()

    recovered = await writer.recover_operations()

    assert recovered == 0
    assert writer.recovery_blocked[0].reason == "committed_disk_hash_mismatch"
    assert pending_path.read_bytes() == pending_before
    assert operations_path.read_bytes() == operations_before
    assert target.read_bytes() == b"external\n"


@pytest.mark.parametrize("mutation", ["write", "mutate", "revert", "purge"])
async def test_every_mutation_fails_closed_after_fresh_recovery_scan(
    tmp_path: Path,
    mutation: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"external\n")
    writer = _writer(vault)
    record = _pending_record(
        operation_id="operation-blocks-mutations",
        rel_path="note.md",
        before=b"before\n",
        after=b"after\n",
    )
    writer._operation_journal.write_pending(record)

    async def run_mutation() -> None:
        if mutation == "write":
            await writer.write_note_atomic("new.md", "new\n", overwrite=False)
        elif mutation == "mutate":
            await writer.mutate_note_atomic("note.md", lambda raw: f"{raw}changed\n")
        elif mutation == "revert":
            await writer.revert_note_atomic(
                "note.md",
                "0" * 64,
                expected_hash=None,
                operation=OperationContext(
                    op="revert",
                    tool="revert_note",
                    actor="recovery-test",
                    parameters={},
                ),
            )
        else:
            await writer.purge_history()

    with pytest.raises(
        RecoveryRequiredError,
        match=r"1 blocked operation.*operation-blocks-mutations",
    ) as error:
        await run_mutation()

    assert error.value.code == "recovery_required"
    assert target.read_bytes() == b"external\n"
    assert not (vault / "new.md").exists()


async def test_blocked_recovery_preserves_expired_history_blob(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(
        vault,
        Settings(
            write_paths=[vault],
            operation_history_purge_min_interval_seconds=0,
        ),
        VaultConfig(history_retention_days=1),
    )
    history_hash = writer._operation_journal.store_history(b"needed-before\n")
    history_path = vault / ".datacron" / "history" / history_hash
    old_time = time.time() - (2 * 86_400)
    os.utime(history_path, (old_time, old_time))
    record = _pending_record(
        operation_id="operation-needs-history",
        rel_path="missing.md",
        before=b"needed-before\n",
        after=b"after\n",
    )
    writer._operation_journal.write_pending(record)

    with pytest.raises(RecoveryRequiredError):
        await writer.purge_history()

    assert history_path.read_bytes() == b"needed-before\n"


async def test_lock_contention_is_not_reported_as_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault], vault_lock_timeout_seconds=0.01),
    )
    _patch_lock_primitive(monkeypatch, busy=True)

    with pytest.raises(VaultLockBusyError):
        await writer.write_note_atomic("note.md", "new\n", overwrite=False)
