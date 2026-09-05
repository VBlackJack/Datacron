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
from typing import TYPE_CHECKING, Any

from datacron.core.memory_protocol import FOLLOW_UP_MARKER_PREFIX, SESSION_MAX_NOTES
from datacron.core.models import Note
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError
from datacron.mcp.sandbox import sanitize_payload_strings, wrap_vault_content
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


def follow_up_entries(note: Note) -> list[dict[str, Any]]:
    """Validate envelopes and revision chains before using persisted tracking data."""
    text = note.content.replace("\r\n", "\n")
    entries: list[dict[str, Any]] = []
    latest: dict[str, str] = {}
    for match in _ENTRY.finditer(text):
        body = match["body"]
        if sha256(body.encode()).hexdigest() != match["digest"]:
            raise ValueError("follow-up entry digest mismatch")
        item = json.loads(body)
        if not isinstance(item, dict) or not isinstance(item.get("record_id"), str):
            raise ValueError("invalid follow-up record")
        key = sha256(f"{note.id}:{item['record_id']}".encode()).hexdigest()
        if (
            key != match["key"]
            or item.get("target_id") != note.id
            or item.get("revision") != match["revision"]
            or item.get("previous_revision") != latest.get(key)
        ):
            raise ValueError("invalid follow-up identity or revision chain")
        latest[key] = match["revision"]
        entries.append(item)
    marker_count = len(
        re.findall(r"^<!-- " + re.escape(FOLLOW_UP_MARKER_PREFIX), text, re.MULTILINE)
    )
    if marker_count != len(entries):
        raise ValueError("malformed follow-up envelope")
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
    if not note_paths or len(note_paths) > SESSION_MAX_NOTES or offset < 0:
        return _error_response("get_follow_up", ValueError("note count exceeds bounds"), started)
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
                safe = sanitize_payload_strings(item)
                # Hash integrity does not make historical text trusted. Reframe even
                # legacy envelopes, neutralizing any nested control delimiters.
                for field in ("summary", "source_excerpt", "identity_basis"):
                    if isinstance(item.get(field), str):
                        safe[field] = wrap_vault_content(note.rel_path, item[field])
                safe = app.secret_redactor.redact_value(safe)
                records.append(
                    {
                        "record": safe,
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
        output = _page(records, legacy, offset, snapshot, app.settings.max_result_tokens * 4)
        _audit("get_follow_up", started, returned=output["returned"], truncated=output["truncated"])
        return output
    except FollowUpReadError as exc:
        return _error_response("get_follow_up", exc, started)
    except (ValueError, FileNotFoundError, NoteAdmissionError, PathConfinementError):
        return _error_response(
            "get_follow_up", ValueError("follow-up source unavailable or invalid"), started
        )
    except Exception:
        return _internal_error_response("get_follow_up", started)


class FollowUpReadError(ValueError):
    """An actionable failure of a bounded, snapshot-bound follow-up read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _page(
    records: list[dict[str, Any]], legacy: int, offset: int, snapshot: str, maximum: int
) -> dict[str, Any]:
    if offset > len(records):
        raise ValueError("follow-up offset exceeds total")
    page = records[offset:]
    output: dict[str, Any] = {
        "records": page,
        "returned": len(page),
        "total": len(records),
        "offset": offset,
        "next_offset": None,
        "snapshot_hash": snapshot,
        "legacy_notes": legacy,
        "coverage": "explicit_notes_structured_entries_only",
        "truncated": offset > 0,
        "omitted": 0,
    }
    while rendered_size(output) > maximum and page:
        page.pop()
        output.update(
            returned=len(page),
            truncated=True,
            omitted=len(records) - offset - len(page),
            next_offset=offset + len(page),
        )
    if rendered_size(output) > maximum or (not page and offset < len(records)):
        raise FollowUpReadError(
            "follow_up_record_too_large",
            "A follow-up record cannot fit; increase DATACRON_MAX_RESULT_TOKENS or use get_note",
        )
    return output
