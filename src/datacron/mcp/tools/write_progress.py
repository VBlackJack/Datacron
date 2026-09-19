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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.hashing import HASH_HEX_LENGTH, hash_text
from datacron.core.memory_protocol import FOLLOW_UP_MAX_RECORDS
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError
from datacron.mcp.tools.payloads import _error_response, _internal_error_response
from datacron.mcp.tools.read import _ULID_PATTERN, _resolve_note
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.core.operation_log import OperationRecord
    from datacron.mcp.server import DatacronApp

_NOTE_REFERENCE_MAX_CHARS: Final[int] = 1024
_REQUEST_ID_MAX_CHARS: Final[int] = 128
_REQUEST_ID_PATTERN: Final[str] = rf"^[A-Za-z0-9][A-Za-z0-9_.-]{{0,{_REQUEST_ID_MAX_CHARS - 1}}}$"
_CONTENT_HASH_PATTERN: Final[str] = rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$"


class WriteReference(BaseModel):
    """A retained request key and target, optionally with its original CAS hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    note: str = Field(
        min_length=1,
        max_length=_NOTE_REFERENCE_MAX_CHARS,
        description=(
            "Vault-relative path or note ULID of the write target, as accepted by "
            "get_note_history and revert_note."
        ),
    )
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    expected_hash: str | None = Field(default=None, pattern=_CONTENT_HASH_PATTERN)


@dataclass(frozen=True)
class _Target:
    """An admitted write target: its live path and, for a ULID reference, its identity."""

    rel_path: str
    note_id: str | None

    def matches(self, record: OperationRecord) -> bool:
        return record.rel_path == self.rel_path or (
            self.note_id is not None and record.note_id == self.note_id
        )


async def get_write_progress(app: DatacronApp, requests: list[WriteReference]) -> dict[str, Any]:
    """Inspect each admitted target independently; never replay or repair a write."""
    started = time.perf_counter()
    try:
        if not requests or len(requests) > FOLLOW_UP_MAX_RECORDS:
            raise ValueError("request count exceeds bounds")
        if len({(r.note, r.request_id) for r in requests}) != len(requests):
            raise ValueError("duplicate request reference")
        # Admit and resolve every reference before exposing any journal evidence.
        targets = [await _resolve_target(app, request.note) for request in requests]
        records = await app.vault_writer.list_operations()
        indexed = await app.store.list_indexed_notes_with_mtime()
        items = []
        for ordinal, (request, target) in enumerate(zip(requests, targets, strict=True), 1):
            receipt = next(
                (
                    r
                    for r in reversed(records)
                    if target.matches(r)
                    and r.parameters.get("request_key_hash") == hash_text(request.request_id)
                ),
                None,
            )
            item = await _inspect_target(app, target, request.expected_hash, receipt, indexed)
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
    except (NoteAdmissionError, PathConfinementError) as exc:
        return _error_response("get_write_progress", _admission_error(exc), started)
    except ValueError as exc:
        return _error_response("get_write_progress", exc, started)
    except Exception:
        return _internal_error_response("get_write_progress", started)


def _admission_error(exc: NoteAdmissionError | PathConfinementError) -> NoteAdmissionError:
    """Map a refused reference to the typed admission error, never to an internal one."""
    if isinstance(exc, NoteAdmissionError):
        return exc
    # The confinement message may name host paths; the class of failure is enough.
    return NoteAdmissionError("write target escapes the admitted vault")


async def _resolve_target(app: DatacronApp, reference: str) -> _Target:
    """Admit a path or ULID reference; a ULID resolves through the same lookup as get_note."""
    note_id: str | None = None
    rel_path = reference
    if _ULID_PATTERN.match(reference):
        note = await _resolve_note(app, reference)
        if note is None:
            raise NoteAdmissionError(f"Note identity is not a live note: {reference!r}")
        note_id, rel_path = note.id, note.rel_path
    app.scope.authorize_note_rel_path(rel_path)
    return _Target(rel_path=rel_path, note_id=note_id)


async def _inspect_target(
    app: DatacronApp,
    target: _Target,
    expected_hash: str | None,
    receipt: OperationRecord | None,
    indexed: dict[str, tuple[str, str, int | None]],
) -> dict[str, Any]:
    item: dict[str, Any] = {"committed": True if receipt else None, "indexed": None}
    if receipt:
        item.update(operation_id=receipt.operation_id, committed_hash=receipt.after_hash)
    try:
        note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(target.rel_path))
    except (OSError, ValueError, NoteAdmissionError, PathConfinementError):
        # A target admitted a moment ago can vanish before it is read: report it, never fail.
        item.update(status="target_unavailable", next_action="inspect_target_before_retry")
        return item
    item["current_hash"] = note.content_hash
    entry = indexed.get(note.rel_path)
    item["indexed"] = entry is not None and entry[:2] == (note.id, note.content_hash)
    if receipt:
        if note.content_hash == receipt.before_hash:
            # The write was undone, not overwritten. Telling the caller not to repeat it
            # would leave the note without the write and the caller believing it landed,
            # which is how a reverted follow-up entry disappears for good. The writer
            # accepts the same request id again once the call carries expected_hash.
            item.update(
                status="committed_reverted",
                next_action="replay_identical_arguments_with_expected_hash",
            )
        elif note.content_hash != receipt.after_hash:
            item.update(status="committed_changed", next_action="read_current_note_do_not_repeat")
        elif item["indexed"]:
            item.update(status="committed_current", next_action="read_current_note")
        else:
            item.update(
                status="committed_index_incomplete", next_action="repair_index_do_not_repeat"
            )
    elif expected_hash is not None and expected_hash != note.content_hash:
        item.update(status="conflict", next_action="inspect_original_request_then_reprepare")
    else:
        # A missing committed receipt does not prove there is no pending transaction.
        item.update(
            status="not_recorded", next_action="inspect_recovery_or_replay_identical_arguments"
        )
    return item
