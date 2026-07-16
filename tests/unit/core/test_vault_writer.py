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
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import PathConfinementError, sidecar_dir, sidecar_index_db
from datacron.core.vault_writer import (
    FilesystemVaultWriter,
    UlidCollisionError,
    VaultLockBusyError,
    WriteConflictError,
    atomic_durable_write,
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
