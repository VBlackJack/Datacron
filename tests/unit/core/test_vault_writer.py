# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.vault_writer`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from datacron.core.config import Settings, VaultConfig
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import PathConfinementError
from datacron.core.vault_writer import (
    FilesystemVaultWriter,
    UlidCollisionError,
    WriteConflictError,
    atomic_durable_write,
)


def _writer(vault: Path) -> FilesystemVaultWriter:
    return FilesystemVaultWriter(vault, Settings(write_paths=[vault]))


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


async def test_create_rejects_frontmatter_ulid_collision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    (vault / "existing.md").write_bytes(f"---\nid: {note_id}\n---\nold\n".encode())
    writer = _writer(vault)

    with pytest.raises(UlidCollisionError, match=note_id):
        await writer.write_note_atomic(
            "new.md",
            f"---\nid: {note_id}\n---\nnew\n",
            overwrite=False,
            note_id=note_id,
        )

    assert not (vault / "new.md").exists()


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
