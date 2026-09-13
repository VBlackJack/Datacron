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
"""Read-only reconciliation of a caller's multi-note write receipts."""

from __future__ import annotations

import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.hashing import hash_text
from datacron.core.memory_protocol import FOLLOW_UP_MAX_RECORDS
from datacron.mcp.tools.payloads import _error_response, _internal_error_response
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.core.operation_log import OperationRecord
    from datacron.mcp.server import DatacronApp


class WriteReference(BaseModel):
    """A retained request key and target, optionally with its original CAS hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    note: str = Field(min_length=1, max_length=1024)
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


async def get_write_progress(app: DatacronApp, requests: list[WriteReference]) -> dict[str, Any]:
    """Inspect each admitted target independently; never replay or repair a write."""
    started = time.perf_counter()
    if not requests or len(requests) > FOLLOW_UP_MAX_RECORDS:
        return _error_response(
            "get_write_progress", ValueError("request count exceeds bounds"), started
        )
    if len({(r.note, r.request_id) for r in requests}) != len(requests):
        return _error_response(
            "get_write_progress", ValueError("duplicate request reference"), started
        )
    try:
        # Authorize the entire request before exposing any journal evidence.
        for request in requests:
            app.scope.authorize_note_rel_path(request.note)
        records = await app.vault_writer.list_operations()
        indexed = await app.store.list_indexed_notes_with_mtime()
        items = []
        for ordinal, request in enumerate(requests, 1):
            receipt = next(
                (
                    r
                    for r in reversed(records)
                    if r.rel_path == request.note
                    and r.parameters.get("request_key_hash") == hash_text(request.request_id)
                ),
                None,
            )
            item = await _inspect_target(app, request, receipt, indexed)
            item["request_index"] = ordinal
            items.append(item)
        result = {
            "items": items,
            "counts": dict(Counter(item["status"] for item in items)),
            "total": len(items),
            "read_only": True,
            "evidence": "receipts_and_live_snapshots_not_multi_note_atomicity",
        }
        if rendered_size(result) > app.settings.max_result_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN:
            return _error_response(
                "get_write_progress",
                ValueError("output budget exceeded; submit fewer references"),
                started,
            )
        return result
    except ValueError as exc:
        return _error_response("get_write_progress", exc, started)
    except Exception:
        return _internal_error_response("get_write_progress", started)


async def _inspect_target(
    app: DatacronApp,
    request: WriteReference,
    receipt: OperationRecord | None,
    indexed: dict[str, tuple[str, str, int | None]],
) -> dict[str, Any]:
    item: dict[str, Any] = {"committed": True if receipt else None, "indexed": None}
    if receipt:
        item.update(operation_id=receipt.operation_id, committed_hash=receipt.after_hash)
    try:
        note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(request.note))
    except (OSError, ValueError):
        item.update(status="target_unavailable", next_action="inspect_target_before_retry")
        return item
    item["current_hash"] = note.content_hash
    entry = indexed.get(note.rel_path)
    item["indexed"] = entry is not None and entry[:2] == (note.id, note.content_hash)
    if receipt:
        if note.content_hash != receipt.after_hash:
            item.update(status="committed_changed", next_action="read_current_note_do_not_repeat")
        elif item["indexed"]:
            item.update(status="committed_current", next_action="read_current_note")
        else:
            item.update(
                status="committed_index_incomplete", next_action="repair_index_do_not_repeat"
            )
    elif request.expected_hash is not None and request.expected_hash != note.content_hash:
        item.update(status="conflict", next_action="inspect_original_request_then_reprepare")
    else:
        # A missing committed receipt does not prove there is no pending transaction.
        item.update(
            status="not_recorded", next_action="inspect_recovery_or_replay_identical_arguments"
        )
    return item
