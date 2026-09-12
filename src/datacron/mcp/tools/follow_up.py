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
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

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
from datacron.mcp.sandbox import sanitize_metadata_value, wrap_vault_content
from datacron.mcp.tools.follow_up_read import follow_up_entries
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.mcp.tools.session import rendered_size

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_HASH = r"^[0-9a-f]{64}$"
_ULID = r"^[0-9A-HJKMNP-TV-Z]{26}$"
_HEADING_MAX_LENGTH: Final[int] = 256
_OWNER_MAX_LENGTH: Final[int] = 256
_IDENTITY_BASIS_MAX_LENGTH: Final[int] = 1000
_TEXT_MIN_LENGTH: Final[int] = 1
_TEXT = Annotated[str, Field(min_length=_TEXT_MIN_LENGTH, max_length=FOLLOW_UP_MAX_TEXT)]


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
    heading: str = Field(min_length=_TEXT_MIN_LENGTH, max_length=_HEADING_MAX_LENGTH)
    source_path: _TEXT
    source_hash: str = Field(pattern=_HASH)
    source_excerpt: _TEXT
    summary: _TEXT
    event_date: date | None = None
    owner: str | None = Field(default=None, max_length=_OWNER_MAX_LENGTH)
    due_date: date | None = None
    status: Literal[
        "unknown", "proposed", "open", "in_progress", "waiting", "completed", "cancelled"
    ] = "unknown"
    identity_confirmed: bool = False
    identity_basis: str | None = Field(default=None, max_length=_IDENTITY_BASIS_MAX_LENGTH)


_RENDERED_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {"pattern", "enum", "minLength", "maxLength", "format", "type", "title", "description"}
)
_PARENT_ONLY_SCHEMA_KEYS: Final[frozenset[str]] = frozenset({"anyOf", "default"})
_RENDERED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {"additionalProperties", "description", "properties", "required", "title", "type"}
)
_RENDERED_FORMATS: Final[frozenset[str]] = frozenset({"date"})
_RENDERED_ENUM_MEMBER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_RENDERED_PROPERTY_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CLAUSE_SEPARATORS: Final[tuple[str, ...]] = ("; ", ", ", ": ")
_RENDERED_TYPES: Final[frozenset[str]] = frozenset({"string", "boolean", "null"})


