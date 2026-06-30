# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.vault_writer`."""

from __future__ import annotations

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.paths import PathConfinementError
from datacron.core.vault_writer import FilesystemVaultWriter


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
    target.write_text("old\n", encoding="utf-8")
    writer = _writer(vault)

    with pytest.raises(FileExistsError):
        await writer.write_note_atomic("note.md", "new\n", overwrite=False)

    assert target.read_text(encoding="utf-8") == "old\n"


async def test_overwrite_snapshots_existing_file_before_replace(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "nested" / "note.md"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    writer = _writer(vault)

    await writer.write_note_atomic("nested/note.md", "new\n", overwrite=True)

    backups = list((vault / ".datacron" / "backups" / "nested" / "note.md").glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old\n"
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
