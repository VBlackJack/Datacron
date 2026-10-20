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
"""Durable review marks for the Companion memory inbox.

A mark says that a person looked at one journaled operation. It is not a note
mutation and it is not written to the operation journal or to frontmatter: it lives
in a small JSON sidecar next to the journal, keyed by operation id. Reads are
lock-free; writes are read-compare-write under a cross-process file lock and an
optional compare-and-swap on the SHA-256 of the file bytes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from datacron.core.config import (
    COMPANION_STATE_DIR_NAME,
    INBOX_REVIEW_LOCK_KEY,
    INBOX_REVIEW_MAX_ENTRIES,
    INBOX_REVIEW_MAX_STATE_BYTES,
    INBOX_REVIEW_STATE_FILENAME,
    INBOX_REVIEW_STATE_VERSION,
    Settings,
)
from datacron.core.durability import atomic_durable_write
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import resolve_sidecar_root, sidecar_dir
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault_writer import hold_sidecar_lock

__all__ = [
    "EMPTY_STATE_HASH",
    "ReviewState",
    "ReviewStateConflictError",
    "read_review_state",
    "review_state_path",
    "update_review_state",
]

EMPTY_STATE_HASH: Final[str] = sha256_bytes(b"")
_KEY_VERSION: Final[str] = "version"
_KEY_REVIEWED: Final[str] = "reviewed"
_KEY_REVIEWED_AT: Final[str] = "reviewed_at"
_JSON_INDENT: Final[int] = 2


class ReviewStateConflictError(Exception):
    """Raised when the caller's state hash is not the hash of the file on disk."""

    def __init__(self, current_hash: str) -> None:
        super().__init__("the review state changed since it was read")
        self.current_hash = current_hash


@dataclass(frozen=True)
class ReviewState:
    """Review marks as read from disk, with the hash a writer must present back."""

    reviewed: Mapping[str, str]
    state_hash: str


def review_state_path(vault_root: Path, settings: Settings) -> Path:
    """Return the review-state file, anchored where the operation journal is anchored.

    In Dual-Vault mode that is the memory vault's sidecar, in Single-Vault mode the
    vault's own sidecar, which is what ``sidecar_dir`` resolves for these settings.
    """
    return (
        sidecar_dir(vault_root, settings=settings)
        / COMPANION_STATE_DIR_NAME
        / INBOX_REVIEW_STATE_FILENAME
    )


def _parse(raw: bytes) -> dict[str, str]:
    """Return operation id -> reviewed_at; an unreadable file counts as no marks."""
    if not raw or len(raw) > INBOX_REVIEW_MAX_STATE_BYTES:
        return {}
    try:
        decoded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    entries = decoded.get(_KEY_REVIEWED)
    if not isinstance(entries, dict):
        return {}
    marks: dict[str, str] = {}
    for operation_id, entry in entries.items():
        if not isinstance(operation_id, str) or not isinstance(entry, dict):
            continue
        reviewed_at = entry.get(_KEY_REVIEWED_AT)
        if isinstance(reviewed_at, str):
            marks[operation_id] = reviewed_at
    return marks


def _read_bytes(path: Path, anchor: Path) -> bytes:
    """Read the state file, refusing a path that passes through a link."""
    confined = assert_path_chain_without_links(path, anchor=anchor, allow_missing=True)
    try:
        return confined.read_bytes()
    except FileNotFoundError:
        return b""


def read_review_state(vault_root: Path, settings: Settings) -> ReviewState:
    """Read the review marks without taking the lock."""
    raw = _read_bytes(
        review_state_path(vault_root, settings),
        resolve_sidecar_root(vault_root, settings=settings),
    )
    return ReviewState(reviewed=_parse(raw), state_hash=sha256_bytes(raw))


def _serialize(marks: Mapping[str, str]) -> bytes:
    document = {
        _KEY_VERSION: INBOX_REVIEW_STATE_VERSION,
        _KEY_REVIEWED: {
            operation_id: {_KEY_REVIEWED_AT: marks[operation_id]} for operation_id in sorted(marks)
        },
    }
    return (json.dumps(document, indent=_JSON_INDENT, sort_keys=True) + "\n").encode("utf-8")


def _bounded(marks: dict[str, str]) -> dict[str, str]:
    """Keep the newest marks when the file would exceed its entry cap."""
    if len(marks) <= INBOX_REVIEW_MAX_ENTRIES:
        return marks
    newest = sorted(marks.items(), key=lambda item: (item[1], item[0]), reverse=True)
    return dict(newest[:INBOX_REVIEW_MAX_ENTRIES])


def update_review_state(
    vault_root: Path,
    settings: Settings,
    *,
    changes: Mapping[str, bool],
    journal_ids: Callable[[], Iterable[str]],
    expected_state_hash: str | None = None,
    now: datetime | None = None,
) -> ReviewState:
    """Apply ``changes`` (operation id -> reviewed?) under the review lock.

    ``journal_ids`` is called inside the lock and returns the operation ids the journal
    holds at that moment. ``expected_state_hash`` is the hash the caller read; when given
    and different from the file on disk the write is refused with
    :class:`ReviewStateConflictError`.
    Omitting it merges the change into whatever is on disk, which is safe because a
    mark and an unmark of different operations commute. Marks for operations the
    journal no longer holds are pruned, and the count is capped.
    """
    path = review_state_path(vault_root, settings)
    sidecar_root = resolve_sidecar_root(vault_root, settings=settings)
    stamp = (now or datetime.now(tz=UTC)).isoformat()
    with hold_sidecar_lock(
        sidecar_dir(vault_root, settings=settings),
        anchor=sidecar_root,
        key=INBOX_REVIEW_LOCK_KEY,
        timeout_seconds=settings.vault_lock_timeout_seconds,
    ):
        known = set(journal_ids())
        raw = _read_bytes(path, sidecar_root)
        current_hash = sha256_bytes(raw)
        if expected_state_hash is not None and expected_state_hash != current_hash:
            raise ReviewStateConflictError(current_hash)
        marks = _parse(raw)
        for operation_id, reviewed in changes.items():
            if reviewed:
                marks.setdefault(operation_id, stamp)
            else:
                marks.pop(operation_id, None)
        marks = _bounded({key: value for key, value in marks.items() if key in known})
        data = _serialize(marks)
        if data == raw:
            return ReviewState(reviewed=marks, state_hash=current_hash)
        parent = assert_path_chain_without_links(
            path.parent,
            anchor=sidecar_root,
            allow_missing=True,
        )
        parent.mkdir(parents=True, exist_ok=True)
        atomic_durable_write(path, data)
        return ReviewState(reviewed=marks, state_hash=sha256_bytes(data))
