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
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import accumulate
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from datacron.core import operation_log
from datacron.core.config import DEFAULT_HISTORY_RETENTION_DAYS, Settings, VaultConfig
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import (
    _REVERSE_READ_CHUNK_BYTES,
    HistoryUnavailableError,
    OperationContext,
    OperationJournal,
    OperationLogError,
    OperationRecord,
    _record_line,
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


def test_switching_to_redacted_keeps_the_versions_full_mode_stored(tmp_path: Path) -> None:
    """Turning history off must not destroy the history already on disk.

    ``redacted`` says this vault stores no new prior bytes. It never said the
    ones an earlier ``full`` period stored should be destroyed, yet the sweep
    did exactly that: with history disabled the retention scan was skipped, so
    the retained set was empty and every blob counted as unreferenced. Editing
    one key in VAULT.yaml and making one unrelated write deleted every earlier
    version of every note, silently and with no way back.
    """
    now = datetime(2026, 7, 10, tzinfo=UTC)
    full = OperationJournal(tmp_path, retention_days=1278, history_mode="full")
    stored = []
    for index in range(3, 0, -1):
        content_hash = full.store_history(f"version {index}".encode())
        stored.append(content_hash)
        full.append_record(
            _record(
                f"operation-{index}",
                now - timedelta(days=index),
                content_hash,
                sha256_bytes(f"after {index}".encode()),
            )
        )

    redacted = OperationJournal(tmp_path, retention_days=1278, history_mode="redacted")

    assert redacted.purge_history(now) == []
    history_dir = tmp_path / ".datacron" / "history"
    assert sorted(path.name for path in history_dir.iterdir()) == sorted(stored)


def test_purge_history_skips_scan_until_interval_elapses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    journal = OperationJournal(
        tmp_path,
        retention_days=30,
        history_mode="full",
        purge_min_interval_seconds=30,
    )
    content_hash = journal.store_history(b"recent version")
    journal.append_record(
        _record("recent-operation", now, content_hash, sha256_bytes(b"recent after"))
    )
    scan = Mock(wraps=journal._hashes_within_retention)
    monkeypatch.setattr(journal, "_hashes_within_retention", scan)

    assert journal.purge_history(now) == []
    assert journal.purge_history(now + timedelta(seconds=29)) == []
    assert scan.call_count == 1

    assert journal.purge_history(now + timedelta(seconds=30)) == []
    assert scan.call_count == 2


async def test_close_writes_only_scan_history_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"before\n")
    original_scan = OperationJournal._hashes_within_retention
    scan_count = 0

    def counted_scan(journal: OperationJournal, purge_at: datetime) -> set[str]:
        nonlocal scan_count
        scan_count += 1
        return original_scan(journal, purge_at)

    monkeypatch.setattr(OperationJournal, "_hashes_within_retention", counted_scan)
    writer = FilesystemVaultWriter(
        vault,
        Settings(
            write_paths=[vault],
            operation_history_purge_min_interval_seconds=3600,
        ),
    )
    operation = OperationContext(
        op="patch_section",
        tool="patch_note_section",
        actor="unit-test",
        parameters={"new_content_chars": 6},
    )

    await writer.mutate_note_atomic("note.md", lambda _current: "first\n", operation=operation)
    await writer.mutate_note_atomic("note.md", lambda _current: "second\n", operation=operation)

    assert scan_count == 1


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


