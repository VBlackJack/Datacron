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
"""Integrity-checked current projections of append-only follow-up revisions."""

from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from html import escape
from typing import TYPE_CHECKING, Any

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.memory_protocol import FOLLOW_UP_MARKER_PREFIX, SESSION_MAX_NOTES
from datacron.core.models import Note
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError
from datacron.mcp.sandbox import (
    VAULT_CONTENT_CLOSE,
    VAULT_CONTENT_NOTICE,
    sanitize_payload_strings,
    wrap_vault_content,
)
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_ENTRY = re.compile(
    r"^<!-- "
    + re.escape(FOLLOW_UP_MARKER_PREFIX)
    + r"(?P<key>[0-9a-f]{64}):(?P<revision>[^: ]+):(?P<digest>[0-9a-f]{64}) -->\n"
    + r"(?P<fence>`{3,})json\n(?P<body>.*?)\n(?P=fence)$",
    re.MULTILINE | re.DOTALL,
)


class FollowUpEntryError(ValueError):
    """Raised when a note's own stored follow-up bytes are malformed.

    Separate from every other refusal this subsystem makes, because it is the only
    one the caller cannot fix by re-reading and resubmitting. It was reported
    through the generic handler, whose message lists identity, source hashes,
    excerpt, heading, revision and budget - six things to re-check, and not the one
    in play. An agent told that does exactly what the memory discipline tells it to
    do on a refusal: reread the targets, refresh the hashes, prepare again. No
    amount of that changes bytes already in the note, so it loops.
    """

    code = "follow_up_entry_malformed"

    def __init__(self, reason: str, rel_path: str) -> None:
        super().__init__(
            f"{reason} in {rel_path!r}; read that note and append a correction "
            "revision, re-preparing the same plan will not change it"
        )
        self.rel_path = rel_path


def follow_up_entries(note: Note) -> list[dict[str, Any]]:
    """Validate envelopes and revision chains before using persisted tracking data."""
    text = note.content.replace("\r\n", "\n")
    entries: list[dict[str, Any]] = []
    latest: dict[str, str] = {}
    for match in _ENTRY.finditer(text):
        body = match["body"]
        if sha256(body.encode()).hexdigest() != match["digest"]:
            raise FollowUpEntryError("follow-up entry digest mismatch", note.rel_path)
        item = json.loads(body)
        if not isinstance(item, dict) or not isinstance(item.get("record_id"), str):
            raise FollowUpEntryError("invalid follow-up record", note.rel_path)
        key = sha256(f"{note.id}:{item['record_id']}".encode()).hexdigest()
        if (
            key != match["key"]
            or item.get("target_id") != note.id
            or item.get("revision") != match["revision"]
            or item.get("previous_revision") != latest.get(key)
        ):
            raise FollowUpEntryError("invalid follow-up identity or revision chain", note.rel_path)
        latest[key] = match["revision"]
        entries.append(item)
    marker_count = len(
        re.findall(r"^<!-- " + re.escape(FOLLOW_UP_MARKER_PREFIX), text, re.MULTILINE)
    )
    if marker_count != len(entries):
        raise FollowUpEntryError("malformed follow-up envelope", note.rel_path)
    return entries


