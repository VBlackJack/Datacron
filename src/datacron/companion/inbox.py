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
"""Companion memory inbox: review what was written to the vault, and undo it.

The inbox lists the journaled operations the vault's scope admits, newest first,
shows the exact change as a unified diff, keeps a per-operation "reviewed" mark in a
sidecar (see :mod:`datacron.companion.review_state`), and restores the bytes that
preceded a change through the same revert path the MCP ``revert_note`` tool uses.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from datacron.companion.api import (
    _build_runtime_settings,
    _parse_bounded_int,
    companion_locale,
)
from datacron.companion.review_state import (
    ReviewStateConflictError,
    read_review_state,
    update_review_state,
)
from datacron.config_contract import read_companion_profile
from datacron.core.config import (
    COMPANION_REVERT_ACTOR,
    INBOX_DEFAULT_LIMIT,
    INBOX_DEFAULT_SINCE,
    INBOX_DIFF_CONTEXT_LINES,
    INBOX_DIFF_FROM_LABEL,
    INBOX_DIFF_MAX_CHARS,
    INBOX_DIFF_TO_LABEL,
    INBOX_FILTER_MAX_CHARS,
    INBOX_MAX_LIMIT,
    INBOX_MAX_OFFSET,
    INBOX_OPERATION_ID_MAX_CHARS,
    INBOX_REVERT_REQUEST_PREFIX,
    INBOX_SINCE_ALL,
    INBOX_SINCE_WINDOWS_SECONDS,
    Settings,
)
from datacron.core.durability import DurabilityUnavailableError, ReadOnlyModeError
from datacron.core.hashing import sha256_bytes
from datacron.core.markdown_headings import split_markdown_lines
from datacron.core.operation_log import HistoryUnavailableError, OperationRecord
from datacron.core.paths import PathConfinementError, sidecar_index_db
from datacron.core.scope import NoteAdmissionError, admits_note_path
from datacron.core.security import SecretRedactor
from datacron.core.vault_writer import VaultLockBusyError, WriteConflictError
from datacron.i18n import t
from datacron.mcp.server import build_app
from datacron.mcp.tools.payloads import VAULT_LOCK_BUSY_CODE
from datacron.mcp.tools.retrieval import protect_note_title
from datacron.mcp.tools.write import revert_note_impl

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = [
    "InboxError",
    "get_companion_inbox_diff",
    "list_companion_inbox",
    "revert_companion_inbox",
    "review_companion_inbox",
]

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"false", "0", "no"})
_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_DIGEST_CHARS: Final[int] = 32
_STATUS_DIFF: Final[str] = "diff"
_STATUS_CREATED: Final[str] = "created"
_STATUS_UNAVAILABLE: Final[str] = "unavailable"
_STATUS_CONFLICT: Final[str] = "conflict"
_COMMITTED_INDEX_INCOMPLETE: Final[str] = "committed_index_incomplete"
_WRITE_CONFLICT_ERROR_TYPE: Final[str] = WriteConflictError.__name__
# Error type reported by the revert tool -> (Companion error key, HTTP status).
_REVERT_FAILURES: Final[dict[str, tuple[str, HTTPStatus]]] = {
    ReadOnlyModeError.__name__: ("inbox_writes_disabled", HTTPStatus.FORBIDDEN),
    DurabilityUnavailableError.__name__: ("inbox_writes_disabled", HTTPStatus.FORBIDDEN),
    _WRITE_CONFLICT_ERROR_TYPE: ("inbox_revert_conflict", HTTPStatus.CONFLICT),
    HistoryUnavailableError.__name__: ("inbox_history_unavailable", HTTPStatus.CONFLICT),
    FileNotFoundError.__name__: ("inbox_note_missing", HTTPStatus.NOT_FOUND),
    NoteAdmissionError.__name__: ("inbox_note_missing", HTTPStatus.NOT_FOUND),
    ValueError.__name__: ("inbox_revert_noop", HTTPStatus.CONFLICT),
}


class InboxError(Exception):
    """A refusal the HTTP layer sends as-is: a localized message and a status code."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.extra: dict[str, Any] = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        return {"error": str(self), **self.extra}


def _refuse(locale: str, key: str, http_status: int, **extra: Any) -> InboxError:
    return InboxError(
        t(f"companion.errors.{key}", locale=locale),
        http_status=http_status,
        extra=extra,
    )


def _open_optional(profile_path: Path | None) -> tuple[DatacronApp, Path, Settings] | None:
    """Build the vault app, or ``None`` when no valid vault is configured yet."""
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        return None
    return build_app(settings=settings, vault_root=vault_root), vault_root, settings


