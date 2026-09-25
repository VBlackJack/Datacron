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
"""Error paths of ``get_write_progress`` that the daily workflows do not exercise."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import DEFAULT_MAX_RESULT_TOKENS, Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import HASH_HEX_LENGTH, sha256_bytes
from datacron.core.memory_protocol import FOLLOW_UP_MAX_RECORDS
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools.write import _append_journal_impl, _revert_note_impl
from datacron.mcp.tools.write_progress import (
    WriteReference,
    _inspect_target,
    _resolve_target,
    get_write_progress,
)

_NOTE = "note.md"
_STALE_HASH = "0" * HASH_HEX_LENGTH


def _settings(root: Path, max_result_tokens: int = DEFAULT_MAX_RESULT_TOKENS) -> Settings:
    return Settings(
        vault_root=root,
        read_paths=[root],
        write_paths=[root],
        session_context_paths=[],
        log_dir=root / "logs",
        repair_min_interval_seconds=0,
        max_result_tokens=max_result_tokens,
    )


async def _open_app(root: Path, max_result_tokens: int = DEFAULT_MAX_RESULT_TOKENS) -> DatacronApp:
    (root / _NOTE).write_text(
        serialize({"title": "Note"}, "# Note\n\n## Journal\n"), encoding="utf-8"
    )
    app = build_app(settings=_settings(root, max_result_tokens), vault_root=root)
    await app.store.open(sidecar_index_db(root))
    return app


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    opened = await _open_app(tmp_path)
    try:
        yield opened
    finally:
        await opened.store.close()


@pytest.fixture
async def tiny_budget_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    opened = await _open_app(tmp_path, max_result_tokens=1)
    try:
        yield opened
    finally:
        await opened.store.close()


def _references(count: int) -> list[WriteReference]:
    return [WriteReference(note=_NOTE, request_id=f"request-{index}") for index in range(count)]


@pytest.mark.parametrize("count", [0, FOLLOW_UP_MAX_RECORDS + 1])
async def test_reference_count_outside_bounds_is_refused(app: DatacronApp, count: int) -> None:
    result = await get_write_progress(app, _references(count))

    assert result["error"]["message"] == "request count exceeds bounds"
    assert "items" not in result


async def test_reference_count_at_the_upper_bound_is_accepted(app: DatacronApp) -> None:
    result = await get_write_progress(app, _references(FOLLOW_UP_MAX_RECORDS))

    assert result["total"] == FOLLOW_UP_MAX_RECORDS
    assert result["counts"] == {"not_recorded": FOLLOW_UP_MAX_RECORDS}


async def test_duplicate_reference_is_refused(app: DatacronApp) -> None:
    duplicate = WriteReference(note=_NOTE, request_id="same")

    result = await get_write_progress(app, [duplicate, duplicate])

    assert result["error"]["message"] == "duplicate request reference"
    assert "items" not in result


async def test_same_request_id_on_two_targets_is_not_a_duplicate(
    app: DatacronApp, tmp_path: Path
) -> None:
    (tmp_path / "other.md").write_text(serialize({"title": "Other"}, "# Other\n"), encoding="utf-8")

    result = await get_write_progress(
        app,
        [
            WriteReference(note=_NOTE, request_id="same"),
            WriteReference(note="other.md", request_id="same"),
        ],
    )

    assert result["total"] == 2


async def test_output_over_budget_is_refused_without_items(tiny_budget_app: DatacronApp) -> None:
    result = await get_write_progress(tiny_budget_app, _references(1))

    assert result["error"]["message"] == "output budget exceeded; submit fewer references"
    assert "items" not in result


async def test_target_removed_between_admission_and_read_is_reported(
    app: DatacronApp, tmp_path: Path
) -> None:
    target = await _resolve_target(app, _NOTE)
    (tmp_path / _NOTE).unlink()

    item = await _inspect_target(app, target, None, None, {})

    assert item["status"] == "target_unavailable"
    assert item["next_action"] == "inspect_target_before_retry"
    assert item["committed"] is None
    assert item["indexed"] is None
    assert "current_hash" not in item


async def test_expected_hash_mismatch_without_receipt_is_a_conflict(app: DatacronApp) -> None:
    target = await _resolve_target(app, _NOTE)
    indexed = await app.store.list_indexed_notes_with_mtime()

    item = await _inspect_target(app, target, _STALE_HASH, None, indexed)

    assert item["status"] == "conflict"
    assert item["next_action"] == "inspect_original_request_then_reprepare"
    assert item["committed"] is None
    assert item["current_hash"] != _STALE_HASH


async def test_matching_expected_hash_without_receipt_is_not_recorded(
    app: DatacronApp, tmp_path: Path
) -> None:
    target = await _resolve_target(app, _NOTE)
    note = await app.vault_reader.read_note(tmp_path / _NOTE)

    item = await _inspect_target(app, target, note.content_hash, None, {})

    assert item["status"] == "not_recorded"
    assert item["indexed"] is False


async def _append_then_revert(
    app: DatacronApp, root: Path, request_id: str, *, with_expected_hash: bool
) -> tuple[str, dict[str, Any]]:
    """Commit one keyed append, revert it, and return the reverted hash and its kwargs."""
    before_hash = sha256_bytes((root / _NOTE).read_bytes())
    arguments: dict[str, Any] = {
        "rel_path": _NOTE,
        "heading": "Journal",
        "entry": f"- entry for {request_id}",
        "request_id": request_id,
    }
    if with_expected_hash:
        arguments["expected_hash"] = before_hash
    written = await _append_journal_impl(app, **arguments)
    assert written.get("committed") is True, written
    reverted = await _revert_note_impl(
        app,
        note=_NOTE,
        to_hash=before_hash,
        expected_hash=sha256_bytes((root / _NOTE).read_bytes()),
    )
    assert "error" not in reverted, reverted
    assert sha256_bytes((root / _NOTE).read_bytes()) == before_hash
    return before_hash, arguments


async def test_reverted_write_without_expected_hash_is_told_to_use_a_new_request_id(
    app: DatacronApp, tmp_path: Path
) -> None:
    """The guidance for a reverted keyed write has to be one that can succeed.

    The request fingerprint includes ``expected_hash``. A write first made without
    one therefore cannot be replayed with one: the writer calls that a different
    set of arguments. Both the writer and ``get_write_progress`` used to send the
    caller exactly there, and every path they offered was refused.
    """
    before_hash, arguments = await _append_then_revert(
        app, tmp_path, "req-no-hash", with_expected_hash=False
    )

    progress = await get_write_progress(app, [WriteReference(note=_NOTE, request_id="req-no-hash")])
    item = progress["items"][0]
    assert item["status"] == "committed_reverted"
    assert item["next_action"] == "retry_with_new_request_id_and_expected_hash"

    refused = await _append_journal_impl(app, **arguments)
    assert "new request_id" in refused["error"]["message"]

    retried = await _append_journal_impl(
        app, **{**arguments, "request_id": "req-no-hash-2", "expected_hash": before_hash}
    )
    assert retried.get("committed") is True, retried
    assert arguments["entry"] in (tmp_path / _NOTE).read_text(encoding="utf-8")


async def test_reverted_write_with_expected_hash_replays_its_identical_arguments(
    app: DatacronApp, tmp_path: Path
) -> None:
    before_hash, arguments = await _append_then_revert(
        app, tmp_path, "req-hash", with_expected_hash=True
    )

    progress = await get_write_progress(
        app, [WriteReference(note=_NOTE, request_id="req-hash", expected_hash=before_hash)]
    )
    item = progress["items"][0]
    assert item["status"] == "committed_reverted"
    assert item["next_action"] == "replay_identical_arguments_with_expected_hash"

    replayed = await _append_journal_impl(app, **arguments)
    assert replayed.get("committed") is True, replayed
    assert replayed.get("replayed") is False
    assert arguments["entry"] in (tmp_path / _NOTE).read_text(encoding="utf-8")
