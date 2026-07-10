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
"""Confined, locked, durable filesystem writes for Markdown vault notes."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Final, final
from uuid import uuid4

from datacron.core.config import SIDECAR_DIR_NAME, Settings, VaultConfig
from datacron.core.durability import (
    WritePolicy,
    flush_directory_entry,
    probe_directory_durability,
)
from datacron.core.hashing import sha256_bytes
from datacron.core.logger import get_logger
from datacron.core.operation_log import (
    HistoryUnavailableError,
    OperationContext,
    OperationJournal,
    OperationLogError,
    OperationRecord,
)
from datacron.core.paths import (
    PathConfinementError,
    assert_within_write_paths,
    sidecar_dir,
    sidecar_index_db,
)
from datacron.core.protocols import VaultWriter
from datacron.core.security import SecretRedactor

__all__ = [
    "FAULT_POINTS",
    "OPERATION_FAULT_POINTS",
    "FilesystemVaultWriter",
    "OperationRecoveryError",
    "UlidCollisionError",
    "UlidVerificationError",
    "WriteConflictError",
    "atomic_durable_write",
    "durable_flush_directory",
]

_LOGGER = get_logger(__name__)

NoteMutation = Callable[[str], str]
FaultInjector = Callable[[str], None]

FAULT_POINTS: Final[tuple[str, ...]] = (
    "before_temp_open",
    "after_temp_open",
    "after_temp_write",
    "after_temp_flush",
    "after_temp_fsync",
    "after_replace",
    "after_directory_fsync",
)
OPERATION_FAULT_POINTS: Final[tuple[str, ...]] = (
    "after_history_write",
    "after_pending_write",
    "after_note_write",
    "after_oplog_write",
    "after_pending_cleanup",
)
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_FRONTMATTER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?m)^id:[ \t]*['\"]?([0-9A-HJKMNP-TV-Z]{26})['\"]?[ \t]*$"
)
_LOCK_RETRY_SECONDS: Final[float] = 0.05


class WriteConflictError(ValueError):
    """Raised when compare-and-swap detects a stale or missing target."""


class UlidCollisionError(ValueError):
    """Raised when a proposed note ULID already exists in the vault identity data."""


class UlidVerificationError(ValueError):
    """Raised when all configured ULID identity sources cannot be verified."""


class OperationRecoveryError(OperationLogError):
    """Raised when a pending operation cannot be reconciled by exact hash."""


def atomic_durable_write(
    path: Path,
    data: bytes,
    *,
    fault_injector: FaultInjector | None = None,
) -> str:
    """Atomically replace ``path`` with durable exact ``data`` and return its SHA-256.

    The caller must create ``path.parent`` first. The temporary file is always a
    sibling so the replacement stays on one filesystem. On Windows, directory
    flushing uses a Win32 directory handle because ``os.open`` rejects directories.
    A target-file fsync is the logged degraded fallback for filesystems that reject
    directory flushing.
    """
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _inject(fault_injector, "before_temp_open")
    try:
        with temp_path.open("xb") as temp_file:
            _inject(fault_injector, "after_temp_open")
            temp_file.write(data)
            _inject(fault_injector, "after_temp_write")
            temp_file.flush()
            _inject(fault_injector, "after_temp_flush")
            os.fsync(temp_file.fileno())
            _inject(fault_injector, "after_temp_fsync")

        os.replace(temp_path, path)
        _inject(fault_injector, "after_replace")
        if not _flush_directory_or_false(path.parent):
            _fsync_file(path)
            _LOGGER.warning(
                "Parent-directory fsync unavailable for %s; used degraded target-file fsync",
                path,
            )
        _inject(fault_injector, "after_directory_fsync")
    finally:
        temp_path.unlink(missing_ok=True)
    return sha256_bytes(data)


def durable_flush_directory(path: Path) -> None:
    """Durably flush directory metadata, with the documented Windows fallback."""
    if not _flush_directory_or_false(path):
        _LOGGER.warning("Directory fsync unavailable for metadata update under %s", path)


def _flush_directory_or_false(path: Path) -> bool:
    try:
        return _fsync_directory(path)
    except OSError as exc:
        _LOGGER.warning("Directory fsync failed for %s: %s", path, exc)
        return False


@final
class FilesystemVaultWriter:
    """Serialize note transactions under configured write and EOL policies."""

    def __init__(
        self,
        vault_root: Path,
        settings: Settings,
        vault_config: VaultConfig | None = None,
        *,
        operation_fault_injector: FaultInjector | None = None,
        write_policy: WritePolicy | None = None,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._settings = settings
        self._vault_config = vault_config or VaultConfig()
        self._operation_fault_injector = operation_fault_injector
        self._write_policy = write_policy or WritePolicy(
            settings,
            probe_directory_durability(self._vault_root),
        )
        self._secret_redactor = SecretRedactor.from_settings(settings)
        self._operation_journal = OperationJournal(
            self._vault_root,
            retention_days=self._vault_config.history_retention_days,
            history_mode=self._vault_config.history_mode,
        )

    async def write_note_atomic(
        self,
        rel_path: str,
        content: str,
        *,
        overwrite: bool,
        expected_hash: str | None = None,
        note_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> str:
        """Write complete content under lock and return its exact-byte hash.

        ``note_id`` enables the global identity lock and collision checks used by
        create operations. Existing-note read-modify-write tools should use
        :meth:`mutate_note_atomic` so their read stays inside the same lock.
        """
        return await asyncio.to_thread(
            self._write_note_atomic_sync,
            rel_path,
            content,
            overwrite,
            expected_hash,
            note_id,
            operation,
        )

    async def mutate_note_atomic(
        self,
        rel_path: str,
        mutation: NoteMutation,
        *,
        expected_hash: str | None = None,
        operation: OperationContext | None = None,
    ) -> str:
        """Run a complete locked read-CAS-mutate-durable-write transaction."""
        return await asyncio.to_thread(
            self._mutate_note_atomic_sync,
            rel_path,
            mutation,
            expected_hash,
            operation,
        )

    async def revert_note_atomic(
        self,
        rel_path: str,
        to_hash: str,
        *,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        """Restore exact history bytes under CAS and journal the revert."""
        return await asyncio.to_thread(
            self._revert_note_atomic_sync,
            rel_path,
            to_hash,
            expected_hash,
            operation,
        )

    async def recover_operations(self) -> int:
        """Resolve durable pending manifests before serving or writing."""
        self._write_policy.ensure_writable()
        return await asyncio.to_thread(self._recover_operations_sync)

    async def list_operations(self) -> list[OperationRecord]:
        """Return an immutable snapshot of committed operation records."""
        return await asyncio.to_thread(self._list_operations_sync)

    async def purge_history(self) -> list[str]:
        """Apply the configured content-history retention policy now."""
        self._write_policy.ensure_writable()
        return await asyncio.to_thread(self._purge_history_sync)

    def _write_note_atomic_sync(
        self,
        rel_path: str,
        content: str,
        overwrite: bool,
        expected_hash: str | None,
        note_id: str | None,
        operation: OperationContext | None,
    ) -> str:
        self._write_policy.ensure_writable()
        self._recover_operations_sync()
        target, safe_rel_path = self._resolve_target(rel_path)
        if note_id is not None and not _ULID_PATTERN.fullmatch(note_id):
            raise ValueError("note_id must be a canonical 26-character ULID")

        target.parent.mkdir(parents=True, exist_ok=True)
        identity_lock = self._advisory_lock("identity") if note_id is not None else nullcontext()
        with identity_lock, self._advisory_lock(f"note:{self._lock_key(target)}"):
            current_bytes = target.read_bytes() if target.exists() else None
            _check_expected_hash(expected_hash, current_bytes)
            if current_bytes is not None and not overwrite:
                raise FileExistsError(f"{safe_rel_path} already exists.")
            if note_id is not None and self._ulid_exists(note_id):
                raise UlidCollisionError(f"ULID collision: {note_id} already exists")

            emitted = self._encode_with_eol_policy(content, current_bytes)
            if operation is not None:
                with self._advisory_lock("oplog"):
                    return self._commit_operation_sync(
                        target,
                        safe_rel_path,
                        current_bytes,
                        emitted,
                        note_id,
                        operation,
                    )
            if current_bytes is not None:
                self._operation_journal.store_history(current_bytes)
            return atomic_durable_write(target, emitted)

    def _mutate_note_atomic_sync(
        self,
        rel_path: str,
        mutation: NoteMutation,
        expected_hash: str | None,
        operation: OperationContext | None,
    ) -> str:
        self._write_policy.ensure_writable()
        self._recover_operations_sync()
        target, safe_rel_path = self._resolve_target(rel_path)
        with self._advisory_lock(f"note:{self._lock_key(target)}"):
            if not target.is_file():
                raise FileNotFoundError(
                    f"note not found at {safe_rel_path.as_posix()}; use create_note_ai"
                )
            current_bytes = target.read_bytes()
            _check_expected_hash(expected_hash, current_bytes)
            current = current_bytes.decode("utf-8", errors="strict")
            content = mutation(current)
            emitted = self._encode_with_eol_policy(content, current_bytes)
            if operation is not None:
                with self._advisory_lock("oplog"):
                    return self._commit_operation_sync(
                        target,
                        safe_rel_path,
                        current_bytes,
                        emitted,
                        None,
                        operation,
                    )
            self._operation_journal.store_history(current_bytes)
            return atomic_durable_write(target, emitted)

    def _revert_note_atomic_sync(
        self,
        rel_path: str,
        to_hash: str,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        self._write_policy.ensure_writable()
        self._recover_operations_sync()
        target, safe_rel_path = self._resolve_target(rel_path)
        with self._advisory_lock(f"note:{self._lock_key(target)}"):
            if not target.is_file():
                raise FileNotFoundError(f"note not found at {safe_rel_path.as_posix()}")
            current_bytes = target.read_bytes()
            _check_expected_hash(expected_hash, current_bytes)
            with self._advisory_lock("oplog"):
                belongs_to_note = any(
                    record.rel_path == safe_rel_path.as_posix()
                    and to_hash in {record.before_hash, record.after_hash}
                    for record in self._operation_journal.read_records()
                )
                if not belongs_to_note:
                    raise HistoryUnavailableError(
                        f"history version {to_hash} is not recorded for {safe_rel_path.as_posix()}"
                    )
                history_bytes = self._operation_journal.read_history(to_hash)
                if history_bytes == current_bytes:
                    raise ValueError("note already has the requested history hash")
                return self._commit_operation_sync(
                    target,
                    safe_rel_path,
                    current_bytes,
                    history_bytes,
                    None,
                    operation,
                )

    def _commit_operation_sync(
        self,
        target: Path,
        safe_rel_path: Path,
        before_bytes: bytes | None,
        after_bytes: bytes,
        note_id: str | None,
        operation: OperationContext,
    ) -> str:
        before_hash = sha256_bytes(before_bytes) if before_bytes is not None else None
        after_hash = sha256_bytes(after_bytes)
        resolved_note_id = self._resolve_operation_note_id(
            explicit=note_id,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            rel_path=safe_rel_path,
        )
        history_stored = before_bytes is not None and self._operation_journal.history_enabled
        record = OperationRecord(
            operation_id=uuid4().hex,
            timestamp=self._operation_journal.next_timestamp(),
            op=operation.op,
            tool=operation.tool,
            note_id=resolved_note_id,
            rel_path=safe_rel_path.as_posix(),
            before_hash=before_hash,
            after_hash=after_hash,
            actor=self._secret_redactor.redact_text(operation.actor.strip())
            or "mcp-client:unidentified",
            parameters={
                key: self._secret_redactor.redact_text(value) if isinstance(value, str) else value
                for key, value in operation.parameters.items()
            },
            history_stored=history_stored,
        )
        if before_bytes is not None:
            stored_hash = self._operation_journal.store_history(before_bytes)
            if stored_hash != before_hash:
                raise OperationLogError("stored history hash differs from before_hash")
        _inject(self._operation_fault_injector, "after_history_write")
        self._operation_journal.write_pending(record)
        _inject(self._operation_fault_injector, "after_pending_write")
        written_hash = atomic_durable_write(target, after_bytes)
        if written_hash != after_hash:
            raise OperationLogError("durable note hash differs from prepared after_hash")
        _inject(self._operation_fault_injector, "after_note_write")
        self._operation_journal.append_record(record)
        _inject(self._operation_fault_injector, "after_oplog_write")
        self._operation_journal.remove_pending(record.operation_id)
        _inject(self._operation_fault_injector, "after_pending_cleanup")
        removed = self._operation_journal.purge_history()
        _LOGGER.info(
            "operation committed id=%s op=%s tool=%s note_id=%s rel_path=%s "
            "before_hash=%s after_hash=%s actor=%s history_purged=%d",
            record.operation_id,
            record.op,
            record.tool,
            record.note_id,
            record.rel_path,
            record.before_hash,
            record.after_hash,
            record.actor,
            len(removed),
        )
        return after_hash

    def _recover_operations_sync(self) -> int:
        recovered = 0
        for pending_path in self._operation_journal.pending_paths():
            record = self._operation_journal.read_pending(pending_path)
            candidate = (self._vault_root / record.rel_path).expanduser().resolve()
            safe_rel_path = self._safe_relative_path(candidate)
            with (
                self._advisory_lock(f"note:{self._lock_key(candidate)}"),
                self._advisory_lock("oplog"),
            ):
                current_path = self._operation_journal.pending_path(record.operation_id)
                if not current_path.is_file():
                    continue
                record = self._operation_journal.read_pending(current_path)
                current_bytes = candidate.read_bytes() if candidate.is_file() else None
                current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
                if self._operation_journal.has_record(record.operation_id):
                    if current_hash != record.after_hash:
                        raise OperationRecoveryError(
                            f"committed operation {record.operation_id} no longer matches "
                            f"{safe_rel_path.as_posix()}"
                        )
                elif current_hash == record.after_hash:
                    self._operation_journal.append_record(record)
                    recovered += 1
                    _LOGGER.warning(
                        "recovered committed operation id=%s rel_path=%s after_hash=%s",
                        record.operation_id,
                        record.rel_path,
                        record.after_hash,
                    )
                elif current_hash != record.before_hash:
                    raise OperationRecoveryError(
                        f"pending operation {record.operation_id} cannot reconcile "
                        f"{safe_rel_path.as_posix()}"
                    )
                self._operation_journal.remove_pending(record.operation_id)
        with self._advisory_lock("oplog"):
            self._operation_journal.purge_history()
        return recovered

    def _list_operations_sync(self) -> list[OperationRecord]:
        with self._advisory_lock("oplog"):
            return self._operation_journal.read_records()

    def _purge_history_sync(self) -> list[str]:
        with self._advisory_lock("oplog"):
            return self._operation_journal.purge_history()

    def _resolve_operation_note_id(
        self,
        *,
        explicit: str | None,
        before_bytes: bytes | None,
        after_bytes: bytes,
        rel_path: Path,
    ) -> str | None:
        if explicit is not None:
            return explicit
        for data in (after_bytes, before_bytes):
            if data is None:
                continue
            try:
                text = data.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                continue
            frontmatter_block = _frontmatter_block(text)
            if frontmatter_block is None:
                continue
            match = _FRONTMATTER_ID_PATTERN.search(frontmatter_block)
            if match is not None:
                return match.group(1)
        sidecar_path = sidecar_dir(self._vault_root) / "ulids.json"
        if not sidecar_path.is_file():
            return None
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(rel_path.as_posix())
        return value if isinstance(value, str) else None

    def _resolve_target(self, rel_path: str) -> tuple[Path, Path]:
        candidate = (self._vault_root / rel_path).expanduser().resolve()
        target = assert_within_write_paths(candidate, self._settings)
        return target, self._safe_relative_path(target)

    def _safe_relative_path(self, target: Path) -> Path:
        try:
            return target.relative_to(self._vault_root)
        except ValueError as exc:
            raise PathConfinementError(
                f"Path {target} is outside the bound vault root {self._vault_root}."
            ) from exc

    def _encode_with_eol_policy(self, content: str, current_bytes: bytes | None) -> bytes:
        eol = (
            _dominant_eol(current_bytes)
            if current_bytes is not None
            else _configured_eol(self._vault_config.line_endings)
        )
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if eol == "\r\n":
            normalized = normalized.replace("\n", "\r\n")
        return normalized.encode("utf-8")

    @contextmanager
    def _advisory_lock(self, key: str) -> Iterator[None]:
        lock_dir = sidecar_dir(self._vault_root) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = f"{sha256_bytes(key.encode('utf-8'))}.lock"
        lock_path = lock_dir / lock_name
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\x00")
                lock_file.flush()
            if os.name == "nt":
                _lock_windows_file(lock_file)
            else:
                _lock_posix_file(lock_file)
            try:
                yield
            finally:
                if os.name == "nt":
                    _unlock_windows_file(lock_file)
                else:
                    _unlock_posix_file(lock_file)

    @staticmethod
    def _lock_key(target: Path) -> str:
        return os.path.normcase(str(target))

    def _ulid_exists(self, note_id: str) -> bool:
        return (
            self._ulid_exists_in_index(note_id)
            or self._ulid_exists_in_sidecar(note_id)
            or self._ulid_exists_in_frontmatter(note_id)
        )

    def _ulid_exists_in_index(self, note_id: str) -> bool:
        db_path = sidecar_index_db(self._vault_root)
        if not db_path.is_file():
            return False
        try:
            with sqlite3.connect(db_path, timeout=5.0) as connection:
                row = connection.execute(
                    "SELECT 1 FROM ulid_paths WHERE note_id = ? LIMIT 1",
                    (note_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise UlidVerificationError(
                f"could not verify ULID uniqueness in index {db_path}"
            ) from exc
        return row is not None

    def _ulid_exists_in_sidecar(self, note_id: str) -> bool:
        sidecar_path = sidecar_dir(self._vault_root) / "ulids.json"
        if not sidecar_path.is_file():
            return False
        try:
            payload = json.loads(sidecar_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UlidVerificationError(
                f"could not verify ULID uniqueness in sidecar {sidecar_path}"
            ) from exc
        return isinstance(payload, dict) and note_id in payload.values()

    def _ulid_exists_in_frontmatter(self, note_id: str) -> bool:
        for current_dir, dirnames, filenames in os.walk(self._vault_root):
            dirnames[:] = sorted(
                name for name in dirnames if name != SIDECAR_DIR_NAME and not name.startswith(".")
            )
            for filename in filenames:
                if not filename.lower().endswith(".md"):
                    continue
                path = Path(current_dir) / filename
                try:
                    raw = path.read_bytes().decode("utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise UlidVerificationError(
                        f"could not verify ULID uniqueness in note {path}"
                    ) from exc
                frontmatter_block = _frontmatter_block(raw)
                if frontmatter_block is None:
                    continue
                match = _FRONTMATTER_ID_PATTERN.search(frontmatter_block)
                if match is not None and match.group(1) == note_id:
                    return True
        return False


def _configured_eol(policy: str) -> str:
    if policy == "lf":
        return "\n"
    if policy == "crlf":
        return "\r\n"
    raise ValueError("line_endings must be 'lf' or 'crlf'")


def _dominant_eol(data: bytes) -> str:
    crlf_count = data.count(b"\r\n")
    lf_count = data.count(b"\n") - crlf_count
    bare_cr_count = data.count(b"\r") - crlf_count
    return "\r\n" if crlf_count > lf_count + bare_cr_count else "\n"


def _check_expected_hash(expected_hash: str | None, current_bytes: bytes | None) -> None:
    if expected_hash is None:
        return
    current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
    if current_hash != expected_hash:
        raise WriteConflictError("note changed since read (hash mismatch); re-read and retry")


def _frontmatter_block(raw: str) -> str | None:
    text = raw[1:] if raw.startswith("\ufeff") else raw
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return None


def _inject(fault_injector: FaultInjector | None, point: str) -> None:
    if fault_injector is not None:
        fault_injector(point)


def _fsync_file(path: Path) -> None:
    # Windows requires a writable handle for os.fsync/FlushFileBuffers.
    with path.open("r+b") as file_handle:
        os.fsync(file_handle.fileno())


def _fsync_directory(path: Path) -> bool:
    return flush_directory_entry(path)


def _lock_windows_file(lock_file: object) -> None:
    import msvcrt  # noqa: PLC0415

    file_handle = lock_file
    if not hasattr(file_handle, "fileno") or not hasattr(file_handle, "seek"):
        raise TypeError("lock file must expose fileno and seek")
    file_handle.seek(0)
    while True:
        try:
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                raise
            time.sleep(_LOCK_RETRY_SECONDS)


def _unlock_windows_file(lock_file: object) -> None:
    import msvcrt  # noqa: PLC0415

    file_handle = lock_file
    if not hasattr(file_handle, "fileno") or not hasattr(file_handle, "seek"):
        raise TypeError("lock file must expose fileno and seek")
    file_handle.seek(0)
    msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)


def _lock_posix_file(lock_file: object) -> None:
    import fcntl  # noqa: PLC0415

    if not hasattr(lock_file, "fileno"):
        raise TypeError("lock file must expose fileno")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_posix_file(lock_file: object) -> None:
    import fcntl  # noqa: PLC0415

    if not hasattr(lock_file, "fileno"):
        raise TypeError("lock file must expose fileno")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _conformance_check(writer: VaultWriter) -> None:
    """Mypy structural conformance: FilesystemVaultWriter satisfies VaultWriter."""
    _ = writer


_conformance_check(FilesystemVaultWriter(Path("."), Settings()))
