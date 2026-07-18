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
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypeAlias

from datacron.core.config import (
    DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS,
    HISTORY_DIR_NAME,
    OPLOG_DIR_NAME,
    OPLOG_PENDING_DIR_NAME,
    SIDECAR_DIR_NAME,
)
from datacron.core.durability import atomic_durable_write, durable_flush_directory
from datacron.core.hashing import sha256_bytes
from datacron.core.logger import get_logger

JsonScalar: TypeAlias = str | int | float | bool | None

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS_FILENAME: Final[str] = "operations.jsonl"
_FORMAT_VERSION: Final[int] = 2
_LOGGER = get_logger(__name__)


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
    prev_hash: str | None = None
    format_version: int = _FORMAT_VERSION

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
            "prev_hash": self.prev_hash,
            "format_version": self.format_version,
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
        prev_hash = payload.get("prev_hash")
        format_version = payload.get("format_version", 1)
        if before_hash is not None and not isinstance(before_hash, str):
            raise OperationLogError("before_hash must be a string or null")
        if note_id is not None and not isinstance(note_id, str):
            raise OperationLogError("note_id must be a string or null")
        if not isinstance(parameters, dict):
            raise OperationLogError("parameters must be a JSON object")
        if not isinstance(history_stored, bool):
            raise OperationLogError("history_stored must be a boolean")
        if prev_hash is not None and not isinstance(prev_hash, str):
            raise OperationLogError("prev_hash must be a string or null")
        if not isinstance(format_version, int) or isinstance(format_version, bool):
            raise OperationLogError("format_version must be an integer")
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
            prev_hash=prev_hash,
            format_version=format_version,
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.before_hash is not None and not _HASH_PATTERN.fullmatch(self.before_hash):
            raise OperationLogError("before_hash is not a lowercase SHA-256")
        if not _HASH_PATTERN.fullmatch(self.after_hash):
            raise OperationLogError("after_hash is not a lowercase SHA-256")
        if self.prev_hash is not None and not _HASH_PATTERN.fullmatch(self.prev_hash):
            raise OperationLogError("prev_hash is not a lowercase SHA-256")
        if self.format_version not in {1, _FORMAT_VERSION}:
            raise OperationLogError(f"unsupported operation log format: {self.format_version}")
        if self.format_version == 1 and self.prev_hash is not None:
            raise OperationLogError("legacy operation records cannot contain prev_hash")
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
        purge_min_interval_seconds: float = DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._sidecar = vault_root.expanduser().resolve() / SIDECAR_DIR_NAME
        self._oplog_dir = self._sidecar / OPLOG_DIR_NAME
        self._pending_dir = self._oplog_dir / OPLOG_PENDING_DIR_NAME
        self._operations_path = self._oplog_dir / _OPERATIONS_FILENAME
        self._history_dir = self._sidecar / HISTORY_DIR_NAME
        self._retention_days = retention_days
        self._history_mode = history_mode
        self._purge_min_interval = timedelta(seconds=purge_min_interval_seconds)
        self._last_purge_at: datetime | None = None
        self._tail_record: OperationRecord | None = None
        self._tail_hash: str | None = None
        self._tail_loaded = False

    @property
    def history_enabled(self) -> bool:
        return self._history_mode == "full"

    def next_timestamp(self, now: datetime | None = None) -> str:
        candidate = (now or datetime.now(tz=UTC)).astimezone(UTC)
        self._ensure_tail_state()
        if self._tail_record is not None:
            previous = datetime.fromisoformat(self._tail_record.timestamp).astimezone(UTC)
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
        self._load_tail_state()
        if self._tail_record is not None and self._tail_record.operation_id == record.operation_id:
            return False
        chained = replace(
            record,
            prev_hash=self._tail_hash,
            format_version=_FORMAT_VERSION,
        )
        chained.validate()
        line = _record_line(chained)
        self._oplog_dir.mkdir(parents=True, exist_ok=True)
        created = not self._operations_path.exists()
        try:
            with self._operations_path.open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise OperationLogError("failed to append the operation log") from exc
        if created:
            _durable_flush_directory(self._oplog_dir)
        self._tail_record = chained
        self._tail_hash = sha256_bytes(line)
        self._tail_loaded = True
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
        self._ensure_tail_state()
        return _parse_records(self._operations_path.read_bytes(), verify_chain=True)

    def has_record(self, operation_id: str) -> bool:
        # Recovery queries are outside the append hot path, so a full verified scan
        # preserves idempotence without maintaining a second durable index.
        return any(record.operation_id == operation_id for record in self.read_records())

    def _ensure_tail_state(self) -> None:
        if not self._tail_loaded:
            self._load_tail_state()

    def _load_tail_state(self) -> None:
        tail = _read_tail_records(self._operations_path)
        if not tail:
            self._tail_record = None
            self._tail_hash = None
            self._tail_loaded = True
            return
        tail_record, tail_line = tail[-1]
        if tail_record.format_version == 1:
            self._migrate_legacy_log()
            return
        if any(record.format_version != _FORMAT_VERSION for record, _line in tail):
            raise OperationLogError("operation log tail mixes legacy and chained records")
        expected_prev_hash = sha256_bytes(tail[-2][1]) if len(tail) == 2 else None
        if tail_record.prev_hash != expected_prev_hash:
            raise OperationLogError("operation log tail hash chain mismatch")
        self._tail_record = tail_record
        self._tail_hash = sha256_bytes(tail_line)
        self._tail_loaded = True

    def _migrate_legacy_log(self) -> None:
        data = self._operations_path.read_bytes()
        legacy_records = _parse_records(data, verify_chain=False)
        if any(record.format_version != 1 for record in legacy_records):
            raise OperationLogError("operation log contains mixed legacy and chained records")
        chained_records: list[OperationRecord] = []
        previous_hash: str | None = None
        for record in legacy_records:
            chained = replace(
                record,
                prev_hash=previous_hash,
                format_version=_FORMAT_VERSION,
            )
            chained_records.append(chained)
            previous_hash = sha256_bytes(_record_line(chained))
        migrated = b"".join(_record_line(record) for record in chained_records)
        _atomic_write(self._operations_path, migrated)
        _LOGGER.warning(
            "Migrated %d legacy operation records to chained format version %d",
            len(chained_records),
            _FORMAT_VERSION,
        )
        self._tail_record = chained_records[-1] if chained_records else None
        self._tail_hash = previous_hash
        self._tail_loaded = True

    def purge_history(self, now: datetime | None = None) -> list[str]:
        purge_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
        if (
            self.history_enabled
            and self._last_purge_at is not None
            and purge_at - self._last_purge_at < self._purge_min_interval
        ):
            return []
        if not self._history_dir.is_dir():
            return []
        retained: set[str] = set()
        if self.history_enabled:
            cutoff = purge_at - timedelta(days=self._retention_days)
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
        self._last_purge_at = purge_at
        return removed