def _open_required(
    profile_path: Path | None,
    locale: str,
) -> tuple[DatacronApp, Path, Settings]:
    opened = _open_optional(profile_path)
    if opened is None:
        raise _refuse(locale, "vault_required", HTTPStatus.BAD_REQUEST)
    return opened


def _text_param(
    query: Mapping[str, Any],
    name: str,
    locale: str,
    *,
    limit: int = INBOX_FILTER_MAX_CHARS,
) -> str | None:
    raw = query.get(name)
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if len(value) > limit:
        raise InboxError(
            t("companion.errors.inbox_filter_too_long", locale=locale, field=name, maximum=limit),
            http_status=HTTPStatus.BAD_REQUEST,
        )
    return value


def _bool_param(query: Mapping[str, Any], name: str, default: bool, locale: str) -> bool:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise InboxError(
        t("companion.errors.boolean_required", locale=locale, field=name),
        http_status=HTTPStatus.BAD_REQUEST,
    )


def _operation_id(payload: Mapping[str, Any], locale: str) -> str:
    raw = payload.get("operation_id")
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or len(value) > INBOX_OPERATION_ID_MAX_CHARS:
        raise _refuse(locale, "operation_id_required", HTTPStatus.BAD_REQUEST)
    return value


def _redact_path(app: DatacronApp, rel_path: str) -> str:
    if SecretRedactor.retrieval_enabled(app.settings):
        return app.secret_redactor.redact_text(rel_path)
    return rel_path


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _admitted_records(app: DatacronApp) -> list[OperationRecord]:
    records = await app.vault_writer.list_operations()
    return [rec for rec in records if admits_note_path(app.scope, rec.rel_path)]


async def _find_record(app: DatacronApp, operation_id: str, locale: str) -> OperationRecord:
    for rec in await _admitted_records(app):
        if rec.operation_id == operation_id:
            return rec
    raise _refuse(locale, "inbox_not_found", HTTPStatus.NOT_FOUND)


async def _current_note(app: DatacronApp, rel_path: str) -> tuple[str | None, str | None]:
    """Return the note's protected title and current content hash, or ``(None, None)``."""
    try:
        note = await app.vault_reader.read_note(app.scope.authorize_note_rel_path(rel_path))
    except (OSError, ValueError, NoteAdmissionError, PathConfinementError):
        return None, None
    return protect_note_title(app, note), note.content_hash


