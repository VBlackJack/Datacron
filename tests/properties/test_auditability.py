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
"""Invariant properties for durable operation evidence and exact reversal."""

from __future__ import annotations

import json
import shutil
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import hash_text, sha256_bytes
from datacron.core.operation_log import OperationContext
from datacron.core.vault_writer import OPERATION_FAULT_POINTS, FilesystemVaultWriter
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _append_journal_impl,
    _audit_query_impl,
    _create_note_ai_impl,
    _get_note_history_impl,
    _patch_note_section_impl,
    _revert_note_impl,
    _set_frontmatter_impl,
)

pytestmark = pytest.mark.invariants

_NOTE_ID = "01J00000000000000000000041"
_INLINE_TEXT = st.text(
    alphabet=st.characters(
        codec="ascii",
        blacklist_characters=("\x00", "\r", "\n", "#"),
    ),
    min_size=1,
    max_size=32,
).filter(lambda value: bool(value.strip()))
_SUPPRESS_FIXTURE_CHECK = [HealthCheck.function_scoped_fixture]


def _fresh_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir()
    return vault


def _metadata() -> dict[str, object]:
    return {
        "id": _NOTE_ID,
        "title": "Audit property",
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "origin": "human",
        "confidence": "high",
        "last_verified": "2026-01-01",
        "supersedes": [],
        "tags": ["audit"],
    }


async def _open_app(vault: Path) -> tuple[DatacronApp, SQLiteFTS5Store]:
    app_settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        max_result_count=100,
        max_result_tokens=100_000,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    return build_app(settings=app_settings, vault_root=vault, store=store), store


@settings(max_examples=2, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(initial=_INLINE_TEXT, replacement=_INLINE_TEXT, journal_entry=_INLINE_TEXT)
async def test_prop_oplog_completeness(
    tmp_path: Path,
    initial: str,
    replacement: str,
    journal_entry: str,
) -> None:
    """Every acknowledged tool mutation has one exact, content-free audit line."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/oplog-completeness.md"
    body = f"# Audit property\n\n## Target\n\nsecret-initial-{initial}\n\n## Journal\n\nStart.\n"
    app, store = await _open_app(vault)
    try:
        created = await _create_note_ai_impl(
            app,
            rel_path=rel_path,
            title="Audit property",
            body=body,
            origin="ai",
            confidence="high",
            tags=["audit"],
            actor="property-client",
        )
        patched = await _patch_note_section_impl(
            app,
            rel_path=rel_path,
            heading="Target",
            new_content=f"secret-replacement-{replacement}",
            expected_hash=created["content_hash"],
            actor="property-client",
        )
        frontmatter = await _set_frontmatter_impl(
            app,
            rel_path=rel_path,
            confidence="medium",
            expected_hash=patched["content_hash"],
            actor="property-client",
        )
        appended = await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading="Journal",
            entry=f"secret-journal-{journal_entry}",
            expected_hash=frontmatter["content_hash"],
            actor="property-client",
        )
        records = await app.vault_writer.list_operations()
    finally:
        await store.close()

    assert all("error" not in result for result in (created, patched, frontmatter, appended))
    assert [record.tool for record in records] == [
        "create_note_ai",
        "patch_note_section",
        "set_frontmatter",
        "append_journal",
    ]
    assert len({record.operation_id for record in records}) == 4
    assert records[0].before_hash is None
    for previous, current in pairwise(records):
        assert current.before_hash == previous.after_hash
    final_bytes = (vault / rel_path).read_bytes()
    assert records[-1].after_hash == sha256_bytes(final_bytes) == appended["content_hash"]
    assert [record.timestamp for record in records] == sorted(
        record.timestamp for record in records
    )
    assert all(record.note_id == created["created"]["id"] for record in records)
    assert all(record.actor == "property-client" for record in records)

    for record in records[1:]:
        assert record.before_hash is not None
        history = vault / ".datacron" / "history" / record.before_hash
        assert sha256_bytes(history.read_bytes()) == record.before_hash

    raw_log = (vault / ".datacron" / "oplog" / "operations.jsonl").read_text(encoding="ascii")
    assert len(raw_log.splitlines()) == 4
    assert f"secret-initial-{initial}" not in raw_log
    assert f"secret-replacement-{replacement}" not in raw_log
    assert f"secret-journal-{journal_entry}" not in raw_log
    assert all(json.loads(line) for line in raw_log.splitlines())


@settings(max_examples=2, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(entry=_INLINE_TEXT)
async def test_prop_revert_correctness(tmp_path: Path, entry: str) -> None:
    """Revert restores exact historical bytes and journals a reversible revert."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/revert-correctness.md"
    original_raw = serialize(
        _metadata(),
        "# Audit property\n\n## Journal\n\nOriginal exact bytes.\n",
    )
    target = vault / rel_path
    target.parent.mkdir(parents=True)
    target.write_bytes(original_raw.encode("utf-8"))
    original_hash = hash_text(original_raw)

    app, store = await _open_app(vault)
    try:
        appended = await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading="Journal",
            entry=f"- changed-{entry}",
            expected_hash=original_hash,
            actor="revert-property-client",
        )
        changed_bytes = target.read_bytes()
        changed_hash = sha256_bytes(changed_bytes)
        reverted = await _revert_note_impl(
            app,
            note=rel_path,
            to_hash=original_hash,
            expected_hash=changed_hash,
            actor="revert-property-client",
        )
        before_reads_log = (vault / ".datacron" / "oplog" / "operations.jsonl").read_bytes()
        before_reads_note = target.read_bytes()
        history = await _get_note_history_impl(app, note=rel_path, limit=100)
        query = await _audit_query_impl(
            app,
            start=None,
            end=None,
            tool="revert_note",
            note=rel_path,
            limit=100,
        )
        records = await app.vault_writer.list_operations()
    finally:
        await store.close()

    assert "error" not in appended
    assert "error" not in reverted
    assert target.read_bytes() == original_raw.encode("utf-8") == before_reads_note
    assert reverted["content_hash"] == original_hash
    assert len(records) == 2
    revert_record = records[-1]
    assert revert_record.op == "revert"
    assert revert_record.before_hash == changed_hash
    assert revert_record.after_hash == original_hash
    assert (vault / ".datacron" / "history" / changed_hash).read_bytes() == changed_bytes
    assert history["total"] == 2
    assert query["total"] == 1
    assert query["operations"][0]["operation_id"] == revert_record.operation_id
    assert (vault / ".datacron" / "oplog" / "operations.jsonl").read_bytes() == before_reads_log
    assert target.read_bytes() == before_reads_note


