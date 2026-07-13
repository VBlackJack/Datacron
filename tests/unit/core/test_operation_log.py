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
"""Tests for durable operation JSONL and history retention policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from datacron.core.config import Settings, VaultConfig
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import (
    HistoryUnavailableError,
    OperationContext,
    OperationJournal,
    OperationLogError,
    OperationRecord,
)
from datacron.core.vault_writer import FilesystemVaultWriter


def _record(
    operation_id: str,
    timestamp: datetime,
    before_hash: str,
    after_hash: str,
) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        timestamp=timestamp.isoformat(timespec="microseconds"),
        op="patch_section",
        tool="patch_note_section",
        note_id="01J00000000000000000000042",
        rel_path="note.md",
        before_hash=before_hash,
        after_hash=after_hash,
        actor="unit-test",
        parameters={"new_content_chars": 3},
        history_stored=True,
    )


def test_retention_purges_only_unreferenced_expired_history(tmp_path: Path) -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    old_bytes = b"expired version"
    recent_bytes = b"recent version"
    old_hash = journal.store_history(old_bytes)
    recent_hash = journal.store_history(recent_bytes)
    journal.append_record(
        _record("old-operation", now - timedelta(days=31), old_hash, sha256_bytes(b"old after"))
    )
    journal.append_record(
        _record(
            "recent-operation",
            now - timedelta(days=1),
            recent_hash,
            sha256_bytes(b"recent after"),
        )
    )

    removed = journal.purge_history(now)

    assert removed == [old_hash]
    assert not (tmp_path / ".datacron" / "history" / old_hash).exists()
    assert (tmp_path / ".datacron" / "history" / recent_hash).read_bytes() == recent_bytes


async def test_redacted_mode_logs_hashes_without_storing_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"before\n")
    writer = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault]),
        VaultConfig(history_mode="redacted"),
    )

    await writer.mutate_note_atomic(
        "note.md",
        lambda _current: "after\n",
        operation=OperationContext(
            op="patch_section",
            tool="patch_note_section",
            actor="unit-test",
            parameters={"new_content_chars": 6},
        ),
    )

    records = await writer.list_operations()
    assert len(records) == 1
    assert records[0].before_hash == sha256_bytes(b"before\n")
    assert records[0].history_stored is False
    assert not (vault / ".datacron" / "history").exists()
    with pytest.raises(HistoryUnavailableError, match="redacted"):
        await writer.revert_note_atomic(
            "note.md",
            sha256_bytes(b"before\n"),
            expected_hash=sha256_bytes(b"after\n"),
            operation=OperationContext(
                op="revert",
                tool="revert_note",
                actor="unit-test",
                parameters={"to_hash": sha256_bytes(b"before\n")},
            ),
        )


async def test_revert_rejects_history_hash_owned_by_another_note(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "one.md").write_bytes(b"one-before\n")
    (vault / "two.md").write_bytes(b"two-before\n")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    context = OperationContext(
        op="patch_section",
        tool="patch_note_section",
        actor="unit-test",
        parameters={"new_content_chars": 10},
    )
    await writer.mutate_note_atomic("one.md", lambda _current: "one-after\n", operation=context)
    await writer.mutate_note_atomic("two.md", lambda _current: "two-after\n", operation=context)

    with pytest.raises(HistoryUnavailableError, match=r"not recorded for two\.md"):
        await writer.revert_note_atomic(
            "two.md",
            sha256_bytes(b"one-before\n"),
            expected_hash=sha256_bytes(b"two-after\n"),
            operation=OperationContext(
                op="revert",
                tool="revert_note",
                actor="unit-test",
                parameters={"to_hash": sha256_bytes(b"one-before\n")},
            ),
        )


def test_monotonic_timestamp_advances_when_wall_clock_moves_back(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    future = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    before_hash = sha256_bytes(b"before")
    after_hash = sha256_bytes(b"after")
    journal.append_record(_record("future-operation", future, before_hash, after_hash))

    timestamp = datetime.fromisoformat(
        journal.next_timestamp(datetime(2026, 7, 10, 11, 0, 0, tzinfo=UTC))
    )

    assert timestamp == future + timedelta(microseconds=1)


def test_full_read_detects_corruption_in_middle_of_hash_chain(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    for index in range(3):
        journal.append_record(
            _record(
                f"operation-{index}",
                now + timedelta(seconds=index),
                sha256_bytes(f"before-{index}".encode()),
                sha256_bytes(f"after-{index}".encode()),
            )
        )
    path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    lines = path.read_text(encoding="ascii").splitlines()
    middle = json.loads(lines[1])
    middle["actor"] = "tampered"
    lines[1] = json.dumps(middle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")

    with pytest.raises(OperationLogError, match="hash chain mismatch"):
        journal.read_records()


def test_legacy_log_is_migrated_once_then_accepts_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    path.parent.mkdir(parents=True)
    legacy_payloads: list[dict[str, object]] = []
    for index in range(2):
        payload = _record(
            f"legacy-{index}",
            now + timedelta(seconds=index),
            sha256_bytes(f"before-{index}".encode()),
            sha256_bytes(f"after-{index}".encode()),
        ).to_dict()
        del payload["format_version"]
        del payload["prev_hash"]
        legacy_payloads.append(payload)
    path.write_text(
        "".join(
            f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n"
            for payload in legacy_payloads
        ),
        encoding="ascii",
    )
    migration_warning = Mock()
    monkeypatch.setattr("datacron.core.operation_log._LOGGER.warning", migration_warning)
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")

    journal.append_record(
        _record(
            "new-operation",
            now + timedelta(seconds=2),
            sha256_bytes(b"before-new"),
            sha256_bytes(b"after-new"),
        )
    )

    records = journal.read_records()
    assert [record.operation_id for record in records] == [
        "legacy-0",
        "legacy-1",
        "new-operation",
    ]
    assert all(record.format_version == 2 for record in records)
    assert records[0].prev_hash is None
    assert all(record.prev_hash is not None for record in records[1:])
    migration_warning.assert_called_once_with(
        "Migrated %d legacy operation records to chained format version %d",
        2,
        2,
    )
