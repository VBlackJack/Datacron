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
"""Confined, locked note writes governed by an explicit durability policy."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, final
from uuid import uuid4

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from datacron.core.batch_transaction import (
    BatchApplyResult,
    BatchFaultInjector,
    BatchPrecommitValidator,
    BatchRecoveryOutcome,
    OrganizationBatchTransaction,
)
from datacron.core.config import SIDECAR_DIR_NAME, Settings, VaultConfig
from datacron.core.durability import (
    RecoveryRequiredError,
    UnexpectedRecoveryEntryError,
    WritePolicy,
    atomic_durable_write,
    durable_flush_directory,
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
    assert_vault_rel_path,
    assert_within_write_paths,
    read_ulid_mappings,
    sidecar_dir,
    sidecar_index_db,
    strip_extended_length_prefix,
)
from datacron.core.protocols import VaultWriter
from datacron.core.recovery import (
    BlockedOperation,
    RecoveryRepairAction,
    RecoveryRepairResult,
)
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.security import SecretRedactor
from datacron.core.vault import ULID_SIDECAR_FILENAME
from datacron.core.write_request import ACTIVE_WRITE_REQUEST, ReplayedWriteError
from datacron.organization.manifest import ValidatedOrganizationBundle

__all__ = [
    "FAULT_POINTS",
    "OPERATION_FAULT_POINTS",
    "BatchApplyResult",
    "FilesystemVaultWriter",
    "OperationRecoveryError",
    "RecoveryRepairResult",
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
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCK_RETRY_SECONDS: Final[float] = 0.05
_REASON_PENDING_DISK_HASH_MISMATCH: Final[str] = "pending_disk_hash_mismatch"
_REASON_COMMITTED_DISK_HASH_MISMATCH: Final[str] = "committed_disk_hash_mismatch"
_REASON_REPAIR_DISK_HASH_MISMATCH: Final[str] = "repair_disk_hash_mismatch"


class WriteConflictError(ValueError):
    """Raised when compare-and-swap detects a stale or missing target."""


class UlidCollisionError(ValueError):
    """Raised when a proposed note ULID already exists in the vault identity data."""


class UlidVerificationError(ValueError):
    """Raised when all configured ULID identity sources cannot be verified."""


class OperationRecoveryError(OperationLogError):
    """Raised when a pending operation cannot be reconciled by exact hash."""


@dataclass(frozen=True)
class RecoveryOutcome:
    """Complete result of one recovery scan."""

    recovered: int = 0
    blocked: tuple[BlockedOperation, ...] = ()
    unexpected_entries: tuple[str, ...] = ()


class VaultLockBusyError(RuntimeError):
    """Raised when an advisory lock stays held past the configured timeout.

    Signals contention from another datacron writer process rather than a
    corrupt or unusable vault, so callers may defer the work and retry later.
    """


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
            purge_min_interval_seconds=(settings.operation_history_purge_min_interval_seconds),
        )
        self._batch_transaction = OrganizationBatchTransaction(
            self._vault_root,
            self._operation_journal,
            write_paths=settings.write_paths,
        )
        self._recovery_outcome = RecoveryOutcome()

    @property
    def recovery_blocked(self) -> tuple[BlockedOperation, ...]:
        """Return the blocked operations observed by the latest complete scan."""
        return self._recovery_outcome.blocked

    @property
    def recovery_unexpected_entries(self) -> tuple[str, ...]:
        """Return the recovery-directory entries the latest scan could not classify."""
        return self._recovery_outcome.unexpected_entries

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
        """Run a locked read-CAS-mutate transaction under the durability policy."""
        return await asyncio.to_thread(
            self._mutate_note_atomic_sync,
            rel_path,
            mutation,
            expected_hash,
            operation,
        )

    @contextmanager
    def lock_note_identity(self, rel_path: str, *, expected_hash: str) -> Iterator[None]:
        """Hold cooperative identity and note locks after a fresh byte-level CAS.

        Explicit maintenance uses this synchronous context when sidecar and index
        effects must remain linearized with one unchanged note. It follows the
        writer-wide order: global mutation, identity, confined note, then oplog.
        """
        with self._advisory_lock("mutation"):
            self._write_policy.ensure_writable()
            recovery = self._recover_operations_sync(
                purge_history=False,
                recover_organization_batches=False,
            )
            self._raise_if_recovery_blocked(recovery)
            target, _safe_rel_path = self._resolve_target(rel_path)
            with (
                self._advisory_lock("identity"),
                self._advisory_lock(f"note:{self._lock_key(target)}"),
            ):
                current_bytes = target.read_bytes() if target.is_file() else None
                _check_expected_hash(expected_hash, current_bytes)
                yield

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

    async def get_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Return one durable organization receipt without mutating state."""
        return await asyncio.to_thread(
            self._get_organization_batch_result_sync,
            manifest_sha256,
        )

    async def resolve_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Recover all pending work atomically, then return one committed receipt."""
        self._write_policy.ensure_writable()
        return await asyncio.to_thread(
            self._resolve_organization_batch_result_sync,
            manifest_sha256,
        )

    async def get_organization_removed_identity_ids(
        self,
        result: BatchApplyResult,
    ) -> tuple[str, ...]:
        """Derive stale index identities from a committed sidecar transition."""
        return await asyncio.to_thread(
            self._get_organization_removed_identity_ids_sync,
            result,
        )

    async def apply_organization_manifest(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        precommit_validator: BatchPrecommitValidator,
        operation: OperationContext,
        fault_injector: BatchFaultInjector | None = None,
    ) -> BatchApplyResult:
        """Apply one validated organization bundle under the global mutation lock."""
        return await asyncio.to_thread(
            self._apply_organization_manifest_sync,
            bundle,
            confirmation_token,
            projected_report_sha256,
            precommit_validator,
            operation,
            fault_injector,
        )

    async def validate_organization_manifest_capacity(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> None:
        """Prove the future durable receipt is bounded without writing state."""
        await asyncio.to_thread(
            self._validate_organization_manifest_capacity_sync,
            bundle,
            confirmation_token,
            projected_report_sha256,
            operation,
        )

    async def has_pending_organization_batches(self) -> bool:
        """Return whether organization batch recovery is pending."""
        return await asyncio.to_thread(self._has_pending_organization_batches_sync)

    def _has_pending_organization_batches_sync(self) -> bool:
        try:
            return self._batch_transaction.has_pending_batches()
        except UnexpectedRecoveryEntryError as exc:
            self._recovery_outcome = RecoveryOutcome(unexpected_entries=exc.entries)
            raise

    async def recover_operations(self) -> int:
        """Resolve durable pending manifests before serving or writing."""
        self._write_policy.ensure_writable()
        outcome = await asyncio.to_thread(self._recover_operations_guarded_sync)
        return outcome.recovered

    async def inspect_recovery(self) -> tuple[BlockedOperation, ...]:
        """Inspect blocked operation manifests without changing durable state."""
        return await asyncio.to_thread(self._inspect_recovery_sync)

    async def repair_recovery(
        self,
        operation_id: str,
        action: RecoveryRepairAction,
        *,
        expected_disk_hash: str,
        actor: str,
    ) -> RecoveryRepairResult:
        """Apply one exact-hash recovery repair after explicit CLI confirmation."""
        self._write_policy.ensure_writable()
        return await asyncio.to_thread(
            self._repair_recovery_guarded_sync,
            operation_id,
            action,
            expected_disk_hash,
            actor,
        )

    async def list_operations(self) -> list[OperationRecord]:
        """Return an immutable snapshot of committed operation records."""
        return await asyncio.to_thread(self._list_operations_sync)

    async def present_history_hashes(self, hashes: Iterable[str | None]) -> set[str]:
        """Return which of these restore points still have their bytes on disk."""
        return await asyncio.to_thread(self._present_history_hashes_sync, list(hashes))

    def _present_history_hashes_sync(self, hashes: list[str | None]) -> set[str]:
        journal = self._operation_journal
        return {
            content_hash
            for content_hash in dict.fromkeys(hashes)
            if content_hash is not None and journal.has_history(content_hash)
        }

    async def purge_history(self) -> list[str]:
        """Apply the configured content-history retention policy now."""
        self._write_policy.ensure_writable()
        return await asyncio.to_thread(self._purge_history_guarded_sync)

    def _apply_organization_manifest_sync(
        self,
        bundle: ValidatedOrganizationBundle,
        confirmation_token: str,
        projected_report_sha256: str,
        precommit_validator: BatchPrecommitValidator,
        operation: OperationContext,
        fault_injector: BatchFaultInjector | None,
    ) -> BatchApplyResult:
        self._write_policy.ensure_writable()
        with self._advisory_lock("mutation"):
            self._batch_transaction.validate_journal_roots()
            recovery = self._recover_operations_sync(purge_history=False)
            self._raise_if_recovery_blocked(recovery)
            sanitized = self._sanitize_organization_operation(operation)
            with self._advisory_lock("oplog"):
                result = self._batch_transaction.apply(
                    bundle,
                    confirmation_token=confirmation_token,
                    projected_report_sha256=projected_report_sha256,
                    precommit_validator=precommit_validator,
                    operation=sanitized,
                    fault_injector=fault_injector,
                )
                self._operation_journal.purge_history()
            return result

    def _validate_organization_manifest_capacity_sync(
        self,
        bundle: ValidatedOrganizationBundle,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> None:
        self._write_policy.ensure_writable()
        self._batch_transaction.validate_capacity(
            bundle,
            confirmation_token=confirmation_token,
            projected_report_sha256=projected_report_sha256,
            operation=self._sanitize_organization_operation(operation),
        )

    def _sanitize_organization_operation(self, operation: OperationContext) -> OperationContext:
        return OperationContext(
            op=operation.op,
            tool=operation.tool,
            actor=(
                self._secret_redactor.redact_text(operation.actor.strip())
                or "mcp-client:unidentified"
            ),
            parameters={
                key: (self._secret_redactor.redact_text(value) if isinstance(value, str) else value)
                for key, value in operation.parameters.items()
            },
        )

    def _get_organization_batch_result_sync(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        with self._advisory_lock("oplog"):
            return self._batch_transaction.get_result(manifest_sha256)

    def _resolve_organization_batch_result_sync(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        self._write_policy.ensure_writable()
        with self._advisory_lock("mutation"):
            recovery = self._recover_operations_sync()
            self._raise_if_recovery_blocked(recovery)
            with self._advisory_lock("oplog"):
                return self._batch_transaction.get_result(manifest_sha256)

    def _get_organization_removed_identity_ids_sync(
        self,
        result: BatchApplyResult,
    ) -> tuple[str, ...]:
        with self._advisory_lock("oplog"):
            return self._batch_transaction.removed_identity_ids(result)

    def _recover_operations_guarded_sync(self) -> RecoveryOutcome:
        with self._advisory_lock("mutation"):
            return self._recover_operations_sync()

    def _repair_recovery_guarded_sync(
        self,
        operation_id: str,
        action: RecoveryRepairAction,
        expected_disk_hash: str,
        actor: str,
    ) -> RecoveryRepairResult:
        with self._advisory_lock("mutation"):
            return self._repair_recovery_sync(
                operation_id,
                action,
                expected_disk_hash,
                actor,
            )

    def _purge_history_guarded_sync(self) -> list[str]:
        with self._advisory_lock("mutation"):
            return self._purge_history_sync()

    def _write_note_atomic_sync(
        self,
        rel_path: str,
        content: str,
        overwrite: bool,
        expected_hash: str | None,
        note_id: str | None,
        operation: OperationContext | None,
    ) -> str:
        with self._advisory_lock("mutation"):
            return self._write_note_atomic_locked_sync(
                rel_path,
                content,
                overwrite,
                expected_hash,
                note_id,
                operation,
            )

    def _write_note_atomic_locked_sync(
        self,
        rel_path: str,
        content: str,
        overwrite: bool,
        expected_hash: str | None,
        note_id: str | None,
        operation: OperationContext | None,
    ) -> str:
        self._write_policy.ensure_writable()
        recovery = self._recover_operations_sync(
            purge_history=False,
            recover_organization_batches=False,
        )
        self._raise_if_recovery_blocked(recovery)
        target, safe_rel_path = self._resolve_target(rel_path)
        if note_id is not None and not _ULID_PATTERN.fullmatch(note_id):
            raise ValueError("note_id must be a canonical 26-character ULID")

        if expected_hash is not None and not overwrite:
            # A creation has nothing to match: the note is absent, so the hash is
            # compared against None and can only mismatch. The MCP tool no longer
            # advertises the parameter; this refuses it by name for any other
            # caller rather than reporting that a path which never existed
            # "changed since read".
            raise ValueError("a creation takes no expected_hash; there is nothing to match")

        identity_lock = self._advisory_lock("identity") if note_id is not None else nullcontext()
        with identity_lock, self._advisory_lock(f"note:{self._lock_key(target)}"):
            current_bytes = target.read_bytes() if target.exists() else None
            self._check_request_replay(
                safe_rel_path, current_bytes, expected_hash, expected_hash_accepted=overwrite
            )
            _check_expected_hash(expected_hash, current_bytes)
            if current_bytes is not None and not overwrite:
                raise FileExistsError(f"{safe_rel_path} already exists.")
            if expected_hash is None:
                self._check_committed_baseline(safe_rel_path, current_bytes)
            if note_id is not None and self._ulid_exists(note_id):
                raise UlidCollisionError(f"ULID collision: {note_id} already exists")

            target.parent.mkdir(parents=True, exist_ok=True)
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
            return self._write_without_operation_sync(target, current_bytes, emitted)

    def _mutate_note_atomic_sync(
        self,
        rel_path: str,
        mutation: NoteMutation,
        expected_hash: str | None,
        operation: OperationContext | None,
    ) -> str:
        with self._advisory_lock("mutation"):
            return self._mutate_note_atomic_locked_sync(
                rel_path,
                mutation,
                expected_hash,
                operation,
            )

    def _mutate_note_atomic_locked_sync(
        self,
        rel_path: str,
        mutation: NoteMutation,
        expected_hash: str | None,
        operation: OperationContext | None,
    ) -> str:
        self._write_policy.ensure_writable()
        recovery = self._recover_operations_sync(
            purge_history=False,
            recover_organization_batches=False,
        )
        self._raise_if_recovery_blocked(recovery)
        target, safe_rel_path = self._resolve_target(rel_path)
        with self._advisory_lock(f"note:{self._lock_key(target)}"):
            current_bytes = target.read_bytes() if target.is_file() else None
            self._check_request_replay(safe_rel_path, current_bytes, expected_hash)
            _check_expected_hash(expected_hash, current_bytes)
            if expected_hash is None:
                self._check_committed_baseline(safe_rel_path, current_bytes)
            if current_bytes is None:
                raise FileNotFoundError(
                    f"note not found at {safe_rel_path.as_posix()}; use create_note_ai"
                )
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
            return self._write_without_operation_sync(target, current_bytes, emitted)

    def _revert_note_atomic_sync(
        self,
        rel_path: str,
        to_hash: str,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        with self._advisory_lock("mutation"):
            return self._revert_note_atomic_locked_sync(
                rel_path,
                to_hash,
                expected_hash,
                operation,
            )

    def _revert_note_atomic_locked_sync(
        self,
        rel_path: str,
        to_hash: str,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        self._write_policy.ensure_writable()
        recovery = self._recover_operations_sync(
            purge_history=False,
            recover_organization_batches=False,
        )
        self._raise_if_recovery_blocked(recovery)
        target, safe_rel_path = self._resolve_target(rel_path)
        with self._advisory_lock(f"note:{self._lock_key(target)}"):
            current_bytes = target.read_bytes() if target.is_file() else None
            self._check_request_replay(safe_rel_path, current_bytes, expected_hash)
            _check_expected_hash(expected_hash, current_bytes)
            if expected_hash is None:
                self._check_committed_baseline(safe_rel_path, current_bytes)
            if current_bytes is None:
                raise FileNotFoundError(f"note not found at {safe_rel_path.as_posix()}")
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

    def _check_request_replay(
        self,
        safe_rel_path: Path,
        current_bytes: bytes | None,
        expected_hash: str | None,
        *,
        expected_hash_accepted: bool = True,
    ) -> None:
        """Check durable receipts under the mutation lock, after recovery and confinement.

        A receipt alone does not mean this call is a retry. It used to: any record
        carrying the request key made the write a replay, and the caller was handed
        the old receipt with ``committed: true`` while nothing happened. Revert a note
        and reissue the same call, which is what ``prepare_follow_up`` does by design
        because it derives the request id from the plan, and the entry was silently
        dropped. That is a write the caller was told had landed and which is nowhere.

        So the receipt is read against the note as it is now:

        - the note still holds what the record produced, so this is the ordinary
          retry, after a timeout, a crash or a concurrent duplicate. Replay it.
        - the note has moved and the caller supplied ``expected_hash``. Let the write
          proceed and let CAS judge it: a reverted note matches and is written again,
          a stale retry raises a conflict.
        - the note has moved and the caller supplied no ``expected_hash``. Nothing
          left can tell a retry from a new write, so refuse rather than guess. The
          old answer fabricated a receipt naming bytes no longer on disk; the other
          guess would append the entry twice.

        The lookup takes the latest record for the key, not the first. A reverted
        and rewritten request has more than one, and only the last describes the
        note; it also lets a retry be answered from the journal's tail rather than
        from a parse of its whole history.
        """
        request = ACTIVE_WRITE_REQUEST.get()
        if request is None:
            return
        with self._advisory_lock("oplog"):
            record = self._operation_journal.latest_record_for_request_key(request.key_hash)
        if record is None:
            return
        if (
            record.parameters.get("request_fingerprint") != request.fingerprint
            or record.rel_path != safe_rel_path.as_posix()
        ):
            raise WriteConflictError("request_id was already used with different arguments")
        current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
        if current_hash != record.after_hash:
            if expected_hash is None:
                if not expected_hash_accepted:
                    # Telling a creation to retry with an exact expected_hash sent
                    # it somewhere it cannot go: the hash is then matched against
                    # an absent note and refused in turn. Reverting a note and
                    # recreating it under the same derived request id was
                    # unrecoverable, and prepare_follow_up derives that id by design.
                    raise WriteConflictError(
                        "request_id was already committed and the note is no longer what it "
                        "wrote; creating it again needs a new request_id"
                    )
                # Retrying the same request_id with an expected_hash cannot work:
                # the request fingerprint includes expected_hash, and this call
                # matched the fingerprint without one, so adding it reads as a
                # different set of arguments. Only a new request_id can proceed.
                raise WriteConflictError(
                    "request_id was already committed and the note has changed since; "
                    "re-read the note, then write again with a new request_id and an "
                    "exact expected_hash"
                )
            return
        request.record = record
        raise ReplayedWriteError(record)

    def _check_committed_baseline(
        self,
        safe_rel_path: Path,
        current_bytes: bytes | None,
    ) -> None:
        """Fail closed when disk diverges from the latest committed path state."""
        with self._advisory_lock("oplog"):
            baseline = self._operation_journal.latest_record_for_path(safe_rel_path.as_posix())
        if baseline is None:
            return
        current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
        if current_hash != baseline.after_hash:
            raise WriteConflictError(
                "note changed outside Datacron since the last committed operation; "
                "re-read and retry with exact expected_hash"
            )

    def _write_without_operation_sync(
        self,
        target: Path,
        before_bytes: bytes | None,
        after_bytes: bytes,
    ) -> str:
        """Write unlogged content, then apply retention without deleting its backup."""
        preserved: set[str] = set()
        if before_bytes is not None:
            preserved.add(self._operation_journal.store_history(before_bytes))
        with self._advisory_lock("oplog"):
            self._operation_journal.purge_history(preserve_hashes=preserved)
        return atomic_durable_write(target, after_bytes)

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
        request = ACTIVE_WRITE_REQUEST.get()
        request_parameters = (
            {}
            if request is None
            else {
                "request_key_hash": request.key_hash,
                "request_fingerprint": request.fingerprint,
            }
        )
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
                **{
                    key: self._secret_redactor.redact_text(value)
                    if isinstance(value, str)
                    else value
                    for key, value in operation.parameters.items()
                },
                **request_parameters,
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
        try:
            written_hash = atomic_durable_write(target, after_bytes)
        except OSError:
            self._discard_unstarted_pending(record, target, after_hash)
            raise
        if written_hash != after_hash:
            raise OperationLogError("durable note hash differs from prepared after_hash")
        _inject(self._operation_fault_injector, "after_note_write")
        self._operation_journal.append_record(record)
        _inject(self._operation_fault_injector, "after_oplog_write")
        self._operation_journal.remove_pending(record.operation_id)
        if request is not None:
            request.record = record
        _inject(self._operation_fault_injector, "after_pending_cleanup")
        removed = self._purge_history_after_commit()
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

    def _purge_history_after_commit(self) -> list[str]:
        """Sweep expired history, without letting the sweep fail a committed write.

        By this point the note, the journal record and the pending cleanup are all
        durable, and the caller is about to be told the write succeeded. Retention is
        housekeeping: a failure here means blobs were kept that could have gone, which
        is the safe direction. Raising instead reported a committed write as failed,
        and because the journal only records the sweep as done once it finishes, the
        next write repeated it and failed again, indefinitely.
        """
        try:
            return self._operation_journal.purge_history()
        except (OperationLogError, OSError) as exc:
            _LOGGER.warning("History retention sweep failed after a committed write: %s", exc)
            return []

    def _recover_operations_sync(
        self,
        *,
        purge_history: bool = True,
        recover_organization_batches: bool = True,
    ) -> RecoveryOutcome:
        """Resolve pending work, recording any entry that blocks it for health to report.

        An entry recovery cannot classify refuses the scan with a typed
        ``recovery_required`` error. It is recorded here first, so ``get_health``
        and startup can name it without a write having to fail on it again.
        """
        try:
            with self._advisory_lock("oplog"):
                self._operation_journal.repair_torn_tail()
            return self._recover_operations_scan_sync(
                purge_history=purge_history,
                recover_organization_batches=recover_organization_batches,
            )
        except UnexpectedRecoveryEntryError as exc:
            self._recovery_outcome = RecoveryOutcome(unexpected_entries=exc.entries)
            raise

    def _recover_operations_scan_sync(
        self,
        *,
        purge_history: bool,
        recover_organization_batches: bool,
    ) -> RecoveryOutcome:
        batch_outcome = self._recover_organization_batches_sync(
            allowed=recover_organization_batches
        )
        if batch_outcome.blocked:
            outcome = RecoveryOutcome(blocked=batch_outcome.blocked)
            self._recovery_outcome = outcome
            return outcome
        recovered = batch_outcome.recovered
        blocked: list[BlockedOperation] = []
        pending_paths = self._operation_journal.pending_paths()
        pending_paths.sort(key=self._recovery_order)
        for pending_path in pending_paths:
            record, pending_snapshot = self._operation_journal.read_pending_snapshot(pending_path)
            candidate = (self._vault_root / record.rel_path).expanduser().resolve()
            safe_rel_path = self._safe_relative_path(candidate)
            with (
                self._advisory_lock(f"note:{self._lock_key(candidate)}"),
                self._advisory_lock("oplog"),
            ):
                current_path = self._operation_journal.pending_path(record.operation_id)
                current_record, current_snapshot = self._operation_journal.read_pending_snapshot(
                    current_path
                )
                if current_snapshot != pending_snapshot or current_record != record:
                    raise OperationLogError(
                        f"pending operation changed during recovery: {record.operation_id}"
                    )
                record = current_record
                current_bytes = candidate.read_bytes() if candidate.is_file() else None
                current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
                records = self._operation_journal.read_records()
                resolution = self._resolution_record(records, record.operation_id)
                if resolution is not None:
                    if current_hash == resolution.after_hash:
                        self._operation_journal.remove_pending(record.operation_id)
                        continue
                    blocked.append(
                        self._blocked_operation(
                            record,
                            safe_rel_path,
                            reason=_REASON_REPAIR_DISK_HASH_MISMATCH,
                            disk_hash=current_hash,
                        )
                    )
                    continue
                if any(item.operation_id == record.operation_id for item in records):
                    if current_hash != record.after_hash:
                        blocked.append(
                            self._blocked_operation(
                                record,
                                safe_rel_path,
                                reason=_REASON_COMMITTED_DISK_HASH_MISMATCH,
                                disk_hash=current_hash,
                            )
                        )
                        continue
                elif current_hash == record.after_hash:
                    self._operation_journal.append_record(
                        self._operation_journal.restamped_past_tail(record)
                    )
                    recovered += 1
                    _LOGGER.warning(
                        "recovered committed operation id=%s rel_path=%s after_hash=%s",
                        record.operation_id,
                        record.rel_path,
                        record.after_hash,
                    )
                elif current_hash != record.before_hash:
                    blocked.append(
                        self._blocked_operation(
                            record,
                            safe_rel_path,
                            reason=_REASON_PENDING_DISK_HASH_MISMATCH,
                            disk_hash=current_hash,
                        )
                    )
                    continue
                self._operation_journal.remove_pending(record.operation_id)
        outcome = RecoveryOutcome(recovered=recovered, blocked=tuple(blocked))
        self._recovery_outcome = outcome
        if purge_history and not blocked:
            with self._advisory_lock("oplog"):
                self._operation_journal.purge_history()
        return outcome

    def _recover_organization_batches_sync(
        self,
        *,
        allowed: bool,
    ) -> BatchRecoveryOutcome:
        """Recover global batches only through an explicitly canonical caller."""
        with self._advisory_lock("oplog"):
            if allowed:
                return self._batch_transaction.recover()
            if self._batch_transaction.has_pending_batches():
                raise RecoveryRequiredError(
                    "Recovery required: pending organization batches must be recovered "
                    "explicitly through the canonical single-tenant vault scope"
                )
            return BatchRecoveryOutcome()

    def _inspect_recovery_sync(self) -> tuple[BlockedOperation, ...]:
        """Report blocked operations without changing durable state.

        Under the oplog lock, like every other reader of the journal. It was the
        one that took no lock at all, and the promise in its own name was what made
        that dangerous: reading the journal can rewrite it, because loading the tail
        migrates a format-version-1 log in place, and that rewrite replaces the whole
        file. Run against a live server it could swap in content predating an append
        the server had just made inside its own lock, losing a committed record while
        the CLI printed that no changes were made. Unlocked it could also read the
        journal mid-append, or a pending manifest the server was unlinking.
        """
        with self._advisory_lock("oplog"):
            return self._inspect_recovery_locked_sync()

    def _inspect_recovery_locked_sync(self) -> tuple[BlockedOperation, ...]:
        """Classify every pending manifest, collecting entries that are not one.

        An entry recovery cannot classify is recorded and the inspection goes on,
        so the operator sees every stray file and every blocked operation in one
        run instead of the first refusal only.
        """
        unexpected: list[str] = []
        try:
            blocked: list[BlockedOperation] = list(self._batch_transaction.inspect())
        except UnexpectedRecoveryEntryError as exc:
            blocked = []
            unexpected.extend(exc.entries)
        records = self._operation_journal.read_records()
        pending_paths, stray = self._operation_journal.scan_pending()
        unexpected.extend(stray)
        for pending_path in pending_paths:
            manifest_before = pending_path.read_bytes()
            try:
                record = self._operation_journal.read_pending(pending_path)
            except UnexpectedRecoveryEntryError as exc:
                unexpected.extend(exc.entries)
                continue
            candidate = (self._vault_root / record.rel_path).expanduser().resolve()
            safe_rel_path = self._safe_relative_path(candidate)
            current_bytes = candidate.read_bytes() if candidate.is_file() else None
            current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
            if not pending_path.is_file() or pending_path.read_bytes() != manifest_before:
                raise VaultLockBusyError(
                    f"pending operation changed during inspection: {record.operation_id}"
                )
            resolution = self._resolution_record(records, record.operation_id)
            if resolution is not None:
                if current_hash != resolution.after_hash:
                    blocked.append(
                        self._blocked_operation(
                            record,
                            safe_rel_path,
                            reason=_REASON_REPAIR_DISK_HASH_MISMATCH,
                            disk_hash=current_hash,
                            log_error=False,
                        )
                    )
                continue
            if any(item.operation_id == record.operation_id for item in records):
                if current_hash != record.after_hash:
                    blocked.append(
                        self._blocked_operation(
                            record,
                            safe_rel_path,
                            reason=_REASON_COMMITTED_DISK_HASH_MISMATCH,
                            disk_hash=current_hash,
                            log_error=False,
                        )
                    )
                continue
            if current_hash not in {record.before_hash, record.after_hash}:
                blocked.append(
                    self._blocked_operation(
                        record,
                        safe_rel_path,
                        reason=_REASON_PENDING_DISK_HASH_MISMATCH,
                        disk_hash=current_hash,
                        log_error=False,
                    )
                )
        self._recovery_outcome = RecoveryOutcome(
            blocked=tuple(blocked),
            unexpected_entries=tuple(sorted(unexpected)),
        )
        return tuple(blocked)

    def _repair_recovery_sync(
        self,
        operation_id: str,
        action: RecoveryRepairAction,
        expected_disk_hash: str,
        actor: str,
    ) -> RecoveryRepairResult:
        cleaned_operation_id = self._validate_repair_request(
            operation_id,
            action,
            expected_disk_hash,
        )
        pending_path = self._operation_journal.pending_path(cleaned_operation_id)
        if not pending_path.is_file():
            raise FileNotFoundError(f"pending operation not found: {cleaned_operation_id}")
        initial = self._operation_journal.read_pending(pending_path)
        candidate = (self._vault_root / initial.rel_path).expanduser().resolve()
        safe_rel_path = self._safe_relative_path(candidate)
        with (
            self._advisory_lock(f"note:{self._lock_key(candidate)}"),
            self._advisory_lock("oplog"),
        ):
            original, current_bytes, current_hash, blocked = self._load_repair_state(
                pending_path,
                initial,
                candidate,
                safe_rel_path,
                expected_disk_hash,
            )
            after_bytes = self._repair_after_bytes(action, original, blocked, current_bytes)
            repair = self._build_repair_record(
                action,
                actor,
                original,
                blocked,
                safe_rel_path,
                current_hash,
                after_bytes,
            )
            self._commit_repair(
                action,
                candidate,
                current_bytes,
                after_bytes,
                repair,
                original.operation_id,
            )
            _LOGGER.warning(
                "operation recovery repaired id=%s repair_id=%s action=%s rel_path=%s "
                "before_hash=%s after_hash=%s actor=%s",
                original.operation_id,
                repair.operation_id,
                action,
                safe_rel_path.as_posix(),
                current_hash,
                repair.after_hash,
                repair.actor,
            )
        self._recovery_outcome = RecoveryOutcome()
        return RecoveryRepairResult(
            operation_id=cleaned_operation_id,
            repair_operation_id=repair.operation_id,
            rel_path=safe_rel_path.as_posix(),
            action=action,
            before_hash=current_hash,
            after_hash=repair.after_hash,
        )

    @staticmethod
    def _validate_repair_request(
        operation_id: str,
        action: RecoveryRepairAction,
        expected_disk_hash: str,
    ) -> str:
        cleaned_operation_id = operation_id.strip()
        if not _OPERATION_ID_PATTERN.fullmatch(cleaned_operation_id):
            raise ValueError("operation_id must be an opaque filename-safe identifier")
        if action not in {"restore-before", "adopt-disk"}:
            raise ValueError("action must be restore-before or adopt-disk")
        if not _CONTENT_HASH_PATTERN.fullmatch(expected_disk_hash):
            raise ValueError("expected_disk_hash must be a lowercase 64-character SHA-256")
        return cleaned_operation_id

    def _load_repair_state(
        self,
        pending_path: Path,
        initial: OperationRecord,
        candidate: Path,
        safe_rel_path: Path,
        expected_disk_hash: str,
    ) -> tuple[OperationRecord, bytes, str, BlockedOperation]:
        if not pending_path.is_file():
            raise FileNotFoundError(f"pending operation not found: {initial.operation_id}")
        original = self._operation_journal.read_pending(pending_path)
        if original.rel_path != initial.rel_path:
            raise VaultLockBusyError(
                f"pending operation changed during repair: {initial.operation_id}"
            )
        if not candidate.is_file():
            raise ValueError("repair requires the target note to exist")
        current_bytes = candidate.read_bytes()
        current_hash = sha256_bytes(current_bytes)
        if current_hash != expected_disk_hash:
            raise WriteConflictError(
                "disk hash changed since inspection: "
                f"expected {expected_disk_hash}, actual {current_hash}"
            )
        blocked = self._classify_blocked_operation(
            original,
            safe_rel_path,
            current_hash,
            self._operation_journal.read_records(),
            log_error=False,
        )
        if blocked is None:
            raise ValueError(f"operation is no longer blocked: {original.operation_id}")
        return original, current_bytes, current_hash, blocked

    def _repair_after_bytes(
        self,
        action: RecoveryRepairAction,
        original: OperationRecord,
        blocked: BlockedOperation,
        current_bytes: bytes,
    ) -> bytes:
        if action == "adopt-disk":
            if not blocked.adopt_disk_available:
                raise ValueError("adopt-disk is unavailable because the target is absent")
            return current_bytes
        if not blocked.restore_before_available or original.before_hash is None:
            raise HistoryUnavailableError(
                "restore-before is unavailable because exact before history is missing"
            )
        return self._operation_journal.read_history(original.before_hash)

    def _build_repair_record(
        self,
        action: RecoveryRepairAction,
        actor: str,
        original: OperationRecord,
        blocked: BlockedOperation,
        safe_rel_path: Path,
        current_hash: str,
        after_bytes: bytes,
    ) -> OperationRecord:
        return OperationRecord(
            operation_id=uuid4().hex,
            timestamp=self._operation_journal.next_timestamp(),
            op="recovery_restore" if action == "restore-before" else "recovery_adopt",
            tool="datacron_ops_repair",
            note_id=original.note_id,
            rel_path=safe_rel_path.as_posix(),
            before_hash=current_hash,
            after_hash=sha256_bytes(after_bytes),
            actor=self._secret_redactor.redact_text(actor.strip()) or "cli:local-operator",
            parameters={
                "action": action,
                "blocked_reason": blocked.reason,
                "original_after_hash": original.after_hash,
                "original_before_hash": original.before_hash,
                "original_op": original.op,
                "original_tool": original.tool,
                "resolves_operation_id": original.operation_id,
            },
            history_stored=(action == "restore-before" and self._operation_journal.history_enabled),
        )

    def _commit_repair(
        self,
        action: RecoveryRepairAction,
        candidate: Path,
        current_bytes: bytes,
        after_bytes: bytes,
        repair: OperationRecord,
        original_operation_id: str,
    ) -> None:
        if action == "restore-before":
            stored_hash = self._operation_journal.store_history(current_bytes)
            if stored_hash != repair.before_hash:
                raise OperationLogError("stored repair history differs from disk hash")
        self._operation_journal.write_pending(repair)
        if action == "restore-before":
            written_hash = atomic_durable_write(candidate, after_bytes)
            if written_hash != repair.after_hash:
                raise OperationLogError("repair note hash differs from prepared after_hash")
        self._operation_journal.append_record(repair)
        self._operation_journal.remove_pending(repair.operation_id)
        self._operation_journal.remove_pending(original_operation_id)

    def _list_operations_sync(self) -> list[OperationRecord]:
        with self._advisory_lock("oplog"):
            return self._operation_journal.read_records()

    def _purge_history_sync(self) -> list[str]:
        recovery = self._recover_operations_sync(
            purge_history=False,
            recover_organization_batches=False,
        )
        self._raise_if_recovery_blocked(recovery)
        with self._advisory_lock("oplog"):
            return self._operation_journal.purge_history()

    def _blocked_operation(
        self,
        record: OperationRecord,
        safe_rel_path: Path,
        *,
        reason: str,
        disk_hash: str | None,
        log_error: bool = True,
    ) -> BlockedOperation:
        blocked = BlockedOperation(
            operation_id=record.operation_id,
            rel_path=safe_rel_path.as_posix(),
            reason=reason,
            expected_before_hash=record.before_hash,
            expected_after_hash=record.after_hash,
            disk_hash=disk_hash,
            restore_before_available=self._history_available(record.before_hash),
            adopt_disk_available=disk_hash is not None,
        )
        if log_error:
            _LOGGER.error(
                "operation recovery blocked id=%s rel_path=%s reason=%s "
                "expected_before_hash=%s expected_after_hash=%s disk_hash=%s",
                blocked.operation_id,
                blocked.rel_path,
                blocked.reason,
                blocked.expected_before_hash,
                blocked.expected_after_hash,
                blocked.disk_hash,
            )
        return blocked

    def _classify_blocked_operation(
        self,
        record: OperationRecord,
        safe_rel_path: Path,
        disk_hash: str | None,
        records: list[OperationRecord],
        *,
        log_error: bool = True,
    ) -> BlockedOperation | None:
        resolution = self._resolution_record(records, record.operation_id)
        if resolution is not None:
            if disk_hash == resolution.after_hash:
                return None
            return self._blocked_operation(
                record,
                safe_rel_path,
                reason=_REASON_REPAIR_DISK_HASH_MISMATCH,
                disk_hash=disk_hash,
                log_error=log_error,
            )
        if any(item.operation_id == record.operation_id for item in records):
            if disk_hash == record.after_hash:
                return None
            return self._blocked_operation(
                record,
                safe_rel_path,
                reason=_REASON_COMMITTED_DISK_HASH_MISMATCH,
                disk_hash=disk_hash,
                log_error=log_error,
            )
        if disk_hash in {record.before_hash, record.after_hash}:
            return None
        return self._blocked_operation(
            record,
            safe_rel_path,
            reason=_REASON_PENDING_DISK_HASH_MISMATCH,
            disk_hash=disk_hash,
            log_error=log_error,
        )

    def _history_available(self, content_hash: str | None) -> bool:
        if content_hash is None:
            return False
        try:
            self._operation_journal.read_history(content_hash)
        except HistoryUnavailableError:
            return False
        return True

    @staticmethod
    def _resolution_record(
        records: list[OperationRecord],
        operation_id: str,
    ) -> OperationRecord | None:
        for record in reversed(records):
            if record.parameters.get("resolves_operation_id") == operation_id:
                return record
        return None

    def _recovery_order(self, pending_path: Path) -> tuple[int, str]:
        record = self._operation_journal.read_pending(pending_path)
        resolves = record.parameters.get("resolves_operation_id")
        return (0 if isinstance(resolves, str) and resolves else 1, pending_path.name)

    @staticmethod
    def _raise_if_recovery_blocked(outcome: RecoveryOutcome) -> None:
        if not outcome.blocked:
            return
        count = len(outcome.blocked)
        noun = "operation" if count == 1 else "operations"
        raise RecoveryRequiredError(
            f"Recovery required: {count} blocked {noun}; "
            f"first operation_id={outcome.blocked[0].operation_id}"
        )

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
        sidecar_path = sidecar_dir(self._vault_root) / ULID_SIDECAR_FILENAME
        if not sidecar_path.is_file():
            return None
        try:
            payload = read_ulid_mappings(
                sidecar_path,
                require_string_pairs=True,
                invalid_object_is_empty=True,
            )
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return payload.get(rel_path.as_posix())

    def _discard_unstarted_pending(
        self,
        record: OperationRecord,
        target: Path,
        after_hash: str,
    ) -> None:
        """Drop a pending record whose note write never landed.

        A synchronous failure leaves this process alive, so recovery has nothing
        to finish. Keeping the record blocks every later write to the vault,
        because recovery runs first and re-resolves the recorded path. The record
        is kept when the target already carries the prepared bytes, since the
        failure then happened after the replacement and the operation is still
        recovery's to complete.
        """
        try:
            if target.is_file() and sha256_bytes(target.read_bytes()) == after_hash:
                return
        except OSError:
            _LOGGER.warning(
                "Could not inspect %s after a failed note write; keeping pending record %s.",
                target,
                record.operation_id,
            )
            return
        self._operation_journal.remove_pending(record.operation_id)
        _LOGGER.warning(
            "Discarded pending record %s: the note write to %s never landed.",
            record.operation_id,
            record.rel_path,
        )

    def _resolve_target(self, rel_path: str) -> tuple[Path, Path]:
        assert_vault_rel_path(rel_path)
        candidate = (self._vault_root / rel_path).expanduser().resolve()
        target = assert_within_write_paths(candidate, self._settings)
        return target, self._safe_relative_path(target)

    def _safe_relative_path(self, target: Path) -> Path:
        plain_target = strip_extended_length_prefix(target)
        try:
            return plain_target.relative_to(strip_extended_length_prefix(self._vault_root))
        except ValueError as exc:
            raise PathConfinementError(
                f"Path {plain_target} is outside the bound vault root {self._vault_root}."
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
        lock_dir = assert_path_chain_without_links(
            sidecar_dir(self._vault_root) / "locks",
            anchor=self._vault_root,
            allow_missing=True,
        )
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_dir = assert_path_chain_without_links(
            lock_dir,
            anchor=self._vault_root,
            allow_missing=False,
        )
        lock_name = f"{sha256_bytes(key.encode('utf-8'))}.lock"
        lock_path = assert_path_chain_without_links(
            lock_dir / lock_name,
            anchor=self._vault_root,
            allow_missing=True,
        )
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\x00")
                lock_file.flush()
            _lock_file(lock_file, key, self._settings.vault_lock_timeout_seconds)
            try:
                yield
            finally:
                _unlock_file(lock_file)

    @staticmethod
    def _lock_key(target: Path) -> str:
        return os.path.normcase(str(target))

    def _ulid_exists(self, note_id: str) -> bool:
        """Return whether an authoritative identity source contains ``note_id``.

        Reconciliation updates the index after each write, while the writer-owned
        sidecar persists assigned identities. If either authority is present, its
        absence answer is final; a full frontmatter walk is reserved for a vault with
        neither source yet. This avoids paying an O(vault) scan for the approximately
        2^-80 collision risk of every freshly generated ULID.
        """
        authority_consulted = False
        db_path = sidecar_index_db(self._vault_root)
        if db_path.is_file():
            authority_consulted = True
            if self._ulid_exists_in_index(note_id):
                return True
        sidecar_path = sidecar_dir(self._vault_root) / ULID_SIDECAR_FILENAME
        if sidecar_path.is_file():
            authority_consulted = True
            if self._ulid_exists_in_sidecar(note_id):
                return True
        if authority_consulted:
            return False
        return self._ulid_exists_in_frontmatter(note_id)

    def _ulid_exists_in_index(self, note_id: str) -> bool:
        db_path = sidecar_index_db(self._vault_root)
        if not db_path.is_file():
            return False
        try:
            with sqlite3.connect(
                db_path, timeout=self._settings.vault_lock_timeout_seconds
            ) as connection:
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
        sidecar_path = sidecar_dir(self._vault_root) / ULID_SIDECAR_FILENAME
        if not sidecar_path.is_file():
            return False
        try:
            payload = read_ulid_mappings(
                sidecar_path,
                require_string_pairs=True,
                invalid_object_is_empty=True,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise UlidVerificationError(
                f"could not verify ULID uniqueness in sidecar {sidecar_path}"
            ) from exc
        return note_id in payload.values()

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
                    _LOGGER.warning(
                        "Skipping unreadable note during fallback ULID scan path=%s error=%s",
                        path,
                        exc,
                    )
                    continue
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


def _raise_vault_lock_busy(key: str, timeout_seconds: float, cause: OSError) -> NoReturn:
    """Log advisory-lock contention and raise a typed, bounded-timeout error."""
    _LOGGER.warning(
        "Vault advisory lock %r still held after %.1fs; another datacron writer is holding it",
        key,
        timeout_seconds,
    )
    raise VaultLockBusyError(
        f"vault lock {key!r} busy after {timeout_seconds:.1f}s "
        "-- another datacron writer is holding it"
    ) from cause


if sys.platform == "win32":

    def _lock_file(lock_file: object, key: str, timeout_seconds: float) -> None:
        file_handle = lock_file
        if not hasattr(file_handle, "fileno") or not hasattr(file_handle, "seek"):
            raise TypeError("lock file must expose fileno and seek")
        file_handle.seek(0)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                msvcrt.locking(file_handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    _raise_vault_lock_busy(key, timeout_seconds, exc)
                time.sleep(_LOCK_RETRY_SECONDS)

    def _unlock_file(lock_file: object) -> None:
        file_handle = lock_file
        if not hasattr(file_handle, "fileno") or not hasattr(file_handle, "seek"):
            raise TypeError("lock file must expose fileno and seek")
        file_handle.seek(0)
        msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    _LOCK_BUSY_ERRNOS: Final[frozenset[int]] = frozenset(
        {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
    )

    def _lock_file(lock_file: object, key: str, timeout_seconds: float) -> None:
        if not hasattr(lock_file, "fileno"):
            raise TypeError("lock file must expose fileno")
        file_descriptor = lock_file.fileno()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in _LOCK_BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    _raise_vault_lock_busy(key, timeout_seconds, exc)
                time.sleep(_LOCK_RETRY_SECONDS)

    def _unlock_file(lock_file: object) -> None:
        if not hasattr(lock_file, "fileno"):
            raise TypeError("lock file must expose fileno")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _conformance_check(writer: VaultWriter) -> None:
    """Mypy structural conformance: FilesystemVaultWriter satisfies VaultWriter."""
    _ = writer


def _assert_conformance() -> None:
    """Static check only -- never invoked at runtime."""
    _conformance_check(FilesystemVaultWriter(Path(), Settings()))