def test_latest_record_for_path_uses_exact_path_and_latest_commit(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    first = _record(
        "first-note-operation",
        now,
        sha256_bytes(b"before-first"),
        sha256_bytes(b"after-first"),
    )
    other = replace(
        _record(
            "other-note-operation",
            now + timedelta(seconds=1),
            sha256_bytes(b"before-other"),
            sha256_bytes(b"after-other"),
        ),
        rel_path="nested/note.md",
    )
    latest = _record(
        "latest-note-operation",
        now + timedelta(seconds=2),
        sha256_bytes(b"before-latest"),
        sha256_bytes(b"after-latest"),
    )
    for record in (first, other, latest):
        journal.append_record(record)

    records = journal.read_records()
    assert journal.latest_record_for_path("note.md") == records[-1]
    nested_record = journal.latest_record_for_path("nested/note.md")
    assert nested_record is not None
    assert nested_record.operation_id == other.operation_id
    assert journal.latest_record_for_path("missing.md") is None


def test_latest_record_for_path_uses_platform_path_identity(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    record = replace(
        _record(
            "mixed-case-operation",
            datetime(2026, 7, 10, tzinfo=UTC),
            sha256_bytes(b"before"),
            sha256_bytes(b"after"),
        ),
        rel_path="Folder/Note.md",
    )
    journal.append_record(record)

    separator_variant = journal.latest_record_for_path(r"Folder\Note.md")
    case_variant = journal.latest_record_for_path("folder/note.md")
    if os.name == "nt":
        assert separator_variant is not None
        assert separator_variant.operation_id == record.operation_id
        assert case_variant is not None
        assert case_variant.operation_id == record.operation_id
    else:
        assert separator_variant is None
        assert case_variant is None


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


def test_append_reads_only_the_journal_tail(tmp_path: Path) -> None:
    """An append must not reread the whole journal; its cost must not grow with history.

    Full chain verification belongs to the readers (``read_records`` and the recovery
    path), not to the append hot path of every mutating tool.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    history_length = 50
    for index in range(history_length):
        assert journal.append_record(
            _record(
                f"operation-{index}",
                now + timedelta(seconds=index),
                sha256_bytes(f"before-{index}".encode()),
                sha256_bytes(f"after-{index}".encode()),
            )
        )
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    original_read_bytes = Path.read_bytes

    def _guarded_read_bytes(path: Path) -> bytes:
        if path.name == operations_path.name:
            raise AssertionError(f"full journal read on the append path: {path}")
        return original_read_bytes(path)

    tail = _record(
        "operation-tail",
        now + timedelta(seconds=history_length),
        sha256_bytes(b"before-tail"),
        sha256_bytes(b"after-tail"),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_bytes", _guarded_read_bytes)
        appended = journal.append_record(tail)
        # Replaying the tail operation is idempotent and stays on the tail-only path too.
        replayed = journal.append_record(tail)

    assert appended is True
    assert replayed is False
    records = journal.read_records()
    assert len(records) == history_length + 1
    assert records[-1].operation_id == "operation-tail"
    assert records[-1].prev_hash == sha256_bytes(_record_line(records[-2]))


def test_retention_sweep_cost_does_not_grow_with_the_blob_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs on every committed write, so it must not stat per blob.

    Each entry used to be validated from the vault root down, twice for a blob
    it deleted: about nineteen lstat calls apiece. A vault holding a few
    thousand blobs inside the retention window added tens of thousands of
    syscalls to a committed write, every thirty seconds of sustained writing,
    whether or not the sweep deleted anything.
    """
    history = tmp_path / ".datacron" / "history"
    history.mkdir(parents=True)
    for index in range(200):
        (history / f"{index:064x}").write_bytes(b"x")
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    calls = 0
    real_lstat = os.lstat

    def counting_lstat(*args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        calls += 1
        return real_lstat(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", counting_lstat)

    removed = journal.purge_history(datetime(2026, 7, 10, tzinfo=UTC))

    assert len(removed) == 200
    # A handful for the guarded directory roots, and nothing per blob.
    assert calls < 50, f"the sweep made {calls} lstat calls for 200 blobs"


def test_retention_sweep_refuses_a_linked_history_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link inside the journal's own directory is tampering, not a blob to skip.

    The per-entry guard used to raise on one. This covers the same refusal
    without needing the symlink privilege, which most Windows hosts withhold.
    """
    history = tmp_path / ".datacron" / "history"
    history.mkdir(parents=True)
    blob = history / ("a" * 64)
    blob.write_bytes(b"x")
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    real_scandir = os.scandir

    class _LinkedEntry:
        name = "a" * 64
        path = str(blob)

        def is_symlink(self) -> bool:
            return True

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return True

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            return os.stat(blob)

    class _Scan:
        def __enter__(self) -> list[_LinkedEntry]:
            return [_LinkedEntry()]

        def __exit__(self, *_exc: object) -> None:
            return None

    def fake_scandir(path: str | os.PathLike[str]) -> object:
        if Path(os.fspath(path)) == history:
            return _Scan()
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    with pytest.raises(OperationLogError, match="linked or reparse"):
        journal.purge_history(datetime(2026, 7, 10, tzinfo=UTC))

    assert blob.is_file()


def _fill_journal(journal: OperationJournal, count: int, *, rel_path: str) -> None:
    """Append ``count`` filler records, then one naming ``rel_path``."""
    now = datetime(2026, 7, 10, tzinfo=UTC)
    for index in range(count):
        journal.append_record(
            replace(
                _record(
                    f"filler-{index}",
                    now + timedelta(seconds=index),
                    sha256_bytes(f"before-{index}".encode()),
                    sha256_bytes(f"after-{index}".encode()),
                ),
                rel_path=f"filler/note-{index}.md",
            )
        )
    journal.append_record(
        replace(
            _record(
                "target-operation",
                now + timedelta(seconds=count),
                sha256_bytes(b"before-target"),
                sha256_bytes(b"after-target"),
            ),
            rel_path=rel_path,
        )
    )


def _rewrite_line(operations_path: Path, index: int, **fields: str) -> None:
    """Replace fields of one journal line in place, leaving its chain link stale."""
    lines = operations_path.read_bytes().splitlines(keepends=True)
    payload = json.loads(lines[index])
    payload.update(fields)
    rendered = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    lines[index] = f"{rendered}\n".encode("ascii")
    operations_path.write_bytes(b"".join(lines))


def _count_baseline_parses(journal: OperationJournal, rel_path: str) -> int:
    """Return how many journal lines one baseline lookup turns into records."""
    parsed = 0
    original_from_dict = OperationRecord.from_dict

    def _counting_from_dict(payload: object) -> OperationRecord:
        nonlocal parsed
        parsed += 1
        return original_from_dict(payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(OperationRecord, "from_dict", _counting_from_dict)
        found = journal.latest_record_for_path(rel_path)
    assert found is not None
    assert found.operation_id == "target-operation"
    return parsed


def test_baseline_lookup_cost_does_not_grow_with_the_journal(tmp_path: Path) -> None:
    """The per-write baseline lookup must read the journal tail, not the whole file.

    ``_check_committed_baseline`` runs on every logged write. A full verified parse
    made each write cost the entire history, so a vault kept for years slowed every
    write down forever. The assertion is that two journals of very different lengths
    cost the same, which a full parse cannot satisfy at any threshold.
    """
    short_journal = OperationJournal(tmp_path / "short", retention_days=30, history_mode="full")
    long_journal = OperationJournal(tmp_path / "long", retention_days=30, history_mode="full")
    _fill_journal(short_journal, 20, rel_path="target.md")
    _fill_journal(long_journal, 400, rel_path="target.md")

    short_parses = _count_baseline_parses(short_journal, "target.md")
    long_parses = _count_baseline_parses(long_journal, "target.md")

    assert short_parses == long_parses
    # The match, the line it chains to, and the tail state loaded once beforehand.
    assert long_parses <= 4


def test_baseline_lookup_never_reads_the_whole_journal(tmp_path: Path) -> None:
    """A full read of operations.jsonl on the write path is the regression itself."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 30, rel_path="target.md")
    original_read_bytes = Path.read_bytes

    def _guarded_read_bytes(path: Path) -> bytes:
        if path.name == "operations.jsonl":
            raise AssertionError(f"full journal read on the write path: {path}")
        return original_read_bytes(path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_bytes", _guarded_read_bytes)
        baseline = journal.latest_record_for_path("target.md")

    assert baseline is not None
    assert baseline.operation_id == "target-operation"


def test_baseline_lookup_reads_records_larger_than_one_chunk(tmp_path: Path) -> None:
    """A record wider than the reverse-read chunk must still be reassembled exactly."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    wide_parameter = "w" * (_REVERSE_READ_CHUNK_BYTES * 2 + 17)
    for index, rel_path in enumerate(("other.md", "target.md", "later.md")):
        journal.append_record(
            replace(
                _record(
                    f"wide-{index}",
                    now + timedelta(seconds=index),
                    sha256_bytes(f"before-{index}".encode()),
                    sha256_bytes(f"after-{index}".encode()),
                ),
                rel_path=rel_path,
                parameters={"note": wide_parameter},
            )
        )

    baseline = journal.latest_record_for_path("target.md")

    assert baseline is not None
    assert baseline.operation_id == "wide-1"
    assert baseline.parameters == {"note": wide_parameter}


def test_baseline_lookup_rejects_corruption_between_the_match_and_the_tail(
    tmp_path: Path,
) -> None:
    """A rewritten record at or after the match breaks the chain the scan verifies."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 6, rel_path="target.md")
    journal.append_record(
        replace(
            _record(
                "after-target",
                datetime(2026, 7, 10, tzinfo=UTC) + timedelta(seconds=100),
                sha256_bytes(b"before-after"),
                sha256_bytes(b"after-after"),
            ),
            rel_path="unrelated.md",
        )
    )
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    _rewrite_line(operations_path, -2, actor="tampered")

    with pytest.raises(OperationLogError, match="hash chain mismatch"):
        journal.latest_record_for_path("target.md")


def test_baseline_lookup_rejects_a_first_line_match_that_is_not_the_chain_root(
    tmp_path: Path,
) -> None:
    """A journal whose head was cut away chains to a line that is no longer there.

    Every surviving link still verifies, so only the root check catches it: the
    oldest line the scan reaches must declare a null ``prev_hash``. Cutting the
    head is how a scan that stops at its match would otherwise be blinded, since
    the damage sits exactly where it no longer reads.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    for index, rel_path in enumerate(("cut.md", "cut.md", "target.md", "later.md")):
        journal.append_record(
            replace(
                _record(
                    f"root-{index}",
                    now + timedelta(seconds=index),
                    sha256_bytes(f"before-{index}".encode()),
                    sha256_bytes(f"after-{index}".encode()),
                ),
                rel_path=rel_path,
            )
        )
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    surviving = operations_path.read_bytes().splitlines(keepends=True)[2:]
    operations_path.write_bytes(b"".join(surviving))

    with pytest.raises(OperationLogError, match="chain root"):
        journal.latest_record_for_path("target.md")


def test_baseline_lookup_rejects_a_journal_that_does_not_end_at_a_line(tmp_path: Path) -> None:
    """A torn tail must fail closed on the write path, not be scanned past."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 3, rel_path="target.md")
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    operations_path.write_bytes(operations_path.read_bytes()[:-1])

    with pytest.raises(OperationLogError):
        journal.latest_record_for_path("target.md")


def test_baseline_lookup_ignores_corruption_older_than_the_match(tmp_path: Path) -> None:
    """Deliberate: a write is not blocked by damage predating the last write to its path.

    The scan verifies the chain from the tail down to the line the match chains to,
    and reads nothing older. Corruption before that point is still reported by
    ``read_records``, which is what ``audit_query`` and recovery use, so it stays
    visible; it simply no longer makes every unrelated write fail. This test records
    the trade so a later reader does not mistake it for an oversight.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 8, rel_path="target.md")
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    _rewrite_line(operations_path, 0, actor="damaged")

    baseline = journal.latest_record_for_path("target.md")

    assert baseline is not None
    assert baseline.operation_id == "target-operation"
    with pytest.raises(OperationLogError, match="hash chain mismatch"):
        journal.read_records()


@pytest.mark.parametrize(
    "shape",
    [
        b"",
        b"one\n",
        b"\n",
        b"one\ntwo\n",
        b"\n\n\n",
        b"one\n\ntwo\n",
        b"one\ntwo\nthree\n",
        b"x" * 40 + b"\n",
    ],
    ids=[
        "empty",
        "single-line",
        "single-blank",
        "two-lines",
        "consecutive-blanks",
        "blank-in-the-middle",
        "three-lines",
        "line-longer-than-the-chunk",
    ],
)
@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 4, 8, 1024])
def test_reverse_line_reader_matches_a_forward_split(
    tmp_path: Path, shape: bytes, chunk_bytes: int
) -> None:
    """The backwards reader must reproduce ``splitlines`` exactly, at any chunk size.

    Chunk sizes of one to eight bytes force a boundary inside a line, on a line
    boundary, and at an exact multiple of the file size, which is where hand-written
    buffer arithmetic fails. The production chunk is 64 KiB, so no realistic journal
    would exercise those boundaries before a user did.
    """
    path = tmp_path / "operations.jsonl"
    path.write_bytes(shape)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(operation_log, "_REVERSE_READ_CHUNK_BYTES", chunk_bytes)
        with path.open("rb") as stream:
            end = stream.seek(0, os.SEEK_END)
            read = list(operation_log._iter_lines_reverse(stream, end))

    expected_lines = shape.splitlines(keepends=True)
    expected_offsets = list(accumulate((len(line) for line in expected_lines), initial=0))
    assert [line for _offset, line in reversed(read)] == expected_lines
    assert [offset for offset, _line in reversed(read)] == expected_offsets[: len(expected_lines)]


def test_baseline_lookup_on_an_empty_journal_file(tmp_path: Path) -> None:
    """A journal file that exists but holds nothing has no baseline for any path."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    operations_path.parent.mkdir(parents=True, exist_ok=True)
    operations_path.write_bytes(b"")

    assert journal.latest_record_for_path("target.md") is None


def test_baseline_lookup_on_a_single_record_journal(tmp_path: Path) -> None:
    """The match is the only line, so it is both the tail and the chain root."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    only = replace(
        _record(
            "only-operation",
            datetime(2026, 7, 10, tzinfo=UTC),
            sha256_bytes(b"before-only"),
            sha256_bytes(b"after-only"),
        ),
        rel_path="target.md",
    )
    journal.append_record(only)

    baseline = journal.latest_record_for_path("target.md")

    assert baseline is not None
    assert baseline.operation_id == "only-operation"
    assert baseline.prev_hash is None


def test_baseline_lookup_for_a_path_with_no_record_reads_back_to_the_root(
    tmp_path: Path,
) -> None:
    """Proving a path was never written costs the whole journal, and must stay correct.

    This is the shape of a first write to a new note, where ``expected_hash`` is
    absent and no record names the path. The scan cannot stop early because nothing
    short of the first line proves the absence, so the answer is ``None`` only after
    the chain has been verified all the way to the root. The bounded case is the
    repeat write, not this one.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 12, rel_path="target.md")

    assert journal.latest_record_for_path("never-written.md") is None

    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    _rewrite_line(operations_path, 0, actor="damaged")

    with pytest.raises(OperationLogError, match="hash chain mismatch"):
        journal.latest_record_for_path("never-written.md")


def test_baseline_lookup_rejects_a_duplicate_operation_id_in_the_scanned_region(
    tmp_path: Path,
) -> None:
    """A repeated operation id inside the scanned suffix is caught as a full parse does."""
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 4, rel_path="target.md")
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    lines = operations_path.read_bytes().splitlines(keepends=True)
    operations_path.write_bytes(b"".join([*lines, lines[-1]]))

    with pytest.raises(OperationLogError, match="duplicate operation_id at byte offset"):
        journal.latest_record_for_path("target.md")


def test_baseline_lookup_chains_over_the_canonical_rendering(tmp_path: Path) -> None:
    """The scan hashes the record, not the bytes, which is what the specification says.

    A line re-spaced by hand keeps its meaning and its canonical hash, so the record
    that chains to it still verifies. Hashing the raw bytes instead would make the
    write path and ``audit_query`` disagree about the very same file.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    _fill_journal(journal, 2, rel_path="target.md")
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    lines = operations_path.read_bytes().splitlines(keepends=True)
    respaced = json.dumps(json.loads(lines[-2]), separators=(", ", ": "), sort_keys=True)
    lines[-2] = f"{respaced}\n".encode("ascii")
    operations_path.write_bytes(b"".join(lines))

    baseline = journal.latest_record_for_path("target.md")

    assert baseline is not None
    assert baseline.operation_id == "target-operation"
    assert journal.read_records()[-2].operation_id == "filler-1"


def _seed_retention_journal(
    vault: Path, *, count: int, span_days: int, now: datetime
) -> OperationJournal:
    """Append ``count`` records evenly spread over ``span_days`` ending at ``now``.

    Each record stores its own history blob, so the sweep has something to keep or
    delete for every record rather than a set of hashes naming nothing.
    """
    journal = OperationJournal(vault, retention_days=30, history_mode="full")
    start = now - timedelta(days=span_days)
    step = timedelta(days=span_days) / count
    for index in range(count):
        before_hash = journal.store_history(f"before-{index}".encode())
        after_hash = journal.store_history(f"after-{index}".encode())
        journal.append_record(
            replace(
                _record(f"retention-{index}", start + step * index, before_hash, after_hash),
                rel_path=f"notes/note-{index}.md",
            )
        )
    return journal


def test_retention_sweep_keeps_exactly_what_a_full_scan_keeps(tmp_path: Path) -> None:
    """The bounded sweep and the full forward pass must delete the same blobs.

    Stopping early is only ever safe if it reaches the same answer, so this pins the
    two against each other on a journal that straddles the cutoff in both directions.
    """
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    bounded_vault = tmp_path / "bounded"
    full_vault = tmp_path / "full"
    bounded = _seed_retention_journal(bounded_vault, count=40, span_days=90, now=now)
    full = _seed_retention_journal(full_vault, count=40, span_days=90, now=now)

    bounded_removed = bounded.purge_history(now)
    cutoff = now - timedelta(days=30)
    full_removed = full._purge_unretained_blobs(
        full_vault / ".datacron" / "history",
        full._hashes_within_retention_by_full_scan(cutoff),
    )

    assert bounded_removed
    assert bounded_removed == full_removed
    kept = sorted(path.name for path in (bounded_vault / ".datacron" / "history").iterdir())
    assert kept == sorted(path.name for path in (full_vault / ".datacron" / "history").iterdir())


def test_retention_sweep_cost_follows_the_window_not_the_history(tmp_path: Path) -> None:
    """A vault kept for years must not make its retention sweep slower every year.

    Both journals hold the same number of records inside the thirty-day window and
    differ only in how much history sits behind it. Reading the whole journal makes
    the longer one cost twice as much; reading back to the cutoff makes them equal.
    That equality is the property, so there is no threshold here to drift.
    """
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    short = _seed_retention_journal(tmp_path / "short", count=30, span_days=90, now=now)
    long = _seed_retention_journal(tmp_path / "long", count=60, span_days=180, now=now)

    short_parses = _count_purge_parses(short, now)
    long_parses = _count_purge_parses(long, now)

    assert short_parses == long_parses
    assert long_parses < 30


def _count_purge_parses(journal: OperationJournal, now: datetime) -> int:
    """Return how many journal lines one retention sweep turns into records."""
    parsed = 0
    original_from_dict = OperationRecord.from_dict

    def _counting_from_dict(payload: object) -> OperationRecord:
        nonlocal parsed
        parsed += 1
        return original_from_dict(payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(OperationRecord, "from_dict", _counting_from_dict)
        journal.purge_history(now)
    return parsed


def test_append_refuses_a_record_that_does_not_advance_the_timestamp(tmp_path: Path) -> None:
    """Position and time must agree in the journal, because the sweep trusts that.

    The retention sweep stops at the first record older than its cutoff. A record
    appended out of order would sit behind that stopping point while still being
    inside the window, and its history blob, the only stored copy of that version of
    the note, would be deleted. The order is therefore refused at the append rather
    than hoped for at the read.
    """
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    journal.append_record(
        _record("first-operation", now, sha256_bytes(b"before"), sha256_bytes(b"after"))
    )

    with pytest.raises(OperationLogError, match="precedes the journal tail"):
        journal.append_record(
            _record(
                "backdated-operation",
                now - timedelta(days=1),
                sha256_bytes(b"before-backdated"),
                sha256_bytes(b"after-backdated"),
            )
        )

    # Sharing an instant with the tail is allowed: two records at the same time are
    # either both inside the retention window or both outside it, so neither can
    # hide behind the other when the sweep stops at the cutoff.
    assert journal.append_record(
        _record(
            "same-instant-operation",
            now,
            sha256_bytes(b"before-same"),
            sha256_bytes(b"after-same"),
        )
    )
    assert [record.operation_id for record in journal.read_records()] == [
        "first-operation",
        "same-instant-operation",
    ]


def test_retention_sweep_keeps_everything_when_the_journal_is_damaged(tmp_path: Path) -> None:
    """A sweep that cannot read the journal must delete nothing, and must say so.

    Deleting on a partial or unverifiable reading is the one outcome that loses a
    note version for good, so the failure direction is to keep the blobs.
    """
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    journal = _seed_retention_journal(tmp_path, count=6, span_days=90, now=now)
    history_dir = tmp_path / ".datacron" / "history"
    before = sorted(path.name for path in history_dir.iterdir())
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    _rewrite_line(operations_path, -2, actor="tampered")

    with pytest.raises(OperationLogError, match="hash chain mismatch"):
        journal.purge_history(now)

    assert sorted(path.name for path in history_dir.iterdir()) == before


async def test_a_failing_retention_sweep_does_not_fail_a_committed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs after the note and the journal are durable, so it cannot veto.

    Reporting a committed write as failed also left the sweep marked as never done,
    so the next write repeated it and failed again, with no way out but repair.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"before\n")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    operation = OperationContext(
        op="patch_section",
        tool="patch_note_section",
        actor="unit-test",
        parameters={"new_content_chars": 6},
    )

    def _failing_purge(*_args: object, **_kwargs: object) -> list[str]:
        raise OperationLogError("operation hash chain mismatch at byte offset 0")

    monkeypatch.setattr(OperationJournal, "purge_history", _failing_purge)

    first = await writer.mutate_note_atomic(
        "note.md", lambda _current: "first\n", operation=operation
    )
    second = await writer.mutate_note_atomic(
        "note.md", lambda _current: "second\n", operation=operation
    )

    assert first == sha256_bytes(b"first\n")
    assert second == sha256_bytes(b"second\n")
    assert (vault / "note.md").read_bytes() == b"second\n"
    assert len(await writer.list_operations()) == 2


def test_the_default_retention_keeps_a_version_from_a_project_paused_for_a_year(
    tmp_path: Path,
) -> None:
    """A project can pause for months, and its history must still be there on return.

    Retention decides when the only stored copy of a previous version of a note is
    deleted. The default was thirty days, which silently discarded the history of
    every subject not touched that month. This pins the default against the case it
    exists for rather than against its number.
    """
    now = datetime(2026, 9, 19, tzinfo=UTC)
    journal = OperationJournal(
        tmp_path,
        retention_days=DEFAULT_HISTORY_RETENTION_DAYS,
        history_mode="full",
    )
    paused_hash = journal.store_history(b"the version from before the pause")
    journal.append_record(
        _record("paused-project", now - timedelta(days=365), paused_hash, sha256_bytes(b"after"))
    )
    journal.append_record(
        _record("today", now, sha256_bytes(b"before-today"), sha256_bytes(b"after-today"))
    )

    assert journal.purge_history(now) == []
    assert (tmp_path / ".datacron" / "history" / paused_hash).is_file()


def test_a_sweep_still_expires_what_falls_outside_a_long_window(tmp_path: Path) -> None:
    """The cheap answer must not become a wrong answer: old blobs still go."""
    now = datetime(2026, 9, 19, tzinfo=UTC)
    journal = OperationJournal(tmp_path, retention_days=1278, history_mode="full")
    expired_hash = journal.store_history(b"older than forty-two months")
    kept_hash = journal.store_history(b"inside the window")
    journal.append_record(
        _record("ancient", now - timedelta(days=1400), expired_hash, sha256_bytes(b"after-old"))
    )
    journal.append_record(
        _record("recent", now - timedelta(days=10), kept_hash, sha256_bytes(b"after-recent"))
    )

    removed = journal.purge_history(now)

    assert removed == [expired_hash]
    assert (tmp_path / ".datacron" / "history" / kept_hash).is_file()


def _keyed_record(
    operation_id: str,
    timestamp: datetime,
    key_hash: str,
) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        timestamp=timestamp.isoformat(timespec="microseconds"),
        op="append_journal",
        tool="append_journal",
        note_id="01J00000000000000000000042",
        rel_path="note.md",
        before_hash=sha256_bytes(f"before-{operation_id}".encode()),
        after_hash=sha256_bytes(f"after-{operation_id}".encode()),
        actor="unit-test",
        parameters={"request_key_hash": key_hash, "request_fingerprint": "f" * 64},
        history_stored=True,
    )


class TestRequestKeyLookup:
    """Finding a write request's receipt must not cost the whole journal."""

    @staticmethod
    def _key(name: str) -> str:
        return sha256_bytes(name.encode())

    def test_the_latest_record_answers_a_reused_key(self, tmp_path: Path) -> None:
        """A key reused after a revert has more than one record, and only the last counts.

        The earlier record describes bytes the note no longer holds, so answering
        with it is what made a reverted write look like a completed one.
        """
        now = datetime(2026, 7, 10, tzinfo=UTC)
        journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        key = self._key("reused")
        first = _keyed_record("first", now, key)
        journal.append_record(first)
        journal.append_record(_keyed_record("between", now + timedelta(minutes=1), self._key("x")))
        second = _keyed_record("second", now + timedelta(minutes=2), key)
        journal.append_record(second)

        found = journal.latest_record_for_request_key(key)

        assert found is not None
        assert found.operation_id == "second"

    def test_an_unused_key_is_answered_without_reading_the_journal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Almost every keyed write is a first use, and that is the case that must be cheap.

        Proving absence by reading back to the first line made every ordinary keyed
        write cost the whole journal, which is a cost that only grows.
        """
        now = datetime(2026, 7, 10, tzinfo=UTC)
        journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        for index in range(20):
            journal.append_record(
                _keyed_record(f"op-{index}", now + timedelta(minutes=index), self._key(str(index)))
            )
        assert journal.latest_record_for_request_key(self._key("absent")) is None

        monkeypatch.setattr(
            operation_log,
            "_iter_verified_records_reverse",
            Mock(side_effect=AssertionError("scanned the journal for an unused key")),
        )

        assert journal.latest_record_for_request_key(self._key("also-absent")) is None

    def test_a_record_another_process_appended_is_still_found(self, tmp_path: Path) -> None:
        """The prefilter is only ever allowed to say no, so it must never be stale.

        Two processes share one journal under the cross-process mutation lock. If the
        set of known keys stopped at what this process had appended, the other's
        records would read as first uses and a retry would write a second time.
        """
        now = datetime(2026, 7, 10, tzinfo=UTC)
        here = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        elsewhere = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        here.append_record(_keyed_record("mine", now, self._key("mine")))
        assert here.latest_record_for_request_key(self._key("theirs")) is None

        elsewhere.append_record(
            _keyed_record("theirs", now + timedelta(minutes=1), self._key("theirs"))
        )

        found = here.latest_record_for_request_key(self._key("theirs"))
        assert found is not None
        assert found.operation_id == "theirs"

    def test_a_shorter_journal_rebuilds_the_index(self, tmp_path: Path) -> None:
        """A journal replaced by a shorter one is a different journal.

        Advancing from the old byte count would skip everything the new file holds
        below it, and every key in there would read as unused.
        """
        now = datetime(2026, 7, 10, tzinfo=UTC)
        journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        for index in range(6):
            journal.append_record(
                _keyed_record(f"op-{index}", now + timedelta(minutes=index), self._key(str(index)))
            )
        assert journal.latest_record_for_request_key(self._key("0")) is not None

        operations = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
        lines = operations.read_bytes().splitlines(keepends=True)
        operations.write_bytes(b"".join(lines[:2]))

        assert journal.latest_record_for_request_key(self._key("1")) is not None
        assert journal.latest_record_for_request_key(self._key("5")) is None


class _PartialThenFailingStream:
    """A raw stream that accepts some bytes and then refuses the rest, like ENOSPC."""

    def __init__(self, inner: Any, budget: int) -> None:
        self._inner = inner
        self._budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _PartialThenFailingStream:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._inner.__exit__(*exc_info)

    def write(self, payload: Any) -> int:
        if self._budget <= 0:
            raise OSError(28, "No space left on device")
        written = int(self._inner.write(bytes(payload)[: self._budget]))
        self._budget -= written
        return written


def test_a_partly_written_record_is_rolled_back_not_left_torn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed append must not leave the journal ending mid-line.

    A tail that does not end at a JSONL boundary is refused by every append,
    every recovery scan and every read, and no command repairs one, so the vault
    becomes unwritable for good. The rollback that exists for this could not run:
    through a buffered writer the first real write happened inside ``flush()``,
    and ``truncate()`` flushes before truncating, so it re-attempted the write
    that had just failed and was swallowed. Measured on the previous code: 936
    committed bytes became 976 with no trailing newline, and the next read raised
    ``operation log does not end at a JSONL boundary``.
    """
    now = datetime(2026, 9, 20, tzinfo=UTC)
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    journal.append_record(_record("first", now, sha256_bytes(b"a"), sha256_bytes(b"b")))
    operations = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    committed = operations.read_bytes()

    real_open = Path.open

    def failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        stream = real_open(self, *args, **kwargs)
        mode = args[0] if args else str(kwargs.get("mode", ""))
        if self.name == "operations.jsonl" and "a" in mode:
            return _PartialThenFailingStream(stream, budget=40)
        return stream

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OperationLogError):
        journal.append_record(
            _record("second", now + timedelta(minutes=1), sha256_bytes(b"c"), sha256_bytes(b"d"))
        )
    monkeypatch.undo()

    assert operations.read_bytes() == committed
    reopened = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    assert [item.operation_id for item in reopened.read_records()] == ["first"]
    reopened.append_record(
        _record("third", now + timedelta(minutes=2), sha256_bytes(b"e"), sha256_bytes(b"f"))
    )
    assert [item.operation_id for item in reopened.read_records()] == ["first", "third"]


class _SteppedClock(datetime):
    """The journal's clock, which a test can step forward and back."""

    offset = timedelta(0)

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        return datetime.now(tz) + cls.offset


async def test_a_clock_stepped_back_between_two_writers_does_not_wedge_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two servers on one vault, and the clock stepped back between their writes.

    The second writer took its timestamp from a tail it had cached, behind the
    other writer's newer record, so the append was refused after the note had
    been replaced; the pending record kept that timestamp and every later write
    and recovery was refused too.
    """
    monkeypatch.setattr(operation_log, "datetime", _SteppedClock)
    monkeypatch.setattr(_SteppedClock, "offset", timedelta(0))
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
    first = FilesystemVaultWriter(vault, settings)
    second = FilesystemVaultWriter(vault, settings)
    (vault / "n.md").write_bytes(b"# N\n")

    def context(index: int) -> OperationContext:
        return OperationContext(
            op="append", tool="append_journal", actor="test", parameters={"n": index}
        )

    content_hash = await first.mutate_note_atomic(
        "n.md", lambda text: text + "a1\n", operation=context(1)
    )
    monkeypatch.setattr(_SteppedClock, "offset", timedelta(minutes=1))
    content_hash = await second.mutate_note_atomic(
        "n.md", lambda text: text + "b1\n", expected_hash=content_hash, operation=context(2)
    )
    monkeypatch.setattr(_SteppedClock, "offset", timedelta(0))

    await first.mutate_note_atomic(
        "n.md", lambda text: text + "a2\n", expected_hash=content_hash, operation=context(3)
    )
    await second.mutate_note_atomic("n.md", lambda text: text + "b2\n", operation=context(4))

    assert (vault / "n.md").read_bytes() == b"# N\na1\nb1\na2\nb2\n"
    assert not list((vault / ".datacron" / "oplog" / "pending").glob("*.json"))


async def test_recovery_cuts_a_torn_final_fragment_and_keeps_a_copy(tmp_path: Path) -> None:
    """A kill or a power loss mid-append leaves the journal ending mid-line.

    Startup reported success and every write then failed with an internal error,
    for good: nothing cut the fragment. Recovery now removes it, under the journal
    lock, after copying it next to the log.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "n.md").write_bytes(b"# N\n")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    context = OperationContext(op="append", tool="append_journal", actor="test", parameters={})
    await writer.mutate_note_atomic("n.md", lambda text: text + "a\n", operation=context)
    operations = vault / ".datacron" / "oplog" / "operations.jsonl"
    committed = operations.read_bytes()
    fragment = b'{"actor":"x","after_'
    operations.write_bytes(committed + fragment)

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    assert await restarted.recover_operations() == 0
    assert operations.read_bytes() == committed
    backups = list(operations.parent.glob("operations.jsonl.torn-*"))
    assert [backup.read_bytes() for backup in backups] == [fragment]

    await restarted.mutate_note_atomic("n.md", lambda text: text + "b\n", operation=context)
    assert (vault / "n.md").read_bytes() == b"# N\na\nb\n"
    assert len(restarted._operation_journal.read_records()) == 2


async def test_recovery_never_cuts_a_complete_record_that_lost_its_newline(
    tmp_path: Path,
) -> None:
    """A final line that parses is a record, not debris, so it is left for an operator."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "n.md").write_bytes(b"# N\n")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    context = OperationContext(op="append", tool="append_journal", actor="test", parameters={})
    await writer.mutate_note_atomic("n.md", lambda text: text + "a\n", operation=context)
    operations = vault / ".datacron" / "oplog" / "operations.jsonl"
    unterminated = operations.read_bytes().rstrip(b"\n")
    operations.write_bytes(unterminated)

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    with pytest.raises(OperationLogError, match="JSONL boundary"):
        await restarted.mutate_note_atomic("n.md", lambda text: text + "b\n", operation=context)

    assert operations.read_bytes() == unterminated
    assert (vault / "n.md").read_bytes() == b"# N\na\n"
    assert not list(operations.parent.glob("operations.jsonl.torn-*"))


def test_a_pending_record_behind_the_tail_is_restamped_on_recovery(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    journal.append_record(
        _record("ahead", now + timedelta(minutes=1), sha256_bytes(b"z"), sha256_bytes(b"a"))
    )
    stale = _record("stale", now, sha256_bytes(b"a"), sha256_bytes(b"b"))

    restamped = journal.restamped_past_tail(stale)

    assert restamped.operation_id == "stale"
    assert datetime.fromisoformat(restamped.timestamp) > now + timedelta(minutes=1)
    assert journal.append_record(restamped) is True