@pytest.mark.parametrize("fault_point", list(OPERATION_FAULT_POINTS))
@settings(max_examples=1, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(old_suffix=_INLINE_TEXT, new_suffix=_INLINE_TEXT)
async def test_prop_oplog_durability(
    tmp_path: Path,
    fault_point: str,
    old_suffix: str,
    new_suffix: str,
) -> None:
    """Crash injection recovers to old/no-log or new/exactly-one-log."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/oplog-crash.md"
    old_raw = serialize(_metadata(), f"# Audit property\n\nold-{old_suffix}\n")
    new_raw = serialize(_metadata(), f"# Audit property\n\nnew-{new_suffix}\n")
    target = vault / rel_path
    target.parent.mkdir(parents=True)
    target.write_bytes(old_raw.encode("utf-8"))

    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"injected operation crash at {point}")

    crashing_writer = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault]),
        operation_fault_injector=inject,
    )
    with pytest.raises(RuntimeError, match=f"injected operation crash at {fault_point}"):
        await crashing_writer.mutate_note_atomic(
            rel_path,
            lambda _current: new_raw,
            expected_hash=hash_text(old_raw),
            operation=OperationContext(
                op="patch_section",
                tool="patch_note_section",
                actor="fault-property-client",
                parameters={"new_content_chars": len(new_raw)},
            ),
        )

    recovered_writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await recovered_writer.recover_operations()
    records = await recovered_writer.list_operations()
    final_bytes = target.read_bytes()
    pending = list((vault / ".datacron" / "oplog" / "pending").glob("*.json"))

    assert not pending
    assert final_bytes in {old_raw.encode("utf-8"), new_raw.encode("utf-8")}
    if final_bytes == old_raw.encode("utf-8"):
        assert records == []
    else:
        assert len(records) == 1
        assert records[0].before_hash == hash_text(old_raw)
        assert records[0].after_hash == hash_text(new_raw)
        assert records[0].actor == "fault-property-client"
