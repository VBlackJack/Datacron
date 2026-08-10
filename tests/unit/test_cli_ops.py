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
"""Tests for the fail-closed operation recovery CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.config import Settings, VaultConfig
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.vault_writer import FilesystemVaultWriter

_RECOVERY_TIMESTAMP = "2026-08-10T00:00:00+00:00"


@pytest.fixture
def runner() -> CliRunner:
    """Return an isolated Typer runner."""
    return CliRunner()


def _blocked_operation(
    tmp_path: Path,
    *,
    committed: bool = False,
) -> tuple[Path, FilesystemVaultWriter, OperationRecord, str]:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    before = b"before\n"
    after = b"after\n"
    disk = b"external\n"
    target.write_bytes(disk)
    writer = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault]),
        VaultConfig(),
    )
    before_hash = writer._operation_journal.store_history(before)
    record = OperationRecord(
        operation_id="blocked-operation",
        timestamp=_RECOVERY_TIMESTAMP,
        op="append",
        tool="append_journal",
        note_id=None,
        rel_path="note.md",
        before_hash=before_hash,
        after_hash=sha256_bytes(after),
        actor="test-actor",
        parameters={"heading": "Journal"},
        history_stored=True,
    )
    if committed:
        writer._operation_journal.append_record(record)
    writer._operation_journal.write_pending(record)
    return vault, writer, record, sha256_bytes(disk)


class TestOpsInspect:
    """Inspection must expose repair evidence without changing it."""

    def test_inspect_is_read_only_and_reports_both_available_actions(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        target = vault / record.rel_path
        pending_path = writer._operation_journal.pending_path(record.operation_id)
        pending_before = pending_path.read_bytes()
        history_path = vault / ".datacron" / "history" / str(record.before_hash)
        history_before = history_path.read_bytes()

        result = runner.invoke(app, ["ops", "inspect", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Recovery inspection: 1 blocked operation" in result.stdout
        assert f"operation_id: {record.operation_id}" in result.stdout
        assert "reason: pending_disk_hash_mismatch" in result.stdout
        assert f"disk_hash: {disk_hash}" in result.stdout
        assert "restore-before: available" in result.stdout
        assert "adopt-disk: available" in result.stdout
        assert "No changes made." in result.stdout
        assert target.read_bytes() == b"external\n"
        assert pending_path.read_bytes() == pending_before
        assert history_path.read_bytes() == history_before
        assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()
        assert not (vault / ".datacron" / "index").exists()

    def test_inspect_reports_clean_state(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()

        result = runner.invoke(app, ["ops", "inspect", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Recovery inspection: no blocked operations." in result.stdout
        assert "No changes made." in result.stdout


class TestOpsRepair:
    """Repairs require exact operator intent and leave durable evidence."""

    def test_repair_refuses_path_shaped_operation_id(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        pending_path = writer._operation_journal.pending_path(record.operation_id)
        pending_before = pending_path.read_bytes()

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                "../blocked-operation",
                "--action",
                "adopt-disk",
                "--expected-disk-hash",
                disk_hash,
                "--confirm",
                "../blocked-operation",
            ],
        )

        assert result.exit_code == 1
        assert "opaque filename-safe identifier" in result.stderr
        assert pending_path.read_bytes() == pending_before
        assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()

    def test_repair_refuses_without_exact_confirmation(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        pending_path = writer._operation_journal.pending_path(record.operation_id)
        pending_before = pending_path.read_bytes()

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                record.operation_id,
                "--action",
                "adopt-disk",
                "--expected-disk-hash",
                disk_hash,
            ],
        )

        assert result.exit_code == 1
        assert f"Pass --confirm {record.operation_id}" in result.stderr
        assert (vault / record.rel_path).read_bytes() == b"external\n"
        assert pending_path.read_bytes() == pending_before
        assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()

    def test_repair_refuses_when_disk_changed_after_inspection(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        target = vault / record.rel_path
        target.write_bytes(b"changed-again\n")
        pending_path = writer._operation_journal.pending_path(record.operation_id)
        pending_before = pending_path.read_bytes()

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                record.operation_id,
                "--action",
                "adopt-disk",
                "--expected-disk-hash",
                disk_hash,
                "--confirm",
                record.operation_id,
            ],
        )

        assert result.exit_code == 1
        assert "disk hash changed since inspection" in result.stderr
        assert target.read_bytes() == b"changed-again\n"
        assert pending_path.read_bytes() == pending_before
        assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()

    def test_adopt_disk_preserves_bytes_and_records_resolution(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        target = vault / record.rel_path

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                record.operation_id,
                "--action",
                "adopt-disk",
                "--expected-disk-hash",
                disk_hash,
                "--confirm",
                record.operation_id,
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Repair complete: adopted current disk bytes" in result.stdout
        assert target.read_bytes() == b"external\n"
        assert not writer._operation_journal.pending_path(record.operation_id).exists()
        records = writer._operation_journal.read_records()
        assert len(records) == 1
        repair = records[0]
        assert repair.op == "recovery_adopt"
        assert repair.tool == "datacron_ops_repair"
        assert repair.before_hash == disk_hash
        assert repair.after_hash == disk_hash
        assert repair.parameters["resolves_operation_id"] == record.operation_id
        assert repair.parameters["action"] == "adopt-disk"

    def test_restore_before_uses_history_and_records_resolution(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path)
        target = vault / record.rel_path

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                record.operation_id,
                "--action",
                "restore-before",
                "--expected-disk-hash",
                disk_hash,
                "--confirm",
                record.operation_id,
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Repair complete: restored exact before bytes" in result.stdout
        assert target.read_bytes() == b"before\n"
        assert not writer._operation_journal.pending_path(record.operation_id).exists()
        records = writer._operation_journal.read_records()
        assert len(records) == 1
        repair = records[0]
        assert repair.op == "recovery_restore"
        assert repair.tool == "datacron_ops_repair"
        assert repair.before_hash == disk_hash
        assert repair.after_hash == record.before_hash
        assert repair.parameters["resolves_operation_id"] == record.operation_id
        assert repair.parameters["action"] == "restore-before"
        assert (vault / ".datacron" / "history" / disk_hash).read_bytes() == b"external\n"

    def test_adopt_repairs_committed_disk_divergence(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, writer, record, disk_hash = _blocked_operation(tmp_path, committed=True)

        result = runner.invoke(
            app,
            [
                "ops",
                "repair",
                "--vault",
                str(vault),
                "--operation-id",
                record.operation_id,
                "--action",
                "adopt-disk",
                "--expected-disk-hash",
                disk_hash,
                "--confirm",
                record.operation_id,
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        records = writer._operation_journal.read_records()
        assert len(records) == 2
        assert records[-1].parameters["resolves_operation_id"] == record.operation_id
        assert not writer._operation_journal.pending_path(record.operation_id).exists()