def _checked_variants(name: str, field_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the schema variants of one property, refusing keywords the clause cannot express.

    A constraint added to the model must never be silently missing from the description.
    """
    variants = field_schema.get("anyOf")
    if variants is None:
        unknown = set(field_schema) - _RENDERED_SCHEMA_KEYS - _PARENT_ONLY_SCHEMA_KEYS
        if unknown:
            raise ValueError(f"{name}: schema keywords not rendered: {sorted(unknown)}")
        return [field_schema]
    beside = set(field_schema) - _PARENT_ONLY_SCHEMA_KEYS - {"title", "description"}
    if beside:
        raise ValueError(f"{name}: constraints beside anyOf are not rendered: {sorted(beside)}")
    for variant in variants:
        unknown = set(variant) - _RENDERED_SCHEMA_KEYS
        if unknown:
            raise ValueError(f"{name}: schema keywords not rendered: {sorted(unknown)}")
    return list(variants)


def _check_variant_values(name: str, variant: dict[str, Any]) -> None:
    """Refuse a format or type value the clause does not express."""
    format_value = variant.get("format", "date")
    if not isinstance(format_value, str) or format_value not in _RENDERED_FORMATS:
        raise ValueError(f"{name}: schema format not rendered: {format_value!r}")
    type_value = variant.get("type")
    if not isinstance(type_value, str) or type_value not in _RENDERED_TYPES:
        raise ValueError(f"{name}: schema type not rendered: {type_value!r}")
    for member in variant.get("enum", ()):
        if not isinstance(member, str) or not _RENDERED_ENUM_MEMBER.fullmatch(member):
            raise ValueError(f"{name}: enum member not rendered unambiguously: {member!r}")
    pattern = variant.get("pattern", "")
    if not isinstance(pattern, str) or any(sep in pattern for sep in _CLAUSE_SEPARATORS):
        raise ValueError(f"{name}: pattern not rendered unambiguously: {pattern!r}")


def _length_clause(variant: dict[str, Any]) -> str | None:
    """Render minLength and maxLength of one variant, or None when it has neither."""
    minimum = variant.get("minLength")
    maximum = variant.get("maxLength")
    if minimum is not None and maximum is not None:
        return f"{minimum} to {maximum} characters"
    if maximum is not None:
        return f"at most {maximum} characters"
    if minimum is not None:
        return f"at least {minimum} characters"
    return None


def _variant_parts(variant: dict[str, Any]) -> list[str]:
    """Render the constraints of one non-null variant."""
    parts: list[str] = []
    if "pattern" in variant:
        parts.append(f"pattern {variant['pattern']}")
    if "enum" in variant:
        parts.append("one of " + ", ".join(variant["enum"]))
    length = _length_clause(variant)
    if length is not None:
        parts.append(length)
    if variant.get("format") == "date":
        parts.append("ISO date")
    if variant.get("type") == "boolean":
        parts.append("boolean")
    return parts


def _constraint_clause(name: str, field_schema: dict[str, Any]) -> str:
    """Render one property of a JSON schema as a short, client-independent clause."""
    if not _RENDERED_PROPERTY_NAME.fullmatch(name):
        raise ValueError(f"property name not rendered unambiguously: {name!r}")
    parts: list[str] = []
    nullable = False
    for variant in _checked_variants(name, field_schema):
        _check_variant_values(name, variant)
        if variant.get("type") == "null":
            if set(variant) != {"type"}:
                raise ValueError(f"{name}: constraints on the null variant are not rendered")
            nullable = True
            continue
        variant_parts = _variant_parts(variant)
        if not variant_parts:
            raise ValueError(f"{name}: an unconstrained variant widens the schema silently")
        if parts:
            raise ValueError(f"{name}: alternatives between constrained variants are not rendered")
        parts.extend(variant_parts)
    if nullable:
        parts.append("or null")
    if "default" in field_schema:
        parts.append(f"default {json.dumps(field_schema['default'])}")
    return f"{name}: " + ", ".join(parts)


def render_follow_up_constraints(record_schema: dict[str, Any]) -> str:
    """Repeat every constraint of the record schema in prose, clause by clause.

    Some MCP clients present the tool schema without its $defs, patterns or bounds. The
    rendered text lets a model that only reads the description build a record the server
    accepts. Clauses are separated by "; " and each starts with the property name.
    """
    unknown = set(record_schema) - _RENDERED_RECORD_KEYS
    if unknown:
        raise ValueError(f"record schema keywords not rendered: {sorted(unknown)}")
    if record_schema.get("type", "object") != "object":
        raise ValueError(f"record schema type not rendered: {record_schema['type']!r}")
    extra = record_schema.get("additionalProperties", True)
    if not isinstance(extra, bool):
        raise ValueError("record schema additionalProperties sub-schema not rendered")
    clauses = [
        "required: " + ", ".join(record_schema["required"]),
        "extra fields ignored" if extra else "extra fields refused",
        f"at most {FOLLOW_UP_MAX_RECORDS} records per call, checked at runtime",
    ]
    clauses.extend(
        _constraint_clause(name, field_schema)
        for name, field_schema in record_schema["properties"].items()
    )
    return "Schema constraints, repeated because some clients strip them: " + "; ".join(clauses)


FOLLOW_UP_CONSTRAINTS_DESCRIPTION: Final[str] = render_follow_up_constraints(
    FollowUpRecord.model_json_schema()
)


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
    legacy = dict(raw)
    legacy["identity_basis"] = (
        wrap_vault_content(target.rel_path, record.identity_basis)
        if record.identity_basis
        else None
    )
    legacy["source_excerpt"] = wrap_vault_content(source.rel_path, record.source_excerpt)
    legacy["summary"] = wrap_vault_content(target.rel_path, record.summary)
    # Retain replay compatibility with existing envelopes without rewriting history.
    legacy_digest = sha256(
        json.dumps(legacy, ensure_ascii=True, sort_keys=True, indent=2).encode()
    ).hexdigest()
    if record.owner is not None:
        raw["owner"] = sanitize_metadata_value(record.owner)
        legacy["owner"] = raw["owner"]
    legacy_sanitized_digest = sha256(
        json.dumps(legacy, ensure_ascii=True, sort_keys=True, indent=2).encode()
    ).hexdigest()
    raw["text_format"] = "raw"
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
        if prior[record.revision] not in {digest, legacy_digest, legacy_sanitized_digest}:
            raise FollowUpValidationError("revision already exists with different content")
        return None
    if known and record.previous_revision != known[-1][0]:
        raise FollowUpValidationError("new revision must reference the latest recorded revision")
    if not known and record.previous_revision is not None:
        raise FollowUpValidationError("previous revision is unavailable in the target note")
    # Fence length exceeds any caller-controlled backtick run.
    fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", rendered)), default=3))
    return f"{marker}\n{fence}json\n{rendered}\n{fence}"
