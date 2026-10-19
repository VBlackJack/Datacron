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
"""The three verbs of the essential tool profile: remember, recall, what_changed.

Every verb is composed from implementations that already exist: the append and
create writers, the full-text search, the note reader, the operation journal and
the subject-state helpers. Nothing here writes a file, queries the index or
ranks a result on its own, so each verb inherits the confinement, the redaction,
the sandbox envelope and the audit trail of the tool it delegates to.
"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from datacron.core.config import (
    CANONICAL_ULID_PATTERN,
    ESSENTIAL_RECALL_DEFAULT_LIMIT,
    ESSENTIAL_RECALL_EXCERPT_MAX_CHARS,
    ESSENTIAL_RECALL_MAX_LIMIT,
    ESSENTIAL_REMEMBER_CAS_RETRIES,
    ESSENTIAL_REMEMBER_CONFIDENCE,
    ESSENTIAL_REMEMBER_DEFAULT_KIND,
    ESSENTIAL_REMEMBER_ENTRY_DATE_FORMAT,
    ESSENTIAL_REMEMBER_HEADING,
    ESSENTIAL_REMEMBER_KINDS,
    ESSENTIAL_REMEMBER_MAX_CANDIDATES,
    ESSENTIAL_REMEMBER_MAX_CONTENT_CHARS,
    ESSENTIAL_REMEMBER_MAX_SOURCE_CHARS,
    ESSENTIAL_REMEMBER_ORIGIN,
    ESSENTIAL_REMEMBER_SOURCE_LABEL,
    ESSENTIAL_REMEMBER_TITLE_MAX_CHARS,
    ESSENTIAL_WHAT_CHANGED_DEFAULT_LIMIT,
    ESSENTIAL_WHAT_CHANGED_MAX_LIMIT,
    ESSENTIAL_WHAT_CHANGED_MAX_OUT_OF_BAND,
    ESSENTIAL_WHAT_CHANGED_SUMMARY_MAX_CHARS,
    OrganizationConfig,
    OrganizationRule,
)
from datacron.core.operation_log import OperationLogError, OperationRecord
from datacron.core.paths import PathConfinementError
from datacron.core.scope import NoteAdmissionError, admits_note_path
from datacron.core.write_request import ReplayedWriteError
from datacron.mcp.tools.ops import _admitted_records
from datacron.mcp.tools.payloads import (
    _audit,
    _bounded_count,
    _error_response,
    _internal_error_response,
    _redact_retrieval_text,
    _sanitize_retrieval_metadata,
)
from datacron.mcp.tools.read import (
    _get_note_impl,
    _read_note_by_rel_path,
    _resolve_note,
    _serialized_chars,
)
from datacron.mcp.tools.search import _repair_index_on_read, _search_text_impl
from datacron.mcp.tools.subject_state import load_subject_notes
from datacron.mcp.tools.write import _append_journal_impl, _create_note_ai_impl
from datacron.mcp.tools.write_requests import replayable_write
from datacron.organization.rules import matches_naming, resolve_rule
from datacron.organization.subject_state import (
    has_state_note_tag,
    matching_subject_tags,
    summarize_subject,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = [
    "_recall_impl",
    "_remember_impl",
    "_what_changed_impl",
    "register_essential_tools",
]

_TOOL_REMEMBER: Final[str] = "remember"
_TOOL_RECALL: Final[str] = "recall"
_TOOL_WHAT_CHANGED: Final[str] = "what_changed"

_MARKDOWN_SUFFIX: Final[str] = ".md"
_POSIX_SEPARATOR: Final[str] = "/"
_WINDOWS_SEPARATOR: Final[str] = "\\"
_HEADING_TRAIL_SEPARATOR: Final[str] = " / "
_ENTRY_CONTINUATION_INDENT: Final[str] = "    "
_TITLE_LEADING_MARKERS: Final[re.Pattern[str]] = re.compile(r"^[#>*\-\s]+")
_NON_SLUG: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d+)\s*([dh])")
_DURATION_UNITS: Final[dict[str, str]] = {"d": "days", "h": "hours"}
_CAS_CONFLICT_ERROR: Final[str] = "WriteConflictError"
_FILE_EXISTS_ERROR: Final[str] = "FileExistsError"
_NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000

_REASON_NO_JOURNAL_RECORD: Final[str] = "no_journal_record"
_REASON_HASH_DIFFERS: Final[str] = "hash_differs_from_last_journaled_write"
_COVERAGE_STATEMENT: Final[str] = (
    "out_of_band lists notes the index holds with a modification time at or after "
    "'since' whose bytes differ from the last journaled write, or that were never "
    "journaled. It reflects the index as of the last reconcile, which ran just "
    "before this call only when index_refreshed is true; an edit made after that "
    "reconcile, or on a read-only server, is not visible here."
)

_REFUSAL_NO_ORGANIZATION: Final[str] = (
    "remember needs the vault's organization rules to place a new note: declare "
    "'organization' with a placement rule per kind and a tag policy in "
    ".datacron/VAULT.yaml, or pass 'about' as the path or id of an existing note"
)
_REFUSAL_NO_TAG_POLICY: Final[str] = (
    "remember needs organization.tags with a placement_namespace to name the "
    "placement tag of a new note; declare it in .datacron/VAULT.yaml"
)
_REFUSAL_EMPTY_CONTENT: Final[str] = "content must not be empty"

_DEFAULT_ACTOR: Final[str] = "direct-call"

_SUMMARY_BY_OPERATION: Final[dict[str, str]] = {
    "create": "note created",
    "append": "entry appended",
    "set_frontmatter": "frontmatter changed",
    "patch_preamble": "preamble patched",
    "patch_section": "section patched",
    "rename_section": "section renamed",
    "delete_section": "section deleted",
    "move_section": "section moved",
    "revert": "note reverted",
    "apply_organization_manifest": "organization manifest applied",
}
_HEADING_PARAMETER: Final[str] = "heading"

_REMEMBER_DESCRIPTION: Final[str] = (
    "Save one durable fact, decision or preference. 'about' names where it belongs: "
    "a note path, a note id, or a registered subject (tag, last segment or alias); "
    "it is appended, dated, to that note, or to the subject's state note. An "
    "ambiguous or unknown 'about' is refused with the candidates, never guessed. "
    "Without 'about' a new note is created in the folder the vault's organization "
    "rules declare for the kind. Confined to DATACRON_WRITE_PATHS. Send request_id to "
    "make a retry safe: an identical retry replays the first receipt. The receipt "
    "carries indexed=true once the note is searchable."
)
_RECALL_DESCRIPTION: Final[str] = (
    "Find what the vault already knows about a question, before answering or "
    "asking. Returns a few ranked notes, one excerpt each, sandbox-wrapped and to be "
    "read as data. 'scope' narrows to a folder or a registered subject. Each hit "
    "carries read_more, the exact get_note arguments for the full section."
)
_WHAT_CHANGED_DESCRIPTION: Final[str] = (
    "Check what was written since a point in time: an ISO-8601 date or timestamp, or "
    "a duration such as 7d or 12h. Lists committed writes grouped per note, newest "
    "first, with their receipts and a content-free summary, plus out_of_band: notes "
    "edited outside Datacron. 'scope' narrows to a folder or a registered subject."
)


class EssentialRefusalError(ValueError):
    """A request an essential verb declines, with the reason the caller can act on."""


def _today() -> str:
    return datetime.now(tz=UTC).strftime(ESSENTIAL_REMEMBER_ENTRY_DATE_FORMAT)


def _slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_text.lower()).strip("-")


def _derive_title(content: str, kind: str) -> str:
    for line in content.splitlines():
        candidate = _TITLE_LEADING_MARKERS.sub("", line).strip()
        if candidate:
            return candidate[:ESSENTIAL_REMEMBER_TITLE_MAX_CHARS].rstrip()
    return kind


def _format_entry(content: str, kind: str, source: str | None, today: str) -> str:
    lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
    entry = [f"- {today} ({kind}): {lines[0]}"]
    entry.extend(f"{_ENTRY_CONTINUATION_INDENT}{line}" for line in lines[1:])
    if source:
        entry.append(f"{_ENTRY_CONTINUATION_INDENT}{ESSENTIAL_REMEMBER_SOURCE_LABEL}: {source}")
    return "\n".join(entry)


def _format_body(title: str, content: str, source: str | None) -> str:
    parts = [f"# {title}", content.strip()]
    if source:
        parts.append(f"{ESSENTIAL_REMEMBER_SOURCE_LABEL}: {source}")
    return "\n\n".join(parts) + "\n"


def _render_stem(naming: str, slug: str, today: str) -> str:
    return naming.replace("{slug}", slug).replace("{date}", today).replace("{iso_date}", today)


def _candidate_lines(candidates: list[str]) -> str:
    shown = candidates[:ESSENTIAL_REMEMBER_MAX_CANDIDATES]
    omitted = len(candidates) - len(shown)
    suffix = f" (and {omitted} more)" if omitted else ""
    return ", ".join(shown) + suffix


def _ambiguous_scope(scope: str, matches: list[str]) -> EssentialRefusalError:
    return EssentialRefusalError(
        f"scope {scope!r} is ambiguous; it matches the subjects {_candidate_lines(matches)}"
    )


def _looks_like_note_reference(about: str) -> bool:
    return bool(
        CANONICAL_ULID_PATTERN.match(about)
        or about.lower().endswith(_MARKDOWN_SUFFIX)
        or _POSIX_SEPARATOR in about
        or _WINDOWS_SEPARATOR in about
    )


def _is_explicit_note_reference(about: str) -> bool:
    return bool(CANONICAL_ULID_PATTERN.match(about) or about.lower().endswith(_MARKDOWN_SUFFIX))


def _placement_tag(organization: OrganizationConfig, kind: str) -> str:
    """Return the declared placement tag of ``kind`` or refuse the creation."""
    policy = organization.tags
    if policy is None:
        raise EssentialRefusalError(_REFUSAL_NO_TAG_POLICY)
    namespace = policy.placement_namespace
    declared = {rule.tag for rule in organization.rules}
    allowed = [
        candidate
        for candidate in ESSENTIAL_REMEMBER_KINDS
        if f"{namespace}/{candidate}" in declared
        and not has_state_note_tag([f"{namespace}/{candidate}"])
    ]
    tag = f"{namespace}/{kind}"
    if has_state_note_tag([tag]) or tag not in declared:
        raise EssentialRefusalError(
            f"no organization rule is declared for kind {kind!r} (tag {tag!r}); "
            f"kinds this vault allows: {', '.join(allowed) if allowed else 'none'}"
        )
    return tag


def _validate_remember(content: str, kind: str, source: str | None) -> tuple[str, str | None]:
    cleaned = content.strip() if isinstance(content, str) else ""
    if not cleaned:
        raise EssentialRefusalError(_REFUSAL_EMPTY_CONTENT)
    if len(cleaned) > ESSENTIAL_REMEMBER_MAX_CONTENT_CHARS:
        raise EssentialRefusalError(
            f"content must be at most {ESSENTIAL_REMEMBER_MAX_CONTENT_CHARS} characters"
        )
    if kind not in ESSENTIAL_REMEMBER_KINDS:
        raise EssentialRefusalError(f"kind must be one of {list(ESSENTIAL_REMEMBER_KINDS)}")
    cleaned_source = source.strip() if isinstance(source, str) and source.strip() else None
    if cleaned_source is not None and len(cleaned_source) > ESSENTIAL_REMEMBER_MAX_SOURCE_CHARS:
        raise EssentialRefusalError(
            f"source must be at most {ESSENTIAL_REMEMBER_MAX_SOURCE_CHARS} characters"
        )
    if cleaned_source is not None and ("\n" in cleaned_source or "\r" in cleaned_source):
        raise EssentialRefusalError("source must be a single line")
    return cleaned, cleaned_source


async def _existing_note_path(app: DatacronApp, about: str) -> str | None:
    """Return the vault-relative path of the note ``about`` names as a path or id."""
    if not _looks_like_note_reference(about):
        return None
    explicit = _is_explicit_note_reference(about)
    try:
        if CANONICAL_ULID_PATTERN.match(about):
            note = await _resolve_note(app, about)
            return None if note is None else note.rel_path
        note = await _read_note_by_rel_path(app, about)
    except (NoteAdmissionError, PathConfinementError) as exc:
        if explicit:
            raise EssentialRefusalError(str(exc)) from exc
        return None
    except (FileNotFoundError, ValueError):
        return None
    return note.rel_path


class _Plan:
    """Where a memory goes: an existing note to append to, or a note to create."""

    def __init__(
        self,
        *,
        append_to: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.append_to = append_to
        self.tags = tags or []


async def _plan_remember(app: DatacronApp, about: str | None, kind: str) -> _Plan:
    reference = about.strip() if isinstance(about, str) else ""
    if reference:
        existing = await _existing_note_path(app, reference)
        if existing is not None:
            return _Plan(append_to=existing)
    organization = app.organization
    if organization is None or not organization.rules:
        raise EssentialRefusalError(_REFUSAL_NO_ORGANIZATION)
    placement = _placement_tag(organization, kind)
    if not reference:
        return _Plan(tags=[placement])
    matches = matching_subject_tags(reference, organization)
    if len(matches) > 1:
        raise EssentialRefusalError(
            f"about {reference!r} is ambiguous; it matches the subjects "
            f"{_candidate_lines(list(matches))}. Repeat with one exact subject tag or a note path"
        )
    if not matches:
        known = (
            [subject.tag for subject in organization.tags.subjects]
            if organization.tags is not None
            else []
        )
        hint = f"; registered subjects: {_candidate_lines(known)}" if known else ""
        raise EssentialRefusalError(
            f"about {reference!r} names no note and no registered subject{hint}. "
            "Repeat with an existing note path or id, a registered subject, or omit 'about'"
        )
    subject_tag = matches[0]
    notes = await load_subject_notes(app, subject_tag)
    state_notes = [item.rel_path for item in summarize_subject(subject_tag, notes).state_notes]
    if len(state_notes) > 1:
        raise EssentialRefusalError(
            f"subject {subject_tag!r} has several state notes: {_candidate_lines(state_notes)}. "
            "Repeat with the path of the one to extend"
        )
    if state_notes:
        return _Plan(append_to=state_notes[0])
    return _Plan(tags=[placement, subject_tag])


async def _latest_operation_id(app: DatacronApp, rel_path: str, content_hash: str) -> str | None:
    try:
        records = await app.vault_writer.list_operations()
    except Exception:
        return None
    for record in reversed(records):
        if record.rel_path == rel_path and record.after_hash == content_hash:
            return record.operation_id
    return None


def _failed(result: dict[str, Any]) -> bool:
    return "error" in result


async def _append_with_retry(
    app: DatacronApp, rel_path: str, entry: str, actor: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attempt in range(ESSENTIAL_REMEMBER_CAS_RETRIES + 1):
        try:
            current = await _read_note_by_rel_path(app, rel_path)
        except (NoteAdmissionError, PathConfinementError, FileNotFoundError, ValueError) as exc:
            raise EssentialRefusalError(str(exc)) from exc
        result = await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading=ESSENTIAL_REMEMBER_HEADING,
            entry=entry,
            expected_hash=current.content_hash,
            actor=actor,
            request_id=None,
        )
        conflicted = _failed(result) and result["error"].get("type") == _CAS_CONFLICT_ERROR
        if not conflicted or attempt == ESSENTIAL_REMEMBER_CAS_RETRIES:
            return result
    return result


async def _create_for_remember(
    app: DatacronApp,
    *,
    tags: list[str],
    content: str,
    kind: str,
    source: str | None,
    actor: str,
) -> dict[str, Any]:
    organization = app.organization
    rule: OrganizationRule | None = resolve_rule(tags, organization)
    if rule is None:
        raise EssentialRefusalError(f"no organization rule governs the tags {tags}")
    today = _today()
    title = _derive_title(content, kind)
    stem = _render_stem(rule.naming, _slugify(title) or kind, today)
    if not matches_naming(stem, rule.naming, calendar_date=today):
        raise EssentialRefusalError(
            f"cannot build a file name that satisfies the naming {rule.naming!r} of the "
            f"rule for {rule.tag!r}"
        )
    rel_path = f"{rule.folder}/{stem}{_MARKDOWN_SUFFIX}"
    result = await _create_note_ai_impl(
        app,
        rel_path=rel_path,
        title=title,
        body=_format_body(title, content, source),
        origin=ESSENTIAL_REMEMBER_ORIGIN,
        confidence=ESSENTIAL_REMEMBER_CONFIDENCE,
        tags=tags,
        actor=actor,
        request_id=None,
    )
    if _failed(result) and result["error"].get("type") == _FILE_EXISTS_ERROR:
        raise EssentialRefusalError(
            f"a note already exists at {rel_path}; repeat with about={rel_path!r} "
            "to add to it, or word the memory differently"
        )
    return result


@replayable_write
async def _remember_impl(
    app: DatacronApp,
    *,
    content: str,
    about: str | None = None,
    kind: str = ESSENTIAL_REMEMBER_DEFAULT_KIND,
    source: str | None = None,
    actor: str = _DEFAULT_ACTOR,
    request_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        cleaned, cleaned_source = _validate_remember(content, kind, source)
        plan = await _plan_remember(app, about, kind)
        if plan.append_to is not None:
            entry = _format_entry(cleaned, kind, cleaned_source, _today())
            result = await _append_with_retry(app, plan.append_to, entry, actor)
            created = False
        else:
            result = await _create_for_remember(
                app,
                tags=plan.tags,
                content=cleaned,
                kind=kind,
                source=cleaned_source,
                actor=actor,
            )
            created = True
        if _failed(result):
            return result
        written = result["created"] if created else result["appended"]
        receipt: dict[str, Any] = {
            "rel_path": written["rel_path"],
            "operation_id": await _latest_operation_id(
                app, written["rel_path"], result["content_hash"]
            ),
            "content_hash": result["content_hash"],
            "indexed": result["indexed"],
            "created": created,
            "appended": not created,
        }
        _audit(
            _TOOL_REMEMBER,
            started,
            rel_path=receipt["rel_path"],
            kind=kind,
            created=created,
        )
        return receipt
    except ReplayedWriteError:
        raise
    except EssentialRefusalError as exc:
        return _error_response(_TOOL_REMEMBER, exc, started)
    except Exception:
        return _internal_error_response(_TOOL_REMEMBER, started)


def _heading_trail(header_path: str) -> list[str]:
    if not header_path:
        return []
    return [part for part in header_path.split(_HEADING_TRAIL_SEPARATOR) if part]


def _recall_scope_filters(
    app: DatacronApp, scope: str | None
) -> tuple[str | None, list[str] | None]:
    """Map ``scope`` to a (folder, tags) pair: a registered subject wins over a path."""
    if scope is None or not scope.strip():
        return None, None
    matches = matching_subject_tags(scope, app.organization)
    if len(matches) > 1:
        raise _ambiguous_scope(scope, list(matches))
    if matches:
        return None, [matches[0]]
    return scope.strip(), None


async def _recall_item(app: DatacronApp, hit: dict[str, Any]) -> dict[str, Any] | None:
    rel_path = str(hit["note_rel_path"])
    trail = _heading_trail(str(hit.get("header_path") or ""))
    payload: dict[str, Any] = {}
    read_more: dict[str, Any] = {
        "tool": "get_note",
        "id_or_path": rel_path,
        "heading_path": trail or None,
    }
    if trail:
        payload = await _get_note_impl(
            app,
            id_or_path=rel_path,
            fmt="full",
            limit=ESSENTIAL_RECALL_EXCERPT_MAX_CHARS,
            heading_path=trail,
        )
    if not trail or "error" in payload:
        chunk_id = str(hit["chunk_id"])
        payload = await _get_note_impl(app, id_or_path=chunk_id, fmt="full")
        read_more = {"tool": "get_note", "id_or_path": chunk_id, "heading_path": None}
        if "error" in payload:
            return None
    frontmatter = payload.get("frontmatter")
    last_verified = frontmatter.get("last_verified") if isinstance(frontmatter, dict) else None
    section = payload.get("section")
    shown_trail = section["heading_path"] if isinstance(section, dict) else trail
    return {
        "rel_path": payload["rel_path"],
        "title": payload.get("title", ""),
        "heading_path": shown_trail,
        "excerpt": payload["content"],
        "excerpt_truncated": bool(payload.get("truncated", False)),
        "content_hash": payload["content_hash"],
        "last_verified": None if last_verified is None else str(last_verified),
        "read_more": read_more,
    }


async def _recall_impl(
    app: DatacronApp,
    *,
    question: str,
    limit: int = ESSENTIAL_RECALL_DEFAULT_LIMIT,
    scope: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded = _bounded_count(limit, ESSENTIAL_RECALL_MAX_LIMIT)
    try:
        folder, tags = _recall_scope_filters(app, scope)
    except EssentialRefusalError as exc:
        return _error_response(_TOOL_RECALL, exc, started)
    try:
        found = await _search_text_impl(
            app,
            query=question,
            limit=bounded,
            folder=folder,
            tags=tags,
            group_by_note=True,
        )
        if "error" in found:
            return found
        items: list[dict[str, Any]] = []
        for hit in found["results"][:bounded]:
            item = await _recall_item(app, hit)
            if item is not None:
                items.append(item)
    except Exception:
        return _internal_error_response(_TOOL_RECALL, started)
    payload: dict[str, Any] = {
        "question": found["query"],
        "results": items,
        "returned": len(items),
        "limit_applied": bounded,
        "truncated": False,
    }
    while items and _serialized_chars(payload) > app.settings.max_read_response_chars:
        items.pop()
        payload["returned"] = len(items)
        payload["truncated"] = True
    _audit(_TOOL_RECALL, started, returned=len(items), scope=scope, truncated=payload["truncated"])
    return payload


def _parse_since(value: str) -> datetime:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        raise EssentialRefusalError("since must not be empty")
    duration = _DURATION_PATTERN.fullmatch(cleaned.lower())
    if duration is not None:
        unit = _DURATION_UNITS[duration.group(2)]
        try:
            return datetime.now(tz=UTC) - timedelta(**{unit: int(duration.group(1))})
        except OverflowError as exc:
            raise EssentialRefusalError("since duration is out of range") from exc
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise EssentialRefusalError(
            "since must be an ISO-8601 date or timestamp, or a duration such as 7d or 12h"
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalize_folder(folder: str) -> str:
    return folder.replace(_WINDOWS_SEPARATOR, _POSIX_SEPARATOR).strip().strip(_POSIX_SEPARATOR)


async def _change_scope(app: DatacronApp, scope: str | None) -> tuple[str | None, set[str] | None]:
    """Return a folder prefix or the set of paths of a registered subject."""
    if scope is None or not scope.strip():
        return None, None
    matches = matching_subject_tags(scope, app.organization)
    if len(matches) > 1:
        raise _ambiguous_scope(scope, list(matches))
    if matches:
        notes = await load_subject_notes(app, matches[0])
        return None, {item.rel_path for item in notes}
    return _normalize_folder(scope), None


def _in_scope(rel_path: str, folder: str | None, subject_paths: set[str] | None) -> bool:
    if subject_paths is not None:
        return rel_path in subject_paths
    if folder:
        return rel_path == folder or rel_path.startswith(folder + _POSIX_SEPARATOR)
    return True


def _operation_summary(app: DatacronApp, record: OperationRecord) -> str:
    """Describe what an operation did from its parameters, never from note content."""
    summary = _SUMMARY_BY_OPERATION.get(record.op, record.op.replace("_", " "))
    heading = record.parameters.get(_HEADING_PARAMETER)
    if isinstance(heading, str) and heading.strip():
        summary = f"{summary} under heading {heading.strip()!r}"
    return _sanitize_retrieval_metadata(app, summary)[:ESSENTIAL_WHAT_CHANGED_SUMMARY_MAX_CHARS]


def _operation_entry(app: DatacronApp, record: OperationRecord, title: str) -> dict[str, Any]:
    return {
        "operation_id": record.operation_id,
        "timestamp": record.timestamp,
        "op": record.op,
        "tool": record.tool,
        "actor": _sanitize_retrieval_metadata(app, record.actor),
        "rel_path": _redact_retrieval_text(app, record.rel_path),
        "title": title,
        "before_hash": record.before_hash,
        "after_hash": record.after_hash,
        "summary": _operation_summary(app, record),
    }


def _latest_records(
    records: list[OperationRecord],
) -> tuple[dict[str, OperationRecord], dict[str, OperationRecord]]:
    """Index the latest record per note id and per path from one journal read."""
    by_note_id: dict[str, OperationRecord] = {}
    by_path: dict[str, OperationRecord] = {}
    for record in records:
        if record.note_id is not None:
            by_note_id[record.note_id] = record
        by_path[record.rel_path] = record
    return by_note_id, by_path


async def _refresh_index(app: DatacronApp) -> bool:
    """Run the shared read repair; report whether it reconciled the index just now."""
    try:
        stats = await _repair_index_on_read(app)
    except Exception:
        return False
    return bool(app.write_policy.writes_allowed and stats["checked_notes"] > 0)


async def _out_of_band(
    app: DatacronApp,
    records: list[OperationRecord],
    since: datetime,
    folder: str | None,
    subject_paths: set[str] | None,
) -> list[dict[str, Any]]:
    by_note_id, by_path = _latest_records(records)
    since_ns = int(since.timestamp() * _NANOSECONDS_PER_SECOND)
    indexed = await app.store.list_indexed_notes_with_mtime()
    flagged: list[tuple[int, str, str, str]] = []
    for rel_path, (note_id, content_hash, mtime_ns) in indexed.items():
        if mtime_ns is None or mtime_ns < since_ns:
            continue
        if not _in_scope(rel_path, folder, subject_paths) or not admits_note_path(
            app.scope, rel_path
        ):
            continue
        latest = by_note_id.get(note_id) or by_path.get(rel_path)
        if latest is None:
            flagged.append((mtime_ns, rel_path, content_hash, _REASON_NO_JOURNAL_RECORD))
        elif latest.after_hash != content_hash:
            flagged.append((mtime_ns, rel_path, content_hash, _REASON_HASH_DIFFERS))
    flagged.sort(reverse=True)
    return [
        {
            "rel_path": _redact_retrieval_text(app, rel_path),
            "content_hash": content_hash,
            "modified_at": datetime.fromtimestamp(
                mtime_ns / _NANOSECONDS_PER_SECOND, tz=UTC
            ).isoformat(),
            "reason": reason,
            "_path": rel_path,
        }
        for mtime_ns, rel_path, content_hash, reason in flagged
    ]


async def _what_changed_impl(
    app: DatacronApp,
    *,
    since: str,
    scope: str | None = None,
    limit: int = ESSENTIAL_WHAT_CHANGED_DEFAULT_LIMIT,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        since_time = _parse_since(since)
        folder, subject_paths = await _change_scope(app, scope)
    except EssentialRefusalError as exc:
        return _error_response(_TOOL_WHAT_CHANGED, exc, started)
    bounded = _bounded_count(limit, ESSENTIAL_WHAT_CHANGED_MAX_LIMIT)
    try:
        refreshed = await _refresh_index(app)
        records = _admitted_records(app, await app.vault_writer.list_operations())
        window = [
            record
            for record in reversed(records)
            if datetime.fromisoformat(record.timestamp).astimezone(UTC) >= since_time
            and _in_scope(record.rel_path, folder, subject_paths)
        ]
        shown = window[:bounded]
        out_of_band = await _out_of_band(app, records, since_time, folder, subject_paths)
        shown_oob = out_of_band[:ESSENTIAL_WHAT_CHANGED_MAX_OUT_OF_BAND]
        titles = await app.store.note_titles(
            [record.rel_path for record in shown] + [item["_path"] for item in shown_oob]
        )
    except OperationLogError as exc:
        return _error_response(_TOOL_WHAT_CHANGED, exc, started)
    except Exception:
        return _internal_error_response(_TOOL_WHAT_CHANGED, started)
    groups: dict[str, dict[str, Any]] = {}
    for record in shown:
        title = _sanitize_retrieval_metadata(app, titles.get(record.rel_path, ""))
        group = groups.setdefault(
            record.rel_path,
            {
                "rel_path": _redact_retrieval_text(app, record.rel_path),
                "title": title,
                "operations": [],
            },
        )
        group["operations"].append(_operation_entry(app, record, title))
    for item in shown_oob:
        item["title"] = _sanitize_retrieval_metadata(app, titles.get(item.pop("_path"), ""))
    payload: dict[str, Any] = {
        "since": since_time.isoformat(),
        "notes": list(groups.values()),
        "total_operations": len(window),
        "returned_operations": len(shown),
        "limit_applied": bounded,
        "truncated": len(shown) < len(window),
        "out_of_band": shown_oob,
        "out_of_band_total": len(out_of_band),
        "out_of_band_truncated": len(shown_oob) < len(out_of_band),
        "coverage": {"index_refreshed": refreshed, "statement": _COVERAGE_STATEMENT},
    }
    _audit(
        _TOOL_WHAT_CHANGED,
        started,
        returned=len(shown),
        out_of_band=len(shown_oob),
        scope=scope,
    )
    return payload


def register_essential_tools(
    server: MCPServer[Any],
    app: Any,
    *,
    read_annotations: ToolAnnotations,
    write_annotations: ToolAnnotations,
) -> None:
    """Register the three verbs on ``server``; only the essential profile calls this."""

    @server.tool(
        name=_TOOL_REMEMBER,
        title="Remember",
        description=_REMEMBER_DESCRIPTION,
        annotations=write_annotations,
    )
    async def remember(
        content: str,
        ctx: Context[Any, Any],
        about: str | None = None,
        kind: str = ESSENTIAL_REMEMBER_DEFAULT_KIND,
        source: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await _remember_impl(
            app,
            content=content,
            about=about,
            kind=kind,
            source=source,
            actor=app.identity_provider.identify(ctx).actor,
            request_id=request_id,
        )

    @server.tool(
        name=_TOOL_RECALL,
        title="Recall",
        description=_RECALL_DESCRIPTION,
        annotations=read_annotations,
    )
    async def recall(
        question: str,
        limit: int = ESSENTIAL_RECALL_DEFAULT_LIMIT,
        scope: str | None = None,
    ) -> dict[str, Any]:
        return await _recall_impl(app, question=question, limit=limit, scope=scope)

    @server.tool(
        name=_TOOL_WHAT_CHANGED,
        title="What changed",
        description=_WHAT_CHANGED_DESCRIPTION,
        annotations=read_annotations,
    )
    async def what_changed(
        since: str,
        scope: str | None = None,
        limit: int = ESSENTIAL_WHAT_CHANGED_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        return await _what_changed_impl(app, since=since, scope=scope, limit=limit)
