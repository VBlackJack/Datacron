# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Durable request replay, real process interruption, and targeted indexing oracles."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools.ops import _get_note_history_impl
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.write import _append_journal_impl, _revert_note_impl

_ID = "01J00000000000000000000091"
_KEY = "integration-request-001"


@pytest.fixture
async def request_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    (tmp_path / "note.md").write_text(
        serialize({"id": _ID}, "# Root\n\n## Log\n\nInitial\n"), encoding="utf-8"
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        yield app
    finally:
        await app.store.close()


async def _call(app: DatacronApp, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await create_server(app).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    return dict(json.loads(result.content[0].text))


async def test_concurrent_replay_and_receipt_lookup(request_app: DatacronApp) -> None:
    app = request_app
    arguments = {
        "rel_path": "note.md",
        "heading": "Log",
        "entry": "ExactlyOnce",
        "request_id": _KEY,
    }
    first, second = await asyncio.gather(
        _call(app, "append_journal", arguments), _call(app, "append_journal", arguments)
    )
    assert {first["replayed"], second["replayed"]} == {True, False}
    assert first["operation_id"] == second["operation_id"]
    assert (app.vault_root / "note.md").read_text().count("ExactlyOnce") == 1
    receipt = await _get_note_history_impl(app, note="note.md", limit=100, request_id=_KEY)
    assert receipt["total"] == 1
    changed = await _call(app, "append_journal", {**arguments, "entry": "Different"})
    assert changed["error"]["type"] == "WriteConflictError"
    assert len(await app.vault_writer.list_operations()) == 1


async def test_history_says_which_restore_points_still_exist(request_app: DatacronApp) -> None:
    """A listed operation must not offer a revert whose bytes retention deleted.

    ``history_stored`` records what happened when the write committed. It says
    nothing about now: retention deletes a version once it falls out of the window.
    A caller reading ``history_stored: true`` on an old operation would offer a
    restore point that no longer exists, which on a vault picked up after months
    away is the ordinary case, not the rare one.
    """
    app = request_app
    await _call(
        app,
        "append_journal",
        {"rel_path": "note.md", "heading": "Log", "entry": "First", "request_id": "req-001"},
    )
    await _call(
        app,
        "append_journal",
        {"rel_path": "note.md", "heading": "Log", "entry": "Second", "request_id": "req-002"},
    )

    history = await _get_note_history_impl(app, note="note.md", limit=100)
    operations = history["operations"]
    assert [operation["history_stored"] for operation in operations] == [True, True]
    assert [operation["restore_available"] for operation in operations] == [True, True]

    purged = operations[0]["before_hash"]
    (app.vault_root / ".datacron" / "history" / purged).unlink()

    history = await _get_note_history_impl(app, note="note.md", limit=100)
    operations = history["operations"]
    assert [operation["history_stored"] for operation in operations] == [True, True]
    assert [operation["restore_available"] for operation in operations] == [False, True]


@pytest.mark.parametrize("fault", ["after_pending_write", "after_note_write", "after_oplog_write"])
async def test_retry_after_process_exit(tmp_path: Path, fault: str) -> None:
    (tmp_path / "note.md").write_text(
        serialize({"id": _ID}, "# Root\n\n## Log\n"), encoding="utf-8"
    )
    script = """
import asyncio, os, sys
from pathlib import Path
from datacron.core.config import Settings
from datacron.core.paths import sidecar_index_db
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.mcp.server import build_app
from datacron.mcp.tools.write import _append_journal_impl
async def main():
    root=Path(sys.argv[1])
    settings=Settings(vault_root=root,read_paths=[root],write_paths=[root])
    app=build_app(settings=settings,vault_root=root)
    def fault(point):
        if point==sys.argv[2]: os._exit(87)
    app.vault_writer._delegate._operation_fault_injector=fault
    await app.store.open(sidecar_index_db(root))
    await _append_journal_impl(app,rel_path="note.md",heading="Log",
                               entry="CrashEntry",request_id="crash-key")
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(tmp_path), fault],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 87, result.stderr.decode(errors="replace")
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        result_payload = await _append_journal_impl(
            app, rel_path="note.md", heading="Log", entry="CrashEntry", request_id="crash-key"
        )
        assert "error" not in result_payload
        assert result_payload["replayed"] is (fault != "after_pending_write")
        assert (tmp_path / "note.md").read_text().count("CrashEntry") == 1
        assert len(await app.vault_writer.list_operations()) == 1
    finally:
        await app.store.close()


async def test_historical_receipt_does_not_reapply_after_later_edit(
    request_app: DatacronApp,
) -> None:
    """A receipt older than the note must not reapply, and must not be handed back.

    The write still does not happen, which is what this has always guarded. What it
    returns changed: it used to answer with the old receipt, whose `content_hash`
    names bytes that are no longer anywhere, so a caller re-reading by that hash
    found nothing and could not tell why. Without `expected_hash` there is nothing
    left to tell a retry from a new write, so it refuses and says what to send.
    """
    app = request_app
    await _append_journal_impl(
        app, rel_path="note.md", heading="Log", entry="First", request_id=_KEY
    )
    await _append_journal_impl(app, rel_path="note.md", heading="Log", entry="Later")
    before = (app.vault_root / "note.md").read_bytes()
    replay = await _append_journal_impl(
        app, rel_path="note.md", heading="Log", entry="First", request_id=_KEY
    )
    assert replay["error"]["type"] == "WriteConflictError"
    assert "expected_hash" in replay["error"]["message"]
    assert before == (app.vault_root / "note.md").read_bytes()
    assert len(await app.vault_writer.list_operations()) == 2


async def test_a_stale_retry_with_expected_hash_conflicts_instead_of_duplicating(
    request_app: DatacronApp,
) -> None:
    """A retry that carries the world it remembers is refused once the world moved.

    This is the sibling of the case above, with `expected_hash` supplied. The receipt
    no longer describes the note, so the write is not suppressed; CAS then refuses it
    because the hash the caller remembers is not the hash on disk. Either path must
    end without a second copy of the entry.
    """
    app = request_app
    expected = sha256_bytes((app.vault_root / "note.md").read_bytes())
    await _append_journal_impl(
        app,
        rel_path="note.md",
        heading="Log",
        entry="Once",
        expected_hash=expected,
        request_id=_KEY,
    )
    await _append_journal_impl(app, rel_path="note.md", heading="Log", entry="Later")

    retry = await _append_journal_impl(
        app,
        rel_path="note.md",
        heading="Log",
        entry="Once",
        expected_hash=expected,
        request_id=_KEY,
    )

    assert "error" in retry, retry
    assert (app.vault_root / "note.md").read_text().count("Once") == 1


async def test_a_reverted_write_is_written_again_by_the_same_request(
    request_app: DatacronApp,
) -> None:
    """Reverting a note must not turn its request into a receipt for nothing.

    `prepare_follow_up` derives the request id from the plan, so a plan re-prepared
    after a revert carries the same id and the same bytes by design. The receipt from
    the first apply used to suppress the second one and report `committed: true`,
    which loses the entry with no trace anywhere. The receipt now only suppresses a
    write the note still holds.
    """
    app = request_app
    original = sha256_bytes((app.vault_root / "note.md").read_bytes())
    first = await _append_journal_impl(
        app,
        rel_path="note.md",
        heading="Log",
        entry="Restored",
        expected_hash=original,
        request_id=_KEY,
    )
    assert first["replayed"] is False
    await _revert_note_impl(app, note="note.md", to_hash=original)
    assert "Restored" not in (app.vault_root / "note.md").read_text()

    again = await _append_journal_impl(
        app,
        rel_path="note.md",
        heading="Log",
        entry="Restored",
        expected_hash=original,
        request_id=_KEY,
    )

    assert "error" not in again, again
    assert again["replayed"] is False
    assert again["operation_id"] != first["operation_id"]
    assert (app.vault_root / "note.md").read_text().count("Restored") == 1


@pytest.mark.parametrize("key", ["", ".invalid", "x" * 129, "clé"])
async def test_invalid_request_key_never_mutates(request_app: DatacronApp, key: str) -> None:
    app = request_app
    before = (app.vault_root / "note.md").read_bytes()
    result = await _append_journal_impl(
        app, rel_path="note.md", heading="Log", entry="No", request_id=key
    )
    assert result["error"]["type"] == "ValueError"
    assert (app.vault_root / "note.md").read_bytes() == before
    assert not await app.vault_writer.list_operations()


async def test_exact_cas_retry_uses_receipt_before_stale_hash_check(
    request_app: DatacronApp,
) -> None:
    app = request_app
    expected = sha256_bytes((app.vault_root / "note.md").read_bytes())
    first = await _append_journal_impl(
        app, rel_path="note.md", heading="Log", entry="CAS", expected_hash=expected, request_id=_KEY
    )
    replay = await _append_journal_impl(
        app, rel_path="note.md", heading="Log", entry="CAS", expected_hash=expected, request_id=_KEY
    )
    assert first["content_hash"] == replay["content_hash"]
    assert replay["replayed"] is True


async def test_targeted_index_does_not_read_unrelated_note(
    request_app: DatacronApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = request_app
    (app.vault_root / "broken.md").write_bytes(b"\xff")
    monkeypatch.setattr(
        app.vault_reader, "stat_notes", AsyncMock(side_effect=AssertionError("scan"))
    )
    result = await _append_journal_impl(app, rel_path="note.md", heading="Log", entry="Indexed")
    assert result["indexed"] is True
    assert await app.store.get_note_id("note.md") == _ID
    note = await _get_note_impl(app, id_or_path="note.md", fmt="full")
    assert "Indexed" in note["content"]
