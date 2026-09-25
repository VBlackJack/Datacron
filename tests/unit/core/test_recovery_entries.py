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
"""Recovery directories meet files Datacron did not write.

An operating system shell, a sync client or an operator can drop a file into
``.datacron/oplog/pending`` or the organization batch directories. One such file
used to abort startup, so the client saw no tool at all, and fail every write with
an opaque internal error. Shell metadata is now ignored; anything else blocks
writes with a typed ``recovery_required`` error that names the entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.durability import RecoveryRequiredError
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.vault_writer import FilesystemVaultWriter

_OPLOG_PENDING = (".datacron", "oplog", "pending")
_BATCH_PENDING = (".datacron", "oplog", "batches", "pending")
_BATCH_STAGE = (".datacron", "oplog", "batches", "stage")


def _writer(tmp_path: Path) -> tuple[Path, FilesystemVaultWriter]:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"# Note\n")
    return vault, FilesystemVaultWriter(vault, Settings(write_paths=[vault]))


def _drop(vault: Path, directory: tuple[str, ...], name: str) -> Path:
    parent = vault.joinpath(*directory)
    parent.mkdir(parents=True, exist_ok=True)
    stray = parent / name
    stray.write_bytes(b"\x00stray\n")
    return stray


@pytest.mark.parametrize(
    ("directory", "name"),
    [
        (_OPLOG_PENDING, "desktop.ini"),
        (_OPLOG_PENDING, "Thumbs.db"),
        (_OPLOG_PENDING, "._0123456789abcdef.json"),
        (_BATCH_PENDING, ".DS_Store"),
        (_BATCH_STAGE, ".DS_Store"),
        (_BATCH_STAGE, "desktop.ini"),
    ],
)
async def test_shell_metadata_in_a_recovery_directory_is_ignored(
    tmp_path: Path,
    directory: tuple[str, ...],
    name: str,
) -> None:
    vault, writer = _writer(tmp_path)
    stray = _drop(vault, directory, name)

    assert await writer.recover_operations() == 0
    await writer.write_note_atomic("note.md", "# Note\n\nwritten\n", overwrite=True)

    assert (vault / "note.md").read_bytes() == b"# Note\n\nwritten\n"
    assert stray.is_file()
    assert writer.recovery_unexpected_entries == ()


@pytest.mark.parametrize(
    ("directory", "name"),
    [
        (_OPLOG_PENDING, "notes.txt"),
        (_BATCH_PENDING, "stray.txt"),
        (_BATCH_STAGE, "stray"),
    ],
)
async def test_an_unexpected_entry_refuses_writes_and_names_itself(
    tmp_path: Path,
    directory: tuple[str, ...],
    name: str,
) -> None:
    vault, writer = _writer(tmp_path)
    stray = _drop(vault, directory, name)
    expected_entry = "/".join((*directory, name))

    with pytest.raises(RecoveryRequiredError, match=name):
        await writer.recover_operations()
    assert writer.recovery_unexpected_entries == (expected_entry,)
    with pytest.raises(RecoveryRequiredError, match=name):
        await writer.write_note_atomic("note.md", "# Changed\n", overwrite=True)

    assert (vault / "note.md").read_bytes() == b"# Note\n"
    assert stray.is_file()

    stray.unlink()
    assert await writer.recover_operations() == 0
    assert writer.recovery_unexpected_entries == ()
    await writer.write_note_atomic("note.md", "# Changed\n", overwrite=True)


async def test_a_sync_conflict_copy_of_a_pending_manifest_is_an_unexpected_entry(
    tmp_path: Path,
) -> None:
    """A copy passes the ``.json`` suffix test and then fails the name binding.

    It must neither be recovered, since it is not the manifest it claims to be,
    nor abort startup: it blocks writes, by name, until an operator looks at it.
    """
    vault, writer = _writer(tmp_path)
    journal = writer._operation_journal
    record = OperationRecord(
        operation_id="0123456789abcdef0123456789abcdef",
        timestamp=datetime(2026, 9, 25, tzinfo=UTC).isoformat(timespec="microseconds"),
        op="append",
        tool="append_journal",
        note_id=None,
        rel_path="note.md",
        before_hash=sha256_bytes(b"# Note\n"),
        after_hash=sha256_bytes(b"# Note\nafter\n"),
        actor="unit-test",
        parameters={},
        history_stored=True,
    )
    journal.write_pending(record)
    original = journal.pending_path(record.operation_id)
    conflict_name = f"{record.operation_id}.sync-conflict-20260925-101010-ABCDEFG.json"
    original.replace(original.with_name(conflict_name))

    with pytest.raises(RecoveryRequiredError, match="sync-conflict"):
        await writer.recover_operations()
    assert writer.recovery_unexpected_entries == ("/".join((*_OPLOG_PENDING, conflict_name)),)
    assert (vault / "note.md").read_bytes() == b"# Note\n"
