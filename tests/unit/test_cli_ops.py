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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datacron.cli import app
from datacron.core.config import Settings, VaultConfig
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import OperationRecord
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.mcp.tools.write_validation import is_canonical_ulid
from datacron.reliability import ReliabilityScan, scan_vault_read_only

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
_FOURTH_ID = "01J00000000000000000000024"
_MALFORMED_ID = "01JIIIIIIIIIIIIIIIIIIIIIII"
_NOTE_BODY = "# Sample note\n\nBody line one.\nBody line two.\n"


def _note_bytes(note_id: str, title: str = "Sample note", body: str = _NOTE_BODY) -> bytes:
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
    """Drop the timestamp that an identity-repair frontmatter rewrite refreshes."""
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

    def test_inspect_id_falls_back_when_the_preferred_id_is_claimed_elsewhere(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """A contested index ID must fall back to an executable frontmatter action."""
        vault = _indexed_vault(
            runner,
            tmp_path,
            {
                "a.md": _note_bytes(_CANONICAL_ID, "A"),
                "b.md": _note_bytes(_OTHER_ID, "B"),
            },
        )
        contested = vault / "b.md"
        contested.write_bytes(_note_bytes(_THIRD_ID, "B"))
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"b.md": _CANONICAL_ID}), encoding="ascii")
        connection = sqlite3.connect(vault / ".datacron" / "index" / "datacron.db")
        try:
            connection.execute("DELETE FROM notes WHERE rel_path = ?", ("b.md",))
            connection.execute("DELETE FROM ulid_paths WHERE rel_path = ?", ("b.md",))
            connection.commit()
        finally:
            connection.close()
        note_before = contested.read_bytes()
        sidecar_before = sidecar.read_bytes()
        index_before = _indexed_note_ids(vault)

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        b_block = result.stdout.split("rel_path: b.md", 1)[1]
        assert "recommended action: adopt-frontmatter" in b_block
        assert contested.read_bytes() == note_before
        assert sidecar.read_bytes() == sidecar_before
        assert _indexed_note_ids(vault) == index_before

    def test_inspect_id_recommends_none_when_every_candidate_id_is_claimed(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """No repair action is executable when both candidate IDs collide."""
        vault = _indexed_vault(
            runner,
            tmp_path,
            {
                "a.md": _note_bytes(_CANONICAL_ID, "A"),
                "b.md": _note_bytes(_OTHER_ID, "B"),
                "c.md": _note_bytes(_THIRD_ID, "C"),
            },
        )
        contested = vault / "b.md"
        contested.write_bytes(_note_bytes(_THIRD_ID, "B"))
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"b.md": _CANONICAL_ID}), encoding="ascii")
        connection = sqlite3.connect(vault / ".datacron" / "index" / "datacron.db")
        try:
            connection.execute("DELETE FROM notes WHERE rel_path = ?", ("b.md",))
            connection.execute("DELETE FROM ulid_paths WHERE rel_path = ?", ("b.md",))
            connection.commit()
        finally:
            connection.close()

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        b_block = result.stdout.split("rel_path: b.md", 1)[1].split("rel_path: c.md", 1)[0]
        assert "recommended action: none --" in b_block
        assert f"{_CANONICAL_ID} is already carried by a.md" in b_block
        assert f"{_THIRD_ID} is already carried by c.md" in b_block


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

    def test_adopt_index_preserves_the_body_but_reserializes_the_frontmatter(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Body bytes are exact; identity repair canonicalizes the frontmatter.

        The other byte-preservation test starts from a `serialize`-produced note, which is
        already canonical, so it cannot see this. A hand-written frontmatter comes back with
        more changed lines than `id` alone, and the documentation says so.
        """
        handwritten = (
            "---\n"
            f"id: {_CANONICAL_ID}\n"
            "title: Sample note\n"
            "created: 2026-06-16T18:00:00+02:00\n"
            "updated: 2026-06-16T18:00:00+02:00\n"
            "origin: ai\n"
            "tags: [alpha, beta, gamma]\n"
            "---\n"
        ) + _NOTE_BODY
        vault = _indexed_vault(runner, tmp_path, {"note.md": handwritten.encode("utf-8")})
        target = vault / "note.md"
        target.write_bytes(handwritten.replace(_CANONICAL_ID, _MALFORMED_ID).encode("utf-8"))
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", sha256_bytes(before), "note.md"),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        after = target.read_bytes()
        assert after.split(b"---\n", 2)[2] == before.split(b"---\n", 2)[2]
        assert f"id: {_CANONICAL_ID}\n".encode() in after
        assert b"tags:\n- alpha\n" in after
        assert b"created: 2026-06-16 18:00:00+02:00\n" in after

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

    def test_adopt_frontmatter_rechecks_hash_under_lock_before_side_effects(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent note edit must leave the sidecar and index untouched."""
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        target = vault / "note.md"
        target.write_bytes(_note_bytes(_THIRD_ID))
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"note.md": _CANONICAL_ID}), encoding="ascii")
        before = target.read_bytes()
        concurrent = before + b"Concurrent body edit.\n"
        sidecar_before = sidecar.read_bytes()
        index_before = _indexed_note_ids(vault)
        real_scan = scan_vault_read_only
        scan_calls = 0

        def _scan_then_edit(vault_root: Path) -> ReliabilityScan:
            nonlocal scan_calls
            scan = real_scan(vault_root)
            scan_calls += 1
            if scan_calls == 1:
                target.write_bytes(concurrent)
            return scan

        monkeypatch.setattr("datacron.reliability.scan_vault_read_only", _scan_then_edit)

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

        assert result.exit_code == 1
        assert "hash mismatch" in result.stderr
        assert target.read_bytes() == concurrent
        assert sidecar.read_bytes() == sidecar_before
        assert _indexed_note_ids(vault) == index_before

    def test_adopt_index_rechecks_rewritten_hash_before_source_side_effects(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A post-rewrite edit must stop stale sidecar and index effects."""
        vault, target, content_hash = _mismatched_vault(runner, tmp_path)
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"note.md": _THIRD_ID}), encoding="ascii")
        sidecar_before = sidecar.read_bytes()
        index_before = _indexed_note_ids(vault)
        real_lock = FilesystemVaultWriter.lock_note_identity
        concurrent: list[bytes] = []

        @contextmanager
        def _edit_then_lock(
            writer: FilesystemVaultWriter,
            rel_path: str,
            *,
            expected_hash: str,
        ) -> Iterator[None]:
            rewritten = target.read_bytes()
            concurrent.append(rewritten + b"Concurrent body edit.\n")
            target.write_bytes(concurrent[-1])
            with real_lock(writer, rel_path, expected_hash=expected_hash):
                yield

        monkeypatch.setattr(FilesystemVaultWriter, "lock_note_identity", _edit_then_lock)

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 1
        assert "applied in part" in result.stderr
        assert "hash mismatch" in result.stderr
        assert target.read_bytes() == concurrent[-1]
        assert sidecar.read_bytes() == sidecar_before
        assert _indexed_note_ids(vault) == index_before

    def test_adopt_index_holds_identity_lock_through_source_realignments(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The post-rewrite lock must cover sidecar, index, and final scan work."""
        vault, _target, content_hash = _mismatched_vault(runner, tmp_path)
        real_lock = FilesystemVaultWriter.lock_note_identity
        from datacron.cli import _apply_id_source_realignments

        lock_held = False

        @contextmanager
        def _tracked_lock(
            writer: FilesystemVaultWriter,
            rel_path: str,
            *,
            expected_hash: str,
        ) -> Iterator[None]:
            nonlocal lock_held
            with real_lock(writer, rel_path, expected_hash=expected_hash):
                lock_held = True
                try:
                    yield
                finally:
                    lock_held = False

        async def _assert_locked_then_realign(
            vault_root: Path,
            rel_path: str,
            note_id: str,
            sources: dict[str, str],
        ) -> tuple[bool, str | None, str | None]:
            assert lock_held
            return await _apply_id_source_realignments(
                vault_root,
                rel_path,
                note_id,
                sources,
            )

        monkeypatch.setattr(FilesystemVaultWriter, "lock_note_identity", _tracked_lock)
        monkeypatch.setattr(
            "datacron.cli._apply_id_source_realignments",
            _assert_locked_then_realign,
        )

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "id_mismatches: 1 -> 0" in result.stdout

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

        inspection = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert inspection.exit_code == 0, inspection.stdout + inspection.stderr
        assert "recommended action: none -- migrated sidecar" in inspection.stdout

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 1
        assert "migrated sidecar" in result.stderr
        assert target.read_bytes() == before


class TestIsCanonicalUlid:
    """The alphabet check is the only barrier: the reader accepts any 26 characters."""

    @pytest.mark.parametrize(
        "value",
        [
            "01J00000000000000000000021",
            "5H87J7Q43H4S9HE0KHDTH186EX",
        ],
    )
    def test_accepts_canonical_ulids(self, value: str) -> None:
        assert is_canonical_ulid(value)

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("01KVMTG0IA2AGENTSCPDC0616", "25 characters"),
            ("01J000000000000000000000211", "27 characters"),
            ("01JIIIIIIIIIIIIIIIIIIIIIII", "I is excluded from Crockford base32"),
            ("01JLLLLLLLLLLLLLLLLLLLLLLL", "L is excluded"),
            ("01JOOOOOOOOOOOOOOOOOOOOOOO", "O is excluded"),
            ("01JUUUUUUUUUUUUUUUUUUUUUUU", "U is excluded"),
            ("01j00000000000000000000021", "lowercase"),
            ("", "empty"),
        ],
    )
    def test_rejects_everything_else(self, value: str, why: str) -> None:
        assert not is_canonical_ulid(value), why