def _record_line(record: OperationRecord) -> bytes:
    rendered = json.dumps(
        record.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{rendered}\n".encode("ascii")


def _parse_records(data: bytes, *, verify_chain: bool) -> list[OperationRecord]:
    try:
        text = data.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise OperationLogError("operation log must be ASCII JSONL") from exc
    records: list[OperationRecord] = []
    seen: set[str] = set()
    previous_hash: str | None = None
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
        if verify_chain:
            if record.format_version != _FORMAT_VERSION:
                raise OperationLogError(f"legacy operation record remains at line {line_number}")
            if record.prev_hash != previous_hash:
                raise OperationLogError(f"operation hash chain mismatch at line {line_number}")
        seen.add(record.operation_id)
        records.append(record)
        previous_hash = sha256_bytes(_record_line(record))
    return records


def _read_tail_records(path: Path) -> list[tuple[OperationRecord, bytes]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            if end == 0:
                return []
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise OperationLogError("operation log does not end at a JSONL boundary")
            position = end
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count < 3:
                read_size = min(4096, position)
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            lines = b"".join(reversed(chunks)).splitlines()[-2:]
    except OSError as exc:
        raise OperationLogError("failed to read operation log tail") from exc
    records: list[tuple[OperationRecord, bytes]] = []
    for line in lines:
        try:
            payload = json.loads(line.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationLogError("invalid operation log tail record") from exc
        records.append((OperationRecord.from_dict(payload), line + b"\n"))
    return records


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_durable_write(path, data)


def _durable_flush_directory(path: Path) -> None:
    durable_flush_directory(path)
