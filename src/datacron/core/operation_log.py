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
"""Durable JSONL operation evidence and content-addressed note history."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypeAlias

from datacron.core.config import (
    HISTORY_DIR_NAME,
    OPLOG_DIR_NAME,
    OPLOG_PENDING_DIR_NAME,
    SIDECAR_DIR_NAME,
)
from datacron.core.hashing import sha256_bytes

JsonScalar: TypeAlias = str | int | float | bool | None

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS_FILENAME: Final[str] = "operations.jsonl"


class OperationLogError(RuntimeError):
    """Raised when durable audit or history state is invalid."""


class HistoryUnavailableError(OperationLogError):
    """Raised when requested exact history bytes are absent or redacted."""


@dataclass(frozen=True)
class OperationContext:
    """Non-content audit metadata supplied by one mutation tool."""

    op: str
    tool: str
    actor: str
    parameters: dict[str, JsonScalar]


@dataclass(frozen=True)
class OperationRecord:
    """One final, committed operation-log line."""

    operation_id: str
    timestamp: str
    op: str
    tool: str
    note_id: str | None
    rel_path: str
    before_hash: str | None
    after_hash: str
    actor: str
    parameters: dict[str, JsonScalar]
    history_stored: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "timestamp": self.timestamp,
            "op": self.op,
            "tool": self.tool,
            "note_id": self.note_id,
            "rel_path": self.rel_path,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "actor": self.actor,
            "parameters": self.parameters,
            "history_stored": self.history_stored,
        }

    @classmethod
    def from_dict(cls, payload: object) -> OperationRecord:
        if not isinstance(payload, dict):
            raise OperationLogError("operation record must be a JSON object")
        required_strings = (
            "operation_id",
            "timestamp",
            "op",
            "tool",
            "rel_path",
            "after_hash",
            "actor",
        )
        for key in required_strings:
            if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
                raise OperationLogError(f"operation record field {key!r} must be a string")
        before_hash = payload.get("before_hash")
        note_id = payload.get("note_id")
        parameters = payload.get("parameters")
        history_stored = payload.get("history_stored")
        if before_hash is not None and not isinstance(before_hash, str):
            raise OperationLogError("before_hash must be a string or null")
        if note_id is not None and not isinstance(note_id, str):
            raise OperationLogError("note_id must be a string or null")
        if not isinstance(parameters, dict):
            raise OperationLogError("parameters must be a JSON object")
        if not isinstance(history_stored, bool):
            raise OperationLogError("history_stored must be a boolean")
        cleaned_parameters: dict[str, JsonScalar] = {}
        for key, value in parameters.items():
            scalar = isinstance(value, (str, int, float, bool, type(None)))
            if not isinstance(key, str) or not scalar:
                raise OperationLogError("parameters must contain scalar JSON values")
            cleaned_parameters[key] = value
        record = cls(
            operation_id=str(payload["operation_id"]),
            timestamp=str(payload["timestamp"]),
            op=str(payload["op"]),
            tool=str(payload["tool"]),
            note_id=note_id,
            rel_path=str(payload["rel_path"]),
            before_hash=before_hash,
            after_hash=str(payload["after_hash"]),
            actor=str(payload["actor"]),
            parameters=cleaned_parameters,
            history_stored=history_stored,
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.before_hash is not None and not _HASH_PATTERN.fullmatch(self.before_hash):
            raise OperationLogError("before_hash is not a lowercase SHA-256")
        if not _HASH_PATTERN.fullmatch(self.after_hash):
            raise OperationLogError("after_hash is not a lowercase SHA-256")
        try:
            parsed = datetime.fromisoformat(self.timestamp)
        except ValueError as exc:
            raise OperationLogError("timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise OperationLogError("timestamp must include a timezone")


class OperationJournal:
    """Manage final audit records, pending manifests, and exact history blobs."""

    def __init__(
        self,
        vault_root: Path,
        *,
        retention_days: int,
        history_mode: str,
    ) -> None:
        self._sidecar = vault_root.expanduser().resolve() / SIDECAR_DIR_NAME
        self._oplog_dir = self._sidecar / OPLOG_DIR_NAME
        self._pending_dir = self._oplog_dir / OPLOG_PENDING_DIR_NAME
        self._operations_path = self._oplog_dir / _OPERATIONS_FILENAME
        self._history_dir = self._sidecar / HISTORY_DIR_NAME
        self._retention_days = retention_days
        self._history_mode = history_mode

    @property
    def history_enabled(self) -> bool:
        return self._history_mode == "full"

    def next_timestamp(self, now: datetime | None = None) -> str:
        candidate = (now or datetime.now(tz=UTC)).astimezone(UTC)
        records = self.read_records()
        if records:
            previous = datetime.fromisoformat(records[-1].timestamp).astimezone(UTC)
            if candidate <= previous:
                candidate = previous + timedelta(microseconds=1)
        return candidate.isoformat(timespec="microseconds")

    def store_history(self, data: bytes) -> str:
        content_hash = sha256_bytes(data)
        if not self.history_enabled:
            return content_hash
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._history_dir / content_hash
        if path.exists():
            existing = path.read_bytes()
            if sha256_bytes(existing) != content_hash or existing != data:
                raise OperationLogError(f"history blob is corrupt: {content_hash}")
            return content_hash
        _atomic_write(path, data)
        return content_hash

    def read_history(self, content_hash: str) -> bytes:
        if not _HASH_PATTERN.fullmatch(content_hash):
            raise ValueError("to_hash must be a lowercase SHA-256")
        if not self.history_enabled:
            raise HistoryUnavailableError("history content is redacted by vault policy")
        path = self._history_dir / content_hash
        if not path.is_file():
            raise HistoryUnavailableError(f"history version not found: {content_hash}")
        data = path.read_bytes()
        if sha256_bytes(data) != content_hash:
            raise OperationLogError(f"history blob hash mismatch: {content_hash}")
        return data

    def write_pending(self, record: OperationRecord) -> None:
        record.validate()
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.pending_path(record.operation_id), _record_line(record))

    def read_pending(self, path: Path) -> OperationRecord:
        try:
            payload = json.loads(path.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationLogError(f"invalid pending operation manifest: {path}") from exc
        return OperationRecord.from_dict(payload)

    def pending_paths(self) -> list[Path]:
        if not self._pending_dir.is_dir():
            return []
        return sorted(self._pending_dir.glob("*.json"))

    def pending_path(self, operation_id: str) -> Path:
        return self._pending_dir / f"{operation_id}.json"

    def append_record(self, record: OperationRecord) -> bool:
        record.validate()
        existing = self._operations_path.read_bytes() if self._operations_path.is_file() else b""
        if existing and not existing.endswith(b"\n"):
            raise OperationLogError("operation log does not end at a JSONL boundary")
        records = _parse_records(existing)
        if any(item.operation_id == record.operation_id for item in records):
            return False
        self._oplog_dir.mkdir(parents=True, exist_ok=True)
        updated = existing + _record_line(record)
        if not updated.startswith(existing):
            raise OperationLogError("operation log append changed its existing prefix")
        _atomic_write(self._operations_path, updated)
        return True

    def remove_pending(self, operation_id: str) -> None:
        path = self.pending_path(operation_id)
        if not path.exists():
            return
        path.unlink()
        _durable_flush_directory(self._pending_dir)

    def read_records(self) -> list[OperationRecord]:
        if not self._operations_path.is_file():
            return []
        return _parse_records(self._operations_path.read_bytes())

    def has_record(self, operation_id: str) -> bool:
        return any(record.operation_id == operation_id for record in self.read_records())

    def purge_history(self, now: datetime | None = None) -> list[str]:
        if not self._history_dir.is_dir():
            return []
        retained: set[str] = set()
        if self.history_enabled:
            cutoff = (now or datetime.now(tz=UTC)).astimezone(UTC) - timedelta(
                days=self._retention_days
            )
            for record in self.read_records():
                timestamp = datetime.fromisoformat(record.timestamp).astimezone(UTC)
                if timestamp < cutoff:
                    continue
                if record.before_hash is not None:
                    retained.add(record.before_hash)
                retained.add(record.after_hash)
        removed: list[str] = []
        for path in sorted(self._history_dir.iterdir()):
            if not path.is_file() or not _HASH_PATTERN.fullmatch(path.name):
                continue
            if path.name in retained:
                continue
            path.unlink()
            removed.append(path.name)
        if removed:
            _durable_flush_directory(self._history_dir)
        return removed


def _record_line(record: OperationRecord) -> bytes:
    rendered = json.dumps(
        record.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{rendered}\n".encode("ascii")


def _parse_records(data: bytes) -> list[OperationRecord]:
    try:
        text = data.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise OperationLogError("operation log must be ASCII JSONL") from exc
    records: list[OperationRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OperationLogError(f"invalid JSONL at line {line_number}") from exc
        record = OperationRecord.from_dict(payload)
        if record.operation_id in seen:
            raise OperationLogError(f"duplicate operation_id at line {line_number}")
        seen.add(record.operation_id)
        records.append(record)
    return records


def _atomic_write(path: Path, data: bytes) -> None:
    from datacron.core.vault_writer import atomic_durable_write  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_durable_write(path, data)


def _durable_flush_directory(path: Path) -> None:
    from datacron.core.vault_writer import durable_flush_directory  # noqa: PLC0415

    durable_flush_directory(path)