class TestOpsRepairIdSafety:
    """Regressions for defects that damaged unrelated notes."""

    def test_refuses_an_id_another_note_already_carries(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """A note that is duplicate AND mismatched is classified `mismatch`.

        Adopting the contested ID makes the index upsert overwrite the other note's
        row by `note_id`: it stays on disk but vanishes from search and backlinks.
        """
        vault = _indexed_vault(
            runner,
            tmp_path,
            {
                "a.md": _note_bytes(_CANONICAL_ID, "A"),
                "b.md": _note_bytes(_OTHER_ID, "B"),
            },
        )
        contested = vault / "b.md"
        contested.write_bytes(_note_bytes(_CANONICAL_ID, "B"))
        before = contested.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "b.md", "adopt-frontmatter", sha256_bytes(before), "b.md"),
        )

        assert result.exit_code == 1
        assert "already carried by a.md" in result.stderr
        assert contested.read_bytes() == before
        assert _indexed_note_ids(vault) == {"a.md": _CANONICAL_ID, "b.md": _OTHER_ID}

    def test_sidecar_realignment_leaves_other_mappings_alone(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Loading the sidecar through JsonIdStore merged the migrated file back in.

        Repairing one note then stamped every stale migrated mapping onto unrelated
        notes, creating divergences the operator never asked about.
        """
        vault = _indexed_vault(
            runner,
            tmp_path,
            {
                "target.md": _note_bytes(_CANONICAL_ID, "Target"),
                "bystander.md": _note_bytes(_OTHER_ID, "Bystander"),
            },
        )
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"target.md": _THIRD_ID}), encoding="ascii")
        migrated = vault / ".datacron" / "ulids.json.migrated"
        migrated.write_text(json.dumps({"bystander.md": _FOURTH_ID}), encoding="ascii")
        target = vault / "target.md"

        result = runner.invoke(
            app,
            _repair_id_argv(
                vault,
                "target.md",
                "adopt-index",
                sha256_bytes(target.read_bytes()),
                "target.md",
            ),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        written = json.loads(sidecar.read_text(encoding="utf-8"))
        assert written == {"target.md": _CANONICAL_ID}
        assert "bystander.md" not in written

    def test_reports_a_partial_application_instead_of_a_refusal(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Saying "refused" after writing the note sends the operator away misinformed."""
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        target = vault / "note.md"
        target.write_bytes(_note_bytes(_MALFORMED_ID))
        sidecar = vault / ".datacron" / "ulids.json"
        sidecar.write_text(json.dumps({"note.md": _THIRD_ID}), encoding="ascii")
        before = target.read_bytes()

        def _unavailable(*_args: object, **_kwargs: object) -> None:
            raise OSError("sidecar unavailable")

        monkeypatch.setattr("datacron.cli._realign_sidecar_entry", _unavailable)

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", sha256_bytes(before), "note.md"),
        )

        assert result.exit_code == 1
        assert "applied in part" in result.stderr
        assert "refused" not in result.stderr
        assert target.read_bytes() != before

    def test_index_failure_does_not_claim_an_absent_sidecar_was_written(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Partial-repair evidence must describe only durable state that exists."""
        vault, _target, content_hash = _mismatched_vault(runner, tmp_path)

        async def _index_unavailable(
            *_args: object,
            **_kwargs: object,
        ) -> tuple[str | None, str | None]:
            raise sqlite3.OperationalError("index unavailable")

        monkeypatch.setattr("datacron.cli._realign_index_identity", _index_unavailable)

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-index", content_hash, "note.md"),
        )

        assert result.exit_code == 1
        assert "applied in part" in result.stderr
        assert "any required sidecar realignment completed" in result.stderr
        assert "and the sidecar now carry" not in result.stderr
        assert not (vault / ".datacron" / "ulids.json").exists()

    def test_realigns_an_index_row_when_the_note_bytes_never_change(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """The stale row was looked up in `ulid_paths` while reconcile gates on `notes`.

        With no `ulid_paths` mapping the pre-drop was a no-op, the mtime gate skipped
        the untouched note, and the stale `notes` row survived while the command still
        reported success.
        """
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        connection = sqlite3.connect(vault / ".datacron" / "index" / "datacron.db")
        try:
            connection.execute(
                "UPDATE notes SET note_id = ? WHERE rel_path = ?", (_THIRD_ID, "note.md")
            )
            connection.execute("DELETE FROM ulid_paths WHERE rel_path = ?", ("note.md",))
            connection.commit()
        finally:
            connection.close()
        target = vault / "note.md"
        before = target.read_bytes()

        result = runner.invoke(
            app,
            _repair_id_argv(vault, "note.md", "adopt-frontmatter", sha256_bytes(before), "note.md"),
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert target.read_bytes() == before
        assert f"indexed id: {_THIRD_ID} -> {_CANONICAL_ID}" in result.stdout
        assert _indexed_note_ids(vault)["note.md"] == _CANONICAL_ID

    def test_inspect_id_recommends_the_action_repair_id_accepts(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """A malformed canonical ID must not be recommended for adoption."""
        vault = _indexed_vault(runner, tmp_path, {"note.md": _note_bytes(_CANONICAL_ID)})
        connection = sqlite3.connect(vault / ".datacron" / "index" / "datacron.db")
        try:
            connection.execute(
                "UPDATE notes SET note_id = ? WHERE rel_path = ?", (_MALFORMED_ID, "note.md")
            )
            connection.commit()
        finally:
            connection.close()

        result = runner.invoke(app, ["ops", "inspect-id", "--vault", str(vault)])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "recommended action: adopt-frontmatter" in result.stdout
