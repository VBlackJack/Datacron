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

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.config import Settings, VaultConfig
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.reliability import scan_vault_read_only

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


_CANONICAL_ID = "01J00000000000000000000021"
_OTHER_ID = "01J00000000000000000000022"
_THIRD_ID = "01J00000000000000000000023"
_MALFORMED_ID = "01KVMTG0IA2AGENTSCPDC0616"
_NOTE_BODY = "# Point IA\n\nBody line one.\nBody line two.\n"


def _note_bytes(note_id: str, title: str = "Point IA", body: str = _NOTE_BODY) -> bytes:
    metadata = {
        "id": note_id,
        "title": title,
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "origin": "human",
        "confidence": "high",
    }
    return serialize(metadata, body).encode("utf-8")


def _without_updated(raw: bytes) -> bytes:
    """Drop the frontmatter `updated` line, which every sanctioned write stamps."""
    return b"\n".join(line for line in raw.split(b"\n") if not line.startswith(b"updated:"))


def _indexed_vault(runner: CliRunner, tmp_path: Path, notes: dict[str, bytes]) -> Path:
    """Build a vault and its real FTS index through the shipped `index` command."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for rel_path, raw in notes.items():
        target = vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    result = runner.invoke(app, ["index", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout + result.stderr
    return vault


def _mismatched_vault(
    runner: CliRunner,
    tmp_path: Path,
    *,
    frontmatter_id: str = _MALFORMED_ID,
) -> tuple[Path, Path, str]:
    """Index a note under the canonical ID, then diverge only its frontmatter."""
    vault = _indexed_vault(
        runner,
        tmp_path,
        {"note.md": _note_bytes(_CANONICAL_ID), "other.md": _note_bytes(_OTHER_ID, "Other")},
    )
    target = vault / "note.md"
    target.write_bytes(_note_bytes(frontmatter_id))
    return vault, target, sha256_bytes(target.read_bytes())


def _duplicate_vault(runner: CliRunner, tmp_path: Path) -> Path:
    return _indexed_vault(
        runner,
        tmp_path,
        {
            "one.md": _note_bytes(_CANONICAL_ID, "One"),
            "two.md": _note_bytes(_CANONICAL_ID, "Two"),
        },
    )


def _indexed_note_ids(vault: Path) -> dict[str, str]:
    connection = sqlite3.connect(vault / ".datacron" / "index" / "datacron.db")
    try:
        rows = connection.execute("SELECT rel_path, note_id FROM notes").fetchall()
    finally:
        connection.close()
    return {str(rel_path): str(note_id) for rel_path, note_id in rows}


def _repair_id_argv(
    vault: Path, rel_path: str, action: str, expected_hash: str, confirm: str
) -> list[str]:
    return [
        "ops",
        "repair-id",
        "--vault",
        str(vault),
        "--rel-path",
        rel_path,
        "--action",
        action,
        "--expected-hash",
        expected_hash,
        "--confirm",
        confirm,
    ]


class TestOpsInspectId:
    """Identity inspection must expose repair evidence without changing it."""

    def test_inspect_id_reports_clean_vault(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Identity inspection: no ID divergences." in result.stdout
        assert "No changes made." in result.stdout

    def test_inspect_id_reports_every_source_and_the_hash_to_copy(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        before = target.read_bytes()

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Identity inspection: 1 ID divergence" in result.stdout
        assert "rel_path: note.md" in result.stdout
        assert f"frontmatter: {_MALFORMED_ID}" in result.stdout
        assert "sidecar: <absent>" in result.stdout
        assert f"sqlite: {_CANONICAL_ID}" in result.stdout
        assert "classification: mismatch" in result.stdout
        assert f"content_hash: {content_hash}" in result.stdout
        assert "recommended action: adopt-index" in result.stdout
        assert "No changes made." in result.stdout
        assert target.read_bytes() == before

    def test_inspect_id_never_recommends_repairing_a_duplicate(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = _duplicate_vault(runner, tmp_path)

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "classification: duplicate" in result.stdout
        assert "duplicate IDs are reported, never repaired automatically" in result.stdout


class TestOpsRepairId:
    """Identity repairs require exact operator intent and converge the scan."""

    def test_adopt_index_repairs_and_preserves_every_other_byte(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Identity repair complete: adopted the index ID." in result.stdout
        assert f"note_id: {_CANONICAL_ID}" in result.stdout
        assert "id_mismatches: 1 -> 0" in result.stdout
        after = target.read_bytes()
        expected = before.replace(_MALFORMED_ID.encode(), _CANONICAL_ID.encode())
        assert _without_updated(after) == _without_updated(expected)
        assert after.endswith(b"Body line two.\n")
        assert not scan_vault_read_only(vault).id_violations
        assert _indexed_note_ids(vault)["note.md"] == _CANONICAL_ID

    def test_adopt_index_journals_the_repair(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, _target, content_hash = _mismatched_vault(runner, tmp_path)

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]), VaultConfig())
        records = writer._operation_journal.read_records()
        assert [record.op for record in records] == ["repair_id"]
        assert records[0].tool == "datacron_ops_repair_id"
        assert records[0].rel_path == "note.md"
        assert records[0].before_hash == content_hash
        assert records[0].parameters["action"] == "adopt-index"
        assert records[0].parameters["note_id"] == _CANONICAL_ID

    def test_adopt_frontmatter_realigns_sidecar_and_index_without_touching_the_note(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        target = vault / "note.md"
        target.write_bytes(_note_bytes(_THIRD_ID))
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"note.md": _CANONICAL_ID}), encoding="ascii")
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(
                vault,
                "note.md",
                "adopt-frontmatter",
                sha256_bytes(before),
                "note.md",
            ),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Identity repair complete: adopted the frontmatter ID." in result.stdout
        assert "note rewritten: no" in result.stdout
        assert "id_mismatches: 1 -> 0" in result.stdout
        assert target.read_bytes() == before
        assert json.loads(sidecar.read_text(encoding="utf-8")) == {"note.md": _THIRD_ID}
        assert _indexed_note_ids(vault)["note.md"] == _THIRD_ID
        assert not scan_vault_read_only(vault).id_violations

    def test_repair_id_refuses_adopt_frontmatter_for_a_malformed_ulid(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-frontmatter", content_hash, "note.md"),
        )

        assert result.exit_code == 1
        assert "is not a canonical 26-character Crockford ULID" in result.stderr
        assert target.read_bytes() == before
        assert _indexed_note_ids(vault)["note.md"] == _CANONICAL_ID

    def test_repair_id_refuses_a_divergent_expected_hash(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, _content_hash = _mismatched_vault(runner, tmp_path)
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", sha256_bytes(b"stale"), "note.md"),
        )

        assert result.exit_code == 1
        assert "hash mismatch" in result.stderr
        assert target.read_bytes() == before

    def test_repair_id_refuses_without_exact_confirmation(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "other.md"),
        )

        assert result.exit_code == 1
        assert "Pass --confirm note.md" in result.stderr
        assert target.read_bytes() == before

    def test_repair_id_refuses_when_there_is_nothing_to_repair(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, _target, _content_hash = _mismatched_vault(runner, tmp_path)
        other = vault / "other.md"
        before = other.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "other.md", "adopt-index", sha256_bytes(before), "other.md"),
        )

        assert result.exit_code == 1
        assert "no ID divergence recorded for other.md" in result.stderr
        assert other.read_bytes() == before

    def test_repair_id_refuses_a_note_without_frontmatter(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        target = vault / "note.md"
        target.write_bytes(b"# Plain note\n\nNo frontmatter here.\n")
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"note.md": _OTHER_ID}), encoding="ascii")
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", sha256_bytes(before), "note.md"),
        )

        assert result.exit_code == 1
        assert "note has no frontmatter" in result.stderr
        assert target.read_bytes() == before

    def test_repair_id_reports_a_duplicate_instead_of_guessing(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault = _duplicate_vault(runner, tmp_path)
        target = vault / "one.md"
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "one.md", "adopt-index", sha256_bytes(before), "one.md"),
        )

        assert result.exit_code == 1
        assert "carries a duplicate ID, not a mismatch" in result.stderr
        assert target.read_bytes() == before

    def test_repair_id_refuses_when_the_migrated_sidecar_would_restore_the_old_id(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        migrated = vault / ".datacron" / "ulids.json.migrated"
        migrated.write_text(json.dumps({"note.md": _THIRD_ID}), encoding="ascii")
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 1
        assert "migrated sidecar" in result.stderr
        assert target.read_bytes() == before