async def list_companion_inbox(
    query: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """List journaled operations, newest first, with their review marks."""
    locale = companion_locale(profile_path)
    limit = _parse_bounded_int(
        query.get("limit"),
        field_name="limit",
        default=INBOX_DEFAULT_LIMIT,
        minimum=1,
        maximum=INBOX_MAX_LIMIT,
        locale=locale,
    )
    offset = _parse_bounded_int(
        query.get("offset"),
        field_name="offset",
        default=0,
        minimum=0,
        maximum=INBOX_MAX_OFFSET,
        locale=locale,
    )
    since = _text_param(query, "since", locale) or INBOX_DEFAULT_SINCE
    if since != INBOX_SINCE_ALL and since not in INBOX_SINCE_WINDOWS_SECONDS:
        raise _refuse(locale, "inbox_invalid_since", HTTPStatus.BAD_REQUEST)
    unreviewed_only = _bool_param(query, "unreviewed", True, locale)
    note_filter = _text_param(query, "note", locale)
    tool_filter = _text_param(query, "tool", locale)
    actor_filter = _text_param(query, "actor", locale)

    opened = _open_optional(profile_path)
    if opened is None:
        return {
            "operations": [],
            "total": 0,
            "returned": 0,
            "limit_applied": limit,
            "offset": offset,
            "state_hash": None,
            "writes_enabled": False,
        }
    app, vault_root, settings = opened
    state = await asyncio.to_thread(read_review_state, vault_root, settings)
    cutoff: datetime | None = None
    if since != INBOX_SINCE_ALL:
        cutoff = datetime.now(tz=UTC) - timedelta(seconds=INBOX_SINCE_WINDOWS_SECONDS[since])
    needle = note_filter.casefold() if note_filter else None

    matching: list[OperationRecord] = []
    for rec in reversed(await _admitted_records(app)):
        if unreviewed_only and rec.operation_id in state.reviewed:
            continue
        if tool_filter is not None and rec.tool != tool_filter:
            continue
        if actor_filter is not None and rec.actor != actor_filter:
            continue
        if needle is not None and needle not in rec.rel_path.casefold():
            continue
        if cutoff is not None:
            stamp = _parse_timestamp(rec.timestamp)
            if stamp is None or stamp < cutoff:
                continue
        matching.append(rec)

    page = matching[offset : offset + limit]
    writes_enabled = app.write_policy.effective_writes_enabled
    present = await app.vault_writer.present_history_hashes(rec.before_hash for rec in page)
    rows: list[dict[str, Any]] = []
    for rec in page:
        title, content_hash = await _current_note(app, rec.rel_path)
        safe_path = _redact_path(app, rec.rel_path)
        can_revert = (
            writes_enabled
            and rec.before_hash is not None
            and rec.before_hash in present
            and content_hash is not None
            and content_hash != rec.before_hash
        )
        rows.append(
            {
                "operation_id": rec.operation_id,
                "timestamp": rec.timestamp,
                "rel_path": safe_path,
                "title": title if title is not None else _redact_path(app, Path(rec.rel_path).stem),
                "op": rec.op,
                "tool": rec.tool,
                "actor": rec.actor,
                "reviewed": rec.operation_id in state.reviewed,
                "can_revert": can_revert,
                "content_hash": content_hash,
            }
        )
    return {
        "operations": rows,
        "total": len(matching),
        "returned": len(rows),
        "limit_applied": limit,
        "offset": offset,
        "state_hash": state.state_hash,
        "writes_enabled": writes_enabled,
    }


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


async def _after_bytes(app: DatacronApp, rec: OperationRecord) -> bytes | None:
    """Bytes a write produced: from history when kept, else the note if it is unchanged."""
    stored = await app.vault_writer.read_history_bytes(rec.after_hash)
    if stored is not None:
        return stored
    try:
        path = app.scope.authorize_note_rel_path(rec.rel_path)
        current = await asyncio.to_thread(path.read_bytes)
    except (OSError, ValueError, NoteAdmissionError, PathConfinementError):
        return None
    return current if sha256_bytes(current) == rec.after_hash else None


def _diff_lines(text: str) -> list[str]:
    """Split on the line model the chunker uses, without the line ends."""
    return [line.rstrip("\r\n") for line in split_markdown_lines(text)]


def _limit_text(text: str) -> tuple[str, bool]:
    if len(text) <= INBOX_DIFF_MAX_CHARS:
        return text, False
    return text[:INBOX_DIFF_MAX_CHARS], True


async def get_companion_inbox_diff(
    query: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Return the change one operation made, as a unified diff over exact history bytes."""
    locale = companion_locale(profile_path)
    operation_id = _operation_id(query, locale)
    app, _vault_root, _settings = _open_required(profile_path, locale)
    rec = await _find_record(app, operation_id, locale)
    redact = SecretRedactor.retrieval_enabled(app.settings)
    after = await _after_bytes(app, rec)
    before: bytes | None = None
    if rec.before_hash is not None:
        before = await app.vault_writer.read_history_bytes(rec.before_hash)
    if after is None or (rec.before_hash is not None and before is None):
        return {"status": _STATUS_UNAVAILABLE}

    after_text = _decode(after)
    if redact:
        # Whole text, never line by line: a multi-line secret only matches whole.
        after_text = app.secret_redactor.redact_text(after_text)
    if before is None:
        content, truncated = _limit_text(after_text)
        return {"status": _STATUS_CREATED, "content": content, "truncated": truncated}

    before_text = _decode(before)
    if redact:
        before_text = app.secret_redactor.redact_text(before_text)
    lines = list(
        difflib.unified_diff(
            _diff_lines(before_text),
            _diff_lines(after_text),
            fromfile=INBOX_DIFF_FROM_LABEL,
            tofile=INBOX_DIFF_TO_LABEL,
            n=INBOX_DIFF_CONTEXT_LINES,
            lineterm="",
        )
    )
    body = lines[2:]
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    diff_text, truncated = _limit_text("\n".join(lines))
    return {
        "status": _STATUS_DIFF,
        "diff": diff_text,
        "added": added,
        "removed": removed,
        "truncated": truncated,
        "redacted_change_only": (rec.before_hash != rec.after_hash and before_text == after_text),
    }


async def review_companion_inbox(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Mark or unmark one operation as reviewed, under compare-and-swap on the state."""
    locale = companion_locale(profile_path)
    operation_id = _operation_id(payload, locale)
    reviewed = payload.get("reviewed")
    if not isinstance(reviewed, bool):
        raise InboxError(
            t("companion.errors.boolean_required", locale=locale, field="reviewed"),
            http_status=HTTPStatus.BAD_REQUEST,
        )
    raw_hash = payload.get("expected_state_hash")
    expected = raw_hash if isinstance(raw_hash, str) and raw_hash else None
    app, vault_root, settings = _open_required(profile_path, locale)
    await _find_record(app, operation_id, locale)
    journal_ids = [rec.operation_id for rec in await app.vault_writer.list_operations()]
    try:
        state = await asyncio.to_thread(
            lambda: update_review_state(
                vault_root,
                settings,
                changes={operation_id: reviewed},
                journal_ids=journal_ids,
                expected_state_hash=expected,
            )
        )
    except ReviewStateConflictError as exc:
        raise _refuse(
            locale,
            "inbox_state_conflict",
            HTTPStatus.CONFLICT,
            status=_STATUS_CONFLICT,
            state_hash=exc.current_hash,
        ) from exc
    except VaultLockBusyError as exc:
        raise _refuse(
            locale, "inbox_lock_busy", HTTPStatus.SERVICE_UNAVAILABLE, retryable=True
        ) from exc
    return {
        "operation_id": operation_id,
        "reviewed": operation_id in state.reviewed,
        "state_hash": state.state_hash,
    }


def _revert_request_id(operation_id: str) -> str:
    """Derive a valid, stable request id so a double submit replays instead of repeating."""
    digest = sha256_bytes(operation_id.encode("utf-8"))
    return f"{INBOX_REVERT_REQUEST_PREFIX}{digest[:_REQUEST_ID_DIGEST_CHARS]}"


def _revert_failure(result: Mapping[str, Any], locale: str) -> InboxError:
    error = result.get("error")
    detail: Mapping[str, Any] = error if isinstance(error, Mapping) else {}
    code = detail.get("code")
    error_type = str(detail.get("type", ""))
    if code == VAULT_LOCK_BUSY_CODE:
        return _refuse(locale, "inbox_lock_busy", HTTPStatus.SERVICE_UNAVAILABLE, retryable=True)
    key, http_status = _REVERT_FAILURES.get(
        error_type, ("internal", HTTPStatus.INTERNAL_SERVER_ERROR)
    )
    extra = {"status": _STATUS_CONFLICT} if error_type == _WRITE_CONFLICT_ERROR_TYPE else {}
    return _refuse(locale, key, http_status, **extra)


async def revert_companion_inbox(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Restore the bytes that preceded one operation, through the MCP revert path.

    The caller's ``expected_hash`` is the current note hash it saw; a stale value is a
    conflict and nothing is written. The caller reloads rather than retrying with a
    fresh hash, because the note has changed since the person decided to revert.
    """
    locale = companion_locale(profile_path)
    operation_id = _operation_id(payload, locale)
    raw_expected = payload.get("expected_hash")
    if not isinstance(raw_expected, str) or _HASH_PATTERN.fullmatch(raw_expected) is None:
        raise _refuse(locale, "inbox_expected_hash_required", HTTPStatus.BAD_REQUEST)
    app, vault_root, settings = _open_required(profile_path, locale)
    rec = await _find_record(app, operation_id, locale)
    if not app.write_policy.effective_writes_enabled:
        raise _refuse(locale, "inbox_writes_disabled", HTTPStatus.FORBIDDEN)
    if rec.before_hash is None:
        raise _refuse(locale, "inbox_revert_creation", HTTPStatus.CONFLICT)
    present = await app.vault_writer.present_history_hashes([rec.before_hash])
    if rec.before_hash not in present:
        raise _refuse(locale, "inbox_history_unavailable", HTTPStatus.CONFLICT)

    # The index is opened read-write around the revert, as a sync does, so the
    # reconcile that follows the write leaves the note indexed.
    db_path = sidecar_index_db(vault_root, settings=settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await app.store.open(db_path)
    try:
        result = await revert_note_impl(
            app,
            note=rec.rel_path,
            to_hash=rec.before_hash,
            expected_hash=raw_expected,
            actor=COMPANION_REVERT_ACTOR,
            request_id=_revert_request_id(operation_id),
        )
    finally:
        await app.store.close()

    error = result.get("error")
    if isinstance(error, Mapping) and error.get("code") == _COMMITTED_INDEX_INCOMPLETE:
        return {
            "status": "reverted",
            "rel_path": _redact_path(app, rec.rel_path),
            "content_hash": error.get("content_hash"),
            "indexed": False,
            "replayed": False,
        }
    if error is not None:
        raise _revert_failure(result, locale)
    return {
        "status": "reverted",
        "rel_path": _redact_path(app, rec.rel_path),
        "content_hash": result.get("content_hash"),
        "indexed": bool(result.get("indexed", False)),
        "replayed": bool(result.get("replayed", False)),
    }
