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
"""Validate sourced follow-up entries and prepare existing journal writes."""

from __future__ import annotations

import json
import re
import time
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from datacron.core.markdown_headings import markdown_headings
from datacron.core.memory_protocol import (
    FOLLOW_UP_MARKER_PREFIX,
    FOLLOW_UP_MAX_RECORDS,
    FOLLOW_UP_MAX_TEXT,
)
from datacron.core.models import Note
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError
from datacron.mcp.sandbox import wrap_vault_content
from datacron.mcp.tools.follow_up_read import follow_up_entries
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_HASH = r"^[0-9a-f]{64}$"
_ULID = r"^[0-9A-HJKMNP-TV-Z]{26}$"
_TEXT = Annotated[str, Field(min_length=1, max_length=FOLLOW_UP_MAX_TEXT)]


class FollowUpValidationError(ValueError):
    """Fixed, content-free diagnostic for a refused follow-up plan."""

    code = "follow_up_validation_failed"


class FollowUpRecord(BaseModel):
    """One explicit, source-backed revision; unknown values remain null."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    record_id: str = Field(pattern=_ID)
    revision: str = Field(pattern=_ID)
    previous_revision: str | None = Field(default=None, pattern=_ID)
    kind: Literal["action", "interaction", "decision", "objective", "project_state"]
    target_path: _TEXT
    target_id: str = Field(pattern=_ULID)
    expected_hash: str = Field(pattern=_HASH)
    heading: str = Field(min_length=1, max_length=256)
    source_path: _TEXT
    source_hash: str = Field(pattern=_HASH)
    source_excerpt: _TEXT
    summary: _TEXT
    event_date: date | None = None
    owner: str | None = Field(default=None, max_length=256)
    due_date: date | None = None
    status: Literal[
        "unknown", "proposed", "open", "in_progress", "waiting", "completed", "cancelled"
    ] = "unknown"
    identity_confirmed: bool = False
    identity_basis: str | None = Field(default=None, max_length=1000)


async def prepare_follow_up(app: DatacronApp, records: list[FollowUpRecord]) -> dict[str, Any]:
    """Produce no writes; reject stale sources and ambiguous or conflicting revisions."""
    started = time.perf_counter()
    if len(records) > FOLLOW_UP_MAX_RECORDS:
        return _error_response(
            "prepare_follow_up", ValueError("record count exceeds bounds"), started
        )
    try:
        cache: dict[str, Note] = {}
        groups: dict[str, dict[str, Any]] = {}
        already: list[str] = []
        identities: set[tuple[str, str]] = set()
        for record in records:
            target = await _read(app, cache, record.target_path)
            source = await _read(app, cache, record.source_path)
            _validate(record, target, source)
            if any(
                app.secret_redactor.redact_text(value) != value
                for value in (
                    source.content,
                    record.summary,
                    record.owner or "",
                    record.identity_basis or "",
                )
            ):
                raise FollowUpValidationError("sensitive follow-up content refused")
            identity = (target.id, record.record_id)
            if identity in identities:
                raise FollowUpValidationError("duplicate record identity in this request")
            identities.add(identity)
            entry = _render_entry(app, record, target, source)
            if entry is None:
                already.append(record.record_id)
                continue
            group = groups.setdefault(
                target.rel_path,
                {
                    "tool": "append_journal",
                    "arguments": {
                        "rel_path": target.rel_path,
                        "heading": record.heading,
                        "expected_hash": target.content_hash,
                        "entry": "",
                    },
                    "record_ids": [],
                },
            )
            if group["arguments"]["heading"] != record.heading:
                raise FollowUpValidationError(
                    "one target note must use one history heading per plan"
                )
            group["arguments"]["entry"] += ("\n\n" if group["arguments"]["entry"] else "") + entry
            group["record_ids"].append(record.record_id)
        plans = list(groups.values())
        for plan in plans:
            args = plan["arguments"]
            args["request_id"] = (
                "follow-up-" + sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
            )
        output: dict[str, Any] = {
            "status": "prepared",
            "committed": False,
            "writes_enabled": app.write_policy.effective_writes_enabled,
            "validation": "source_bytes_and_structure_not_semantic_truth",
            "plans": plans,
            "already_recorded": already,
            "next_action": (
                "Apply plans sequentially with existing writers; require indexed:true"
                " and reread each target. On uncertainty retrieve the same request "
                "receipt. Reprepare remaining plans after conflicts."
            ),
        }
        if rendered_size(output) > app.settings.max_result_tokens * 4:
            raise FollowUpValidationError(
                "follow-up plan exceeds output budget; submit fewer records"
            )
        _audit("prepare_follow_up", started, records=len(records), plans=len(plans))
        return output
    except FollowUpValidationError as exc:
        return _error_response("prepare_follow_up", exc, started)
    except (ValueError, FileNotFoundError, NoteAdmissionError, PathConfinementError):
        # Caller values and full vault text must not leak through validation messages.
        return _error_response(
            "prepare_follow_up",
            ValueError(
                "follow-up validation failed: check identity, source "
                "hashes/excerpt, history heading, revision and budget"
            ),
            started,
        )
    except Exception:
        return _internal_error_response("prepare_follow_up", started)


async def _read(app: DatacronApp, cache: dict[str, Note], path: str) -> Note:
    if path not in cache:
        cache[path] = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(path))
    return cache[path]


def _validate(record: FollowUpRecord, target: Note, source: Note) -> None:
    if target.id != record.target_id or target.content_hash != record.expected_hash:
        raise FollowUpValidationError("target identity or hash changed")
    if source.content_hash != record.source_hash or record.source_excerpt not in source.content:
        raise FollowUpValidationError("source hash or exact excerpt does not match")
    headings = [
        h
        for h in markdown_headings(target.content.splitlines(keepends=True))
        if h.text == record.heading
    ]
    if len(headings) != 1 or headings[0].level < 2:
        raise FollowUpValidationError("history heading must exist exactly once at H2-H6")
    person_related = (
        "memory/contact" in target.tags
        or record.kind == "interaction"
        or "/people/" in "/" + target.rel_path.lower()
    )
    if person_related and (
        not record.identity_confirmed or not (record.identity_basis or "").strip()
    ):
        raise FollowUpValidationError("person identity requires explicit contextual confirmation")
    if not record.summary.strip() or not record.source_excerpt.strip():
        raise FollowUpValidationError("summary and evidence must not be blank")


def _render_entry(
    app: DatacronApp, record: FollowUpRecord, target: Note, source: Note
) -> str | None:
    raw = record.model_dump(
        mode="json",
        exclude={"expected_hash", "heading"},
    )
    raw["source_id"] = source.id
    raw["identity_basis"] = (
        wrap_vault_content(target.rel_path, record.identity_basis)
        if record.identity_basis
        else None
    )
    raw["source_excerpt"] = wrap_vault_content(source.rel_path, record.source_excerpt)
    raw["summary"] = wrap_vault_content(target.rel_path, record.summary)
    rendered = json.dumps(raw, ensure_ascii=True, sort_keys=True, indent=2)
    # Prepared output is a write payload, not a retrieval snippet: refuse rather than
    # silently modify sensitive text and invalidate the source evidence.
    if app.secret_redactor.redact_text(rendered) != rendered:
        raise FollowUpValidationError("sensitive follow-up content refused")
    key = sha256(f"{target.id}:{record.record_id}".encode()).hexdigest()
    digest = sha256(rendered.encode()).hexdigest()
    marker = f"<!-- {FOLLOW_UP_MARKER_PREFIX}{key}:{record.revision}:{digest} -->"
    known = [
        (
            str(item["revision"]),
            sha256(
                json.dumps(item, ensure_ascii=True, sort_keys=True, indent=2).encode()
            ).hexdigest(),
        )
        for item in follow_up_entries(target)
        if item["record_id"] == record.record_id
    ]
    prior = dict(known)
    if record.revision in prior:
        if prior[record.revision] != digest:
            raise FollowUpValidationError("revision already exists with different content")
        return None
    if known and record.previous_revision != known[-1][0]:
        raise FollowUpValidationError("new revision must reference the latest recorded revision")
    if not known and record.previous_revision is not None:
        raise FollowUpValidationError("previous revision is unavailable in the target note")
    # Fence length exceeds any caller-controlled backtick run.
    fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", rendered)), default=3))
    return f"{marker}\n{fence}json\n{rendered}\n{fence}"
