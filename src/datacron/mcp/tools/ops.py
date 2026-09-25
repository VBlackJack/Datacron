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

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from datacron.core.hashing import hash_text
from datacron.core.operation_log import (
    OperationLogError,
    OperationRecord,
)
from datacron.mcp.tools.payloads import (
    _audit,
    _bounded_count,
    _error_response,
    _internal_error_response,
    _redact_retrieval_text,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp
    from datacron.mcp.tool_contract import HealthDetail


async def _get_health_impl(
    app: DatacronApp,
    *,
    detail: HealthDetail = "summary",
    limit: int = 0,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        from datacron.mcp.health import build_health  # noqa: PLC0415

        payload = await build_health(app, detail=detail, limit=limit)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return _error_response("get_health", exc, started)
    except Exception:
        return _internal_error_response("get_health", started)
    audit_fields: dict[str, Any] = {
        "status": payload["status"],
        "read_only": payload["read_only"],
        "notes_count": payload["index"]["vault_notes_count"],
        "stale_entries": payload["index"]["stale_entries"],
        "detail": detail,
    }
    if detail == "full":
        findings = payload["integrity"]["findings"]
        audit_fields["returned"] = findings["returned"]
        audit_fields["truncated"] = findings["truncated"]
    _audit(
        "get_health",
        started,
        **audit_fields,
    )
    return payload


async def _get_note_history_impl(
    app: DatacronApp,
    *,
    note: str,
    limit: int,
    request_id: str | None = None,
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
    except Exception:
        # The journal is read with an unguarded read_bytes(): a real OSError used to
        # escape to the SDK, which returns str(exc) to the caller -- errno, host path
        # and user name included.
        return _internal_error_response("get_note_history", started, note=cleaned_note)
    matching = [record for record in records if cleaned_note in (record.rel_path, record.note_id)]
    if request_id is not None:
        matching = [
            record
            for record in matching
            if record.parameters.get("request_key_hash") == hash_text(request_id)
        ]
    returned = matching[-bounded_limit:]
    try:
        present = await app.vault_writer.present_history_hashes(
            record.before_hash for record in returned
        )
    except Exception:
        return _internal_error_response("get_note_history", started, note=cleaned_note)
    payload = {
        "note": cleaned_note,
        "operations": [_restore_point_payload(app, record, present) for record in returned],
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
    except Exception:
        return _internal_error_response("audit_query", started)

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
        "operations": [_operation_payload(app, record) for record in returned],
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


def _operation_payload(app: DatacronApp, record: OperationRecord) -> dict[str, object]:
    """Render one journal record, with its note path redacted like every retrieval.

    Search and backlinks redact a note path that carries a secret-shaped name, and
    the journal readers returned the same path in full, so the secret the other
    tools concealed was one audit_query away.

    Every string parameter is redacted too. Redacting the path alone left the same
    secret in ``parameters.source_rel_path``, which an organization batch records for
    every move. Keys are left alone: the server names them, a caller never does.
    """
    payload = record.to_dict()
    payload["rel_path"] = _redact_retrieval_text(app, record.rel_path)
    payload["parameters"] = {
        key: _redact_retrieval_text(app, value) if isinstance(value, str) else value
        for key, value in record.parameters.items()
    }
    return payload


def _restore_point_payload(
    app: DatacronApp,
    record: OperationRecord,
    present_hashes: set[str],
) -> dict[str, object]:
    """Add to one history record whether it is still a restore point.

    ``history_stored`` says the prior bytes were stored when the write committed,
    which is not the same as their being there now: retention deletes a version once
    it falls out of the window, and a vault switched to ``redacted`` stores nothing
    new. A caller reading ``history_stored: true`` months later would offer a revert
    that fails, which on a vault resumed after a long pause is the common case rather
    than the rare one.
    """
    payload = _operation_payload(app, record)
    payload["restore_available"] = record.before_hash in present_hashes
    return payload


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
