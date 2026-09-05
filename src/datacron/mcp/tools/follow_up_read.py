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
) -> dict[str, Any]:
    """Read the latest revision per identity; disclose legacy and freshness limitations."""
    started = time.perf_counter()
    if not note_paths or len(note_paths) > SESSION_MAX_NOTES:
        return _error_response("get_follow_up", ValueError("note count exceeds bounds"), started)
    try:
        records: list[dict[str, Any]] = []
        legacy = 0
        for path in dict.fromkeys(note_paths):
            note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(path))
            entries = follow_up_entries(note)
            legacy += not entries
            current = {str(item["record_id"]): item for item in entries}
            for item in current.values():
                if not include_closed and item.get("status") in {"completed", "cancelled"}:
                    continue
                safe = app.secret_redactor.redact_value(item)
                records.append(
                    {
                        "record": safe,
                        "note_content_hash": note.content_hash,
                        "source_freshness": "not_revalidated",
                    }
                )
        output: dict[str, Any] = {
            "records": records,
            "returned": len(records),
            "legacy_notes": legacy,
            "coverage": "explicit_notes_structured_entries_only",
            "truncated": False,
            "omitted": 0,
        }
        while rendered_size(output) > app.settings.max_result_tokens * 4 and records:
            records.pop()
            output.update(returned=len(records), truncated=True, omitted=output["omitted"] + 1)
        if rendered_size(output) > app.settings.max_result_tokens * 4:
            raise ValueError("follow-up output budget too small")
        _audit("get_follow_up", started, returned=len(records), truncated=output["truncated"])
        return output
    except (ValueError, FileNotFoundError, NoteAdmissionError, PathConfinementError):
        return _error_response(
            "get_follow_up", ValueError("follow-up source unavailable or invalid"), started
        )
    except Exception:
        return _internal_error_response("get_follow_up", started)