async def get_follow_up(
    app: DatacronApp,
    note_paths: list[str],
    *,
    include_closed: bool = False,
    offset: int = 0,
    expected_snapshot: str | None = None,
) -> dict[str, Any]:
    """Read the latest revision per identity; disclose legacy and freshness limitations."""
    started = time.perf_counter()
    if not note_paths or len(note_paths) > SESSION_MAX_NOTES:
        return _error_response("get_follow_up", ValueError("note count exceeds bounds"), started)
    if offset < 0:
        return _error_response(
            "get_follow_up",
            FollowUpReadError(
                "follow_up_offset_invalid",
                f"offset {offset} is negative; offset must be between 0 and total",
            ),
            started,
        )
    try:
        records: list[dict[str, Any]] = []
        sources: list[tuple[str, str]] = []
        legacy = 0
        for path in dict.fromkeys(note_paths):
            note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(path))
            sources.append((note.rel_path, note.content_hash))
            entries = follow_up_entries(note)
            legacy += not entries
            current = {str(item["record_id"]): item for item in entries}
            for item in current.values():
                if not include_closed and item.get("status") in {"completed", "cancelled"}:
                    continue
                projected = _project_text(item)
                safe = sanitize_payload_strings(projected)
                if isinstance(projected.get("source_excerpt"), str):
                    safe["source_excerpt"] = wrap_vault_content(
                        str(item.get("source_path", note.rel_path)), projected["source_excerpt"]
                    )
                safe = app.secret_redactor.redact_value(safe)
                records.append(
                    {
                        "record": safe,
                        # The only path inside the record is target_path, stored
                        # verbatim from whoever prepared it and never re-checked
                        # against the note it was found on: records are matched by
                        # target_id. After a rename that preserves the ULID, which
                        # apply_organization_manifest performs by design, that
                        # stored path names a file that no longer exists. This is
                        # where the note actually is, now.
                        "note_rel_path": note.rel_path,
                        "note_content_hash": note.content_hash,
                        "source_freshness": "not_revalidated",
                    }
                )
        snapshot = sha256(json.dumps([include_closed, sources]).encode()).hexdigest()
        if (offset and expected_snapshot is None) or (
            expected_snapshot is not None and expected_snapshot != snapshot
        ):
            raise FollowUpReadError(
                "follow_up_snapshot_changed",
                "Restart pagination from offset 0; sources changed or snapshot missing",
            )
        output = _page(
            records,
            legacy,
            offset,
            snapshot,
            app.settings.max_result_tokens * TOKEN_ESTIMATE_CHARS_PER_TOKEN,
        )
        _audit("get_follow_up", started, returned=output["returned"], truncated=output["truncated"])
        return output
    except FollowUpReadError as exc:
        return _error_response("get_follow_up", exc, started)
    except (ValueError, FileNotFoundError, NoteAdmissionError, PathConfinementError) as exc:
        # A malformed stored envelope names its note. This batch reads up to
        # SESSION_MAX_NOTES of them and the generic message names none of them,
        # so the one refusal the caller cannot fix by retrying said the least.
        reported = (
            exc
            if isinstance(exc, FollowUpEntryError)
            else ValueError("follow-up source unavailable or invalid")
        )
        return _error_response("get_follow_up", reported, started)
    except Exception:
        return _internal_error_response("get_follow_up", started)


class FollowUpReadError(ValueError):
    """An actionable failure of a bounded, snapshot-bound follow-up read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _project_text(item: dict[str, Any]) -> dict[str, Any]:
    """Unframe recognized historical presentation text without changing stored bytes."""
    projected = dict(item)
    if item.get("text_format") == "raw":
        return projected
    for field in ("summary", "source_excerpt", "identity_basis"):
        value = item.get(field)
        path = item.get("source_path" if field == "source_excerpt" else "target_path")
        if not isinstance(value, str) or not isinstance(path, str):
            continue
        prefix = f'<vault_content path="{escape(path, quote=True)}">\n{VAULT_CONTENT_NOTICE}\n'
        suffix = f"\n{VAULT_CONTENT_CLOSE}"
        if value.startswith(prefix) and value.endswith(suffix):
            projected[field] = value[len(prefix) : -len(suffix)]
    return projected


def _page(
    records: list[dict[str, Any]], legacy: int, offset: int, snapshot: str, maximum: int
) -> dict[str, Any]:
    if offset > len(records):
        raise FollowUpReadError(
            "follow_up_offset_invalid",
            f"offset {offset} exceeds total {len(records)}; restart at offset 0",
        )
    available = records[offset:]

    def rendered(kept: int) -> dict[str, Any]:
        complete = kept == len(available)
        return {
            "records": available[:kept],
            "returned": kept,
            "total": len(records),
            "offset": offset,
            "next_offset": None if complete else offset + kept,
            "snapshot_hash": snapshot,
            "legacy_notes": legacy,
            "coverage": "explicit_notes_structured_entries_only",
            "truncated": offset > 0 or not complete,
            "omitted": 0 if complete else len(records) - offset - kept,
        }

    # Find the longest prefix that fits, by halving rather than by dropping one
    # record at a time. Each step re-serialises the page it is measuring, so
    # dropping one at a time cost one serialisation of the whole remaining page per
    # record dropped: quadratic in the records a note holds, which nothing bounds.
    # A note that accumulated a few hundred commitments turned a read into seconds,
    # once per page, on the synchronous path. The size grows with the records kept,
    # so the boundary this finds is the same one the loop walked to.
    low, high = 0, len(available)
    while low < high:
        middle = (low + high + 1) // 2
        if rendered_size(rendered(middle)) <= maximum:
            low = middle
        else:
            high = middle - 1
    output = rendered(low)
    if rendered_size(output) > maximum or (not output["records"] and offset < len(records)):
        raise FollowUpReadError(
            "follow_up_record_too_large",
            "A follow-up record cannot fit; increase DATACRON_MAX_RESULT_TOKENS or use get_note",
        )
    return output
