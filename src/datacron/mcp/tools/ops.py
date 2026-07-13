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
"""Operational health and audit tool implementations."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from datacron.core.hashing import HASH_HEX_LENGTH
from datacron.core.logger import get_logger
from datacron.core.operation_log import (
    OperationLogError,
    OperationRecord,
)
from datacron.mcp.tools.payloads import _audit, _bounded_count, _error_response

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_LOGGER = get_logger(__name__)

GetNoteFormat = str  # "full" | "map" -- kept loose for FastMCP schema
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CHUNK_ID_SEPARATOR: Final[str] = "::"
_MEMORY_ORIGINS: Final[frozenset[str]] = frozenset({"ai", "human", "merged"})
_MEMORY_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low", "needs_verification"}
)
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
_ULID_CREATE_ATTEMPTS: Final[int] = 5


async def _get_health_impl(app: DatacronApp) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        from datacron.mcp.health import build_health  # noqa: PLC0415

        payload = await build_health(app)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return _error_response("get_health", exc, started)
    except Exception:
        _LOGGER.exception("get_health failed")
        return _error_response("get_health", RuntimeError("internal error"), started)
    _audit(
        "get_health",
        started,
        status=payload["status"],
        read_only=payload["read_only"],
        notes_count=payload["index"]["vault_notes_count"],
        stale_entries=payload["index"]["stale_entries"],
    )
    return payload


async def _get_note_history_impl(
    app: DatacronApp,
    *,
    note: str,
    limit: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    cleaned_note = note.strip()
    if not cleaned_note:
        return _error_response(
            "get_note_history",
            ValueError("note must not be empty"),
            started,
            note=note,
        )
    bounded_limit = _bounded_count(limit, app.settings.max_result_count)
    try:
        records = await app.vault_writer.list_operations()
    except OperationLogError as exc:
        return _error_response("get_note_history", exc, started, note=cleaned_note)
    matching = [record for record in records if cleaned_note in (record.rel_path, record.note_id)]
    returned = matching[-bounded_limit:]
    payload = {
        "note": cleaned_note,
        "operations": [_operation_payload(record) for record in returned],
        "total": len(matching),
        "returned": len(returned),
        "limit_applied": bounded_limit,
        "truncated": len(returned) < len(matching),
    }
    _audit(
        "get_note_history",
        started,
        note=cleaned_note,
        total=len(matching),
        returned=len(returned),
    )
    return payload


async def _audit_query_impl(
    app: DatacronApp,
    *,
    start: str | None,
    end: str | None,
    tool: str | None,
    note: str | None,
    limit: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        start_time = _parse_audit_time(start, field="start")
        end_time = _parse_audit_time(end, field="end")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start must be before or equal to end")
        bounded_limit = _bounded_count(limit, app.settings.max_result_count)
        records = await app.vault_writer.list_operations()
    except (OperationLogError, ValueError) as exc:
        return _error_response("audit_query", exc, started)

    cleaned_tool = tool.strip() if tool else None
    cleaned_note = note.strip() if note else None
    matching: list[OperationRecord] = []
    for record in records:
        timestamp = datetime.fromisoformat(record.timestamp).astimezone(UTC)
        if start_time is not None and timestamp < start_time:
            continue
        if end_time is not None and timestamp > end_time:
            continue
        if cleaned_tool and record.tool != cleaned_tool:
            continue
        if cleaned_note and cleaned_note not in {record.note_id, record.rel_path}:
            continue
        matching.append(record)
    returned = matching[-bounded_limit:]
    payload = {
        "filters": {
            "start": start,
            "end": end,
            "tool": cleaned_tool,
            "note": cleaned_note,
        },
        "operations": [_operation_payload(record) for record in returned],
        "total": len(matching),
        "returned": len(returned),
        "limit_applied": bounded_limit,
        "truncated": len(returned) < len(matching),
    }
    _audit(
        "audit_query",
        started,
        filter_tool=cleaned_tool,
        note=cleaned_note,
        total=len(matching),
        returned=len(returned),
    )
    return payload


def _operation_payload(record: OperationRecord) -> dict[str, object]:
    return record.to_dict()


def _parse_audit_time(value: str | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    cleaned = value.strip()
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)
