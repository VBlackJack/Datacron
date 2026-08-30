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
"""Crash-consistent application of validated organization bundles.

The transaction deliberately exposes no general note-move primitive. It accepts
only a :class:`ValidatedOrganizationBundle` produced by the organization
manifest validator. Exact payload bytes are staged durably before one pending
batch receipt is published. Recovery can then roll the whole batch forward when
every affected path is still at its declared before or after state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Final, Literal, NoReturn, TypeAlias, final

from ulid import ULID

from datacron.core.config import VaultConfig
from datacron.core.durability import (
    RecoveryRequiredError,
    atomic_durable_write,
    durable_flush_directory,
)
from datacron.core.frontmatter import (
    FrontmatterError,
    build_tiered_alias_index,
    coerce_string_list,
    resolve_note_title,
)
from datacron.core.hashing import sha256_bytes
from datacron.core.logger import get_logger
from datacron.core.operation_log import (
    JsonScalar,
    OperationContext,
    OperationJournal,
    OperationLogError,
    OperationRecord,
)
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.recovery import BlockedOperation
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault import SKIPPED_FOLDERS, NoteAdmissionPolicy
from datacron.organization.manifest import (
    MAX_MANIFEST_BYTES,
    MAX_OPERATION_COUNT,
    MAX_PAYLOAD_BYTES,
    MAX_TOTAL_PAYLOAD_BYTES,
    IdentitySidecarCaseCanonicalization,
    OrganizationManifestError,
    OrganizationScopeNotePrecondition,
    ResolvedOrganizationOperation,
    ValidatedOrganizationBundle,
    canonicalize_identity_sidecar_case_collisions,
    hash_identity_sidecar_case_canonicalizations,
    normalize_vault_rel_path,
    parse_organization_config_document,
    parse_organization_note_strict,
)

__all__ = [
    "BATCH_FAULT_POINTS",
    "BatchApplyResult",
    "BatchConflictError",
    "BatchFaultInjector",
    "BatchMemberResult",
    "BatchPrecommitValidator",
    "BatchRecoveryOutcome",
    "OrganizationBatchTransaction",
]

BatchFaultInjector: TypeAlias = Callable[[str], None]
BatchPrecommitValidator: TypeAlias = Callable[[], None]
BatchMemberKind: TypeAlias = Literal[
    "create_exact",
    "replace_exact",
    "move_replace_exact",
    "config_replace_exact",
    "identity_sidecar_replace_exact",
]

BATCH_FAULT_POINTS: Final[tuple[str, ...]] = (
    "after_stage_write",
    "after_history_write",
    "after_pending_write",
    "after_member_write",
    "after_source_delete",
    "after_operation_record",
    "after_commit_marker",
    "after_pending_cleanup",
    "after_stage_cleanup",
)
_BATCH_SCHEMA: Final[str] = "organization-batch-pending-v1"
_RESULT_SCHEMA: Final[str] = "organization-batch-result-v1"
_BATCHES_DIR_NAME: Final[str] = "batches"
_PENDING_DIR_NAME: Final[str] = "pending"
_STAGE_DIR_NAME: Final[str] = "stage"
_COMMITTED_DIR_NAME: Final[str] = "committed"
_CONFIG_REL_PATH: Final[str] = ".datacron/VAULT.yaml"
_IDENTITY_SIDECAR_REL_PATH: Final[str] = ".datacron/ulids.json"
_MIGRATED_IDENTITY_SIDECAR_REL_PATH: Final[str] = ".datacron/ulids.json.migrated"
_HISTORY_REL_ROOT: Final[str] = ".datacron/history"
_OPERATIONS_REL_PATH: Final[str] = ".datacron/oplog/operations.jsonl"
_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_STAGE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}\.after$")
_ATOMIC_RECEIPT_TEMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\.[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp$"
)
_CANONICAL_NOTE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_EXISTING_NOTE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Z]{26}$")
_H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)
_MAX_PENDING_MEMBERS: Final[int] = MAX_OPERATION_COUNT + 1
_MAX_PENDING_BYTES: Final[int] = MAX_MANIFEST_BYTES
_MAX_RESULT_BYTES: Final[int] = MAX_MANIFEST_BYTES
_MAX_ACTOR_LENGTH: Final[int] = 256
_MAX_SCOPE_NOTE_PRECONDITIONS: Final[int] = 8192
_MAX_IDENTITY_SIDECAR_CASE_CANONICALIZATIONS: Final[int] = 8192
_STREAM_CHUNK_BYTES: Final[int] = 64 * 1024
_REASON_CROSS_BATCH_EFFECT: Final[str] = "pending_batch_cross_effect"
_REASON_OPERATION_RECORD_MISMATCH: Final[str] = "pending_batch_operation_record_mismatch"
_REASON_RECEIPT_MISMATCH: Final[str] = "pending_batch_receipt_mismatch"
_REASON_SCOPE_VIOLATION: Final[str] = "pending_batch_scope_violation"
_REASON_STAGE_INVALID: Final[str] = "pending_batch_stage_invalid"
_REASON_BASELINE_MISMATCH: Final[str] = "pending_batch_baseline_mismatch"
_REASON_SCOPE_PRECONDITION: Final[str] = "pending_batch_scope_precondition_mismatch"
_PENDING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "batch_id",
        "manifest_sha256",
        "confirmation_token",
        "projected_report_sha256",
        "op",
        "tool",
        "actor",
        "parameters",
        "members",
        "scope_note_preconditions",
        "identity_sidecar_case_canonicalizations",
    }
)
_PENDING_MEMBER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "operation_id",
        "kind",
        "source_rel_path",
        "target_rel_path",
        "source_before_hash",
        "target_before_hash",
        "after_hash",
        "note_id",
        "before_aliases",
        "aliases",
        "stage_name",
        "created_parent_dirs",
    }
)
_BATCH_PARAMETER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "batch_id",
        "confirmation_token",
        "manifest_sha256",
        "member_count",
        "projected_report_sha256",
        "payload_set_sha256",
        "scope_digest",
        "operation_count",
        "total_payload_bytes",
        "config_replaced",
        "config_before_sha256",
        "identity_sidecar_replaced",
        "identity_sidecar_before_sha256",
        "migrated_identity_sidecar_before_sha256",
        "scope_note_preconditions_sha256",
        "identity_sidecar_case_canonicalization_count",
        "identity_sidecar_case_canonicalization_sha256",
    }
)
_SCOPE_NOTE_PRECONDITION_KEYS: Final[frozenset[str]] = frozenset({"rel_path", "sha256"})
_IDENTITY_SIDECAR_CASE_CANONICALIZATION_KEYS: Final[frozenset[str]] = frozenset(
    {"stale_path", "stale_id", "live_path", "live_id"}
)
_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "batch_id",
        "manifest_sha256",
        "confirmation_token",
        "projected_report_sha256",
        "payload_set_sha256",
        "scope_digest",
        "config_before_sha256",
        "members",
        "identity_sidecar_case_canonicalizations",
    }
)
_RESULT_MEMBER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "operation_id",
        "kind",
        "source_rel_path",
        "target_rel_path",
        "before_hash",
        "after_hash",
        "note_id",
    }
)
_LOGGER = get_logger(__name__)


class BatchConflictError(ValueError):
    """Raised when exact batch preconditions no longer match disk state."""


@dataclass(frozen=True)
class BatchMemberResult:
    """Committed exact-byte effect for one manifest member."""

    operation_id: str
    kind: BatchMemberKind
    source_rel_path: str | None
    target_rel_path: str
    before_hash: str | None
    after_hash: str
    note_id: str | None


@dataclass(frozen=True)
class BatchApplyResult:
    """Durable receipt returned by organization batch application."""

    batch_id: str
    manifest_sha256: str
    confirmation_token: str
    projected_report_sha256: str
    payload_set_sha256: str
    scope_digest: str
    config_before_sha256: str
    members: tuple[BatchMemberResult, ...]
    identity_sidecar_case_canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...] = ()
    already_committed: bool = False

    @property
    def identity_sidecar_case_canonicalization_count(self) -> int:
        """Return the number of proven obsolete sidecar case aliases removed."""
        return len(self.identity_sidecar_case_canonicalizations)

    @property
    def identity_sidecar_case_canonicalization_sha256(self) -> str:
        """Return the canonical digest bound to the receipt and operation log."""
        return hash_identity_sidecar_case_canonicalizations(
            self.identity_sidecar_case_canonicalizations
        )


@dataclass(frozen=True)
class BatchRecoveryOutcome:
    """Result of scanning every durable pending organization batch."""

    recovered: int = 0
    blocked: tuple[BlockedOperation, ...] = ()


@dataclass(frozen=True)
class _PendingMember:
    operation_id: str
    kind: BatchMemberKind
    source_rel_path: str | None
    target_rel_path: str
    source_before_hash: str | None
    target_before_hash: str | None
    after_hash: str
    note_id: str | None
    before_aliases: tuple[str, ...] | None
    aliases: tuple[str, ...] | None
    stage_name: str
    created_parent_dirs: tuple[str, ...]

    @property
    def result_before_hash(self) -> str | None:
        if self.kind == "move_replace_exact":
            return self.source_before_hash
        return self.target_before_hash

    def to_result(self) -> BatchMemberResult:
        return BatchMemberResult(
            operation_id=self.operation_id,
            kind=self.kind,
            source_rel_path=self.source_rel_path,
            target_rel_path=self.target_rel_path,
            before_hash=self.result_before_hash,
            after_hash=self.after_hash,
            note_id=self.note_id,
        )


@dataclass(frozen=True)
class _ScopeNotePrecondition:
    rel_path: str
    sha256: str


@dataclass(frozen=True)
class _PendingBatch:
    batch_id: str
    manifest_sha256: str
    confirmation_token: str
    projected_report_sha256: str
    op: str
    tool: str
    actor: str
    parameters: dict[str, JsonScalar]
    members: tuple[_PendingMember, ...]
    scope_note_preconditions: tuple[_ScopeNotePrecondition, ...]
    identity_sidecar_case_canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...]

    def to_result(self, *, already_committed: bool) -> BatchApplyResult:
        return BatchApplyResult(
            batch_id=self.batch_id,
            manifest_sha256=self.manifest_sha256,
            confirmation_token=self.confirmation_token,
            projected_report_sha256=self.projected_report_sha256,
            payload_set_sha256=_pending_parameter_hash(
                self.parameters,
                "payload_set_sha256",
            ),
            scope_digest=_pending_parameter_hash(self.parameters, "scope_digest"),
            config_before_sha256=_pending_parameter_hash(
                self.parameters,
                "config_before_sha256",
            ),
            members=tuple(member.to_result() for member in self.members),
            identity_sidecar_case_canonicalizations=(self.identity_sidecar_case_canonicalizations),
            already_committed=already_committed,
        )


@dataclass(frozen=True)
class _PendingSnapshot:
    pending: _PendingBatch
    path: Path
    raw_bytes: bytes


@dataclass(frozen=True)
class _RecoveryIdentity:
    rel_path: str
    note_id: str
    frontmatter_id: str | None
    title: str
    aliases: tuple[str, ...]

    @property
    def stem(self) -> str:
        return PurePosixPath(self.rel_path).stem


@dataclass(frozen=True)
class _PathState:
    member: _PendingMember
    rel_path: str
    before_hash: str | None
    after_hash: str | None
    is_source: bool


@final
class OrganizationBatchTransaction:
    """Apply and recover one validated organization manifest transaction."""

    def __init__(
        self,
        vault_root: Path,
        journal: OperationJournal,
        *,
        write_paths: Iterable[Path],
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._journal = journal
        self._write_paths = tuple(path.expanduser().resolve() for path in write_paths)
        self._batches_root = self._vault_root / ".datacron" / "oplog" / _BATCHES_DIR_NAME
        self._pending_root = self._batches_root / _PENDING_DIR_NAME
        self._stage_root = self._batches_root / _STAGE_DIR_NAME
        self._committed_root = self._batches_root / _COMMITTED_DIR_NAME

    def get_result(self, manifest_sha256: str) -> BatchApplyResult | None:
        """Return the durable committed result for one manifest hash, if present."""
        _require_hash(manifest_sha256, "manifest_sha256")
        path = self._assert_internal_path(
            self._committed_path(manifest_sha256),
            allow_missing=True,
        )
        if not path.exists():
            return None
        if not path.is_file():
            raise RecoveryRequiredError(
                "Recovery required: organization batch receipt is not a regular file"
            )
        try:
            result = _read_result(path)
        except (BatchConflictError, OperationLogError) as exc:
            raise RecoveryRequiredError(
                "Recovery required: corrupt organization batch receipt"
            ) from exc
        if (
            path.stem != manifest_sha256
            or result.batch_id != manifest_sha256
            or result.manifest_sha256 != manifest_sha256
        ):
            raise RecoveryRequiredError(
                "Recovery required: organization batch receipt identity differs from filename"
            )
        self._verify_result_records(result)
        self._verify_result_disk_state(result)
        return replace(result, already_committed=True)

    def _verify_result_disk_state(self, result: BatchApplyResult) -> None:
        """Bind an idempotent replay to the exact committed filesystem state."""
        for member in result.members:
            target_hash = self._disk_hash(member.target_rel_path)
            if target_hash != member.after_hash:
                raise RecoveryRequiredError(
                    "Recovery required: committed organization batch target differs from "
                    f"receipt: {member.target_rel_path}"
                )
            if (
                member.source_rel_path is not None
                and self._disk_hash(member.source_rel_path) is not None
            ):
                raise RecoveryRequiredError(
                    "Recovery required: committed organization batch move source reappeared: "
                    f"{member.source_rel_path}"
                )

    def removed_identity_ids(self, result: BatchApplyResult) -> tuple[str, ...]:
        """Return obsolete sidecar IDs authenticated by the committed receipt."""
        self._verify_result_records(result)
        self._verify_result_disk_state(result)
        return tuple(item.stale_id for item in result.identity_sidecar_case_canonicalizations)

    def validate_capacity(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> None:
        """Validate batch receipt capacity and live path policy without writing."""
        _require_hash(confirmation_token, "confirmation_token")
        _require_hash(projected_report_sha256, "projected_report_sha256")
        if self.has_pending_batches():
            raise BatchConflictError("pending organization batch requires recovery before preview")
        pending, _payloads = self._prepare_pending(
            bundle,
            confirmation_token,
            projected_report_sha256,
            operation,
        )
        self._validate_pending_receipt_size(pending)
        policy_error = self._pending_policy_error(pending)
        if policy_error is not None:
            raise BatchConflictError(policy_error)

    def has_pending_batches(self) -> bool:
        """Return whether a durable organization batch is pending recovery."""
        return bool(self._pending_paths())

    def apply(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        precommit_validator: BatchPrecommitValidator,
        operation: OperationContext,
        fault_injector: BatchFaultInjector | None = None,
    ) -> BatchApplyResult:
        """Apply a validated bundle while the caller holds the mutation lock.

        The synchronous validator is deliberately invoked before creating a stage
        directory. Callers use it to recompute live scope and preview evidence under
        the same global mutation lock that protects this method.
        """
        _require_hash(confirmation_token, "confirmation_token")
        _require_hash(projected_report_sha256, "projected_report_sha256")
        existing = self.get_result(bundle.manifest_sha256)
        if existing is not None:
            if existing.confirmation_token != confirmation_token:
                raise BatchConflictError(
                    "manifest was committed with a different confirmation token"
                )
            if existing.projected_report_sha256 != projected_report_sha256:
                raise BatchConflictError(
                    "manifest was committed with a different projected report hash"
                )
            return existing
        precommit_validator()
        pending, payloads = self._prepare_pending(
            bundle,
            confirmation_token,
            projected_report_sha256,
            operation,
        )
        self._validate_pending_receipt_size(pending)
        policy_error = self._pending_policy_error(pending)
        if policy_error is not None:
            raise BatchConflictError(policy_error)
        self._assert_journal_roots()
        self._validate_before_states(pending)
        records_started = False
        try:
            self._stage_payloads(pending, payloads, fault_injector)
            self._store_before_history(pending, fault_injector)
            self._write_pending(pending)
            _inject(fault_injector, "after_pending_write")
            self._roll_forward_bytes(pending, fault_injector)
            records_started = True
            self._append_records(pending, fault_injector)
            result = pending.to_result(already_committed=False)
            self._write_result(result)
            _inject(fault_injector, "after_commit_marker")
            self._remove_pending(pending.batch_id)
            _inject(fault_injector, "after_pending_cleanup")
            self._remove_stage(pending.batch_id)
            _inject(fault_injector, "after_stage_cleanup")
            _LOGGER.info(
                "organization batch committed batch_id=%s manifest_sha256=%s members=%d",
                pending.batch_id,
                pending.manifest_sha256,
                len(pending.members),
            )
            return result
        except Exception as exc:
            try:
                published = self._pending_is_published_exact(pending)
            except RecoveryRequiredError as recovery_exc:
                raise recovery_exc from exc
            if published and not records_started:
                self._rollback_exact(pending)
            elif not published:
                self._remove_stage(pending.batch_id)
            raise

    def recover(self) -> BatchRecoveryOutcome:
        """Roll every safe pending batch forward, or return exact-hash blockers."""
        snapshots = tuple(self._read_pending_snapshot(path) for path in self._pending_paths())
        if not snapshots:
            self._cleanup_atomic_receipt_temps()
            self._remove_orphan_stages(set())
            return BatchRecoveryOutcome()
        batches = tuple(snapshot.pending for snapshot in snapshots)
        records = {record.operation_id: record for record in self._read_records()}
        blocked = (
            *self._cross_batch_blockers(batches),
            *(
                blocked_item
                for pending in batches
                for blocked_item in self._classify_blocked(
                    pending,
                    records=records,
                )
            ),
        )
        if blocked:
            return BatchRecoveryOutcome(blocked=blocked)
        recovered = 0
        for snapshot in snapshots:
            pending = snapshot.pending
            snapshot_guard = partial(self._assert_pending_snapshot, snapshot)
            snapshot_guard()
            committed = self._read_committed_for_pending(pending)
            self._roll_forward_bytes(
                pending,
                None,
                pre_mutation=snapshot_guard,
            )
            if committed is None:
                snapshot_guard()
                self._append_records(
                    pending,
                    None,
                    pre_mutation=snapshot_guard,
                )
                snapshot_guard()
                self._write_result(pending.to_result(already_committed=False))
            snapshot_guard()
            self._remove_pending(pending.batch_id)
            self._remove_stage(pending.batch_id)
            recovered += 1
            _LOGGER.warning(
                "recovered organization batch batch_id=%s members=%d",
                pending.batch_id,
                len(pending.members),
            )
        self._cleanup_atomic_receipt_temps()
        self._remove_orphan_stages({pending.batch_id for pending in batches})
        return BatchRecoveryOutcome(recovered=recovered)

    def inspect(self) -> tuple[BlockedOperation, ...]:
        """Inspect pending batches without changing vault or sidecar bytes."""
        batches = tuple(self._read_pending_stable(path) for path in self._pending_paths())
        records = {record.operation_id: record for record in self._read_records()}
        return (
            *self._cross_batch_blockers(batches),
            *(
                blocked_item
                for pending in batches
                for blocked_item in self._classify_blocked(
                    pending,
                    records=records,
                )
            ),
        )

    def _prepare_pending(
        self,
        bundle: ValidatedOrganizationBundle,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> tuple[_PendingBatch, dict[str, bytes]]:
        _require_hash(bundle.manifest_sha256, "manifest_sha256")
        ordered = sorted(bundle.operations, key=_operation_sort_key)
        members: list[_PendingMember] = []
        payloads: dict[str, bytes] = {}
        for index, resolved in enumerate(ordered):
            member = self._prepare_note_member(bundle.manifest_sha256, index, resolved)
            members.append(member)
            payloads[member.stage_name] = resolved.payload.raw_bytes
        prepared_config = self._prepare_config_member(bundle, len(members))
        if prepared_config is not None:
            config_member, config_bytes = prepared_config
            members.append(config_member)
            payloads[config_member.stage_name] = config_bytes
        sidecar_rel_path = self._validate_identity_sidecar_paths(bundle)
        prepared_sidecar = self._prepare_identity_sidecar_member(
            bundle,
            len(members),
            sidecar_rel_path,
        )
        if prepared_sidecar is not None:
            sidecar_member, sidecar_bytes = prepared_sidecar
            members.append(sidecar_member)
            payloads[sidecar_member.stage_name] = sidecar_bytes
        self._validate_member_paths(members)
        scope_note_preconditions = _prepare_scope_note_preconditions(
            bundle.scope_note_preconditions
        )
        external_operation_count = len(bundle.operations) + int(bundle.config_payload is not None)
        total_payload_bytes = sum(len(payload) for payload in payloads.values())
        if external_operation_count > MAX_OPERATION_COUNT:
            raise BatchConflictError(
                f"organization batch exceeds {MAX_OPERATION_COUNT} manifest operations"
            )
        if total_payload_bytes > MAX_TOTAL_PAYLOAD_BYTES:
            raise BatchConflictError(
                f"organization batch payloads exceed {MAX_TOTAL_PAYLOAD_BYTES} bytes"
            )
        cleaned_parameters = _clean_parameters(operation.parameters)
        cleaned_parameters.update(
            {
                "batch_id": bundle.manifest_sha256,
                "confirmation_token": confirmation_token,
                "manifest_sha256": bundle.manifest_sha256,
                "member_count": len(members),
                "projected_report_sha256": projected_report_sha256,
                "payload_set_sha256": bundle.payload_set_sha256,
                "scope_digest": bundle.scope_digest,
                "operation_count": external_operation_count,
                "total_payload_bytes": total_payload_bytes,
                "config_replaced": bundle.config_payload is not None,
                "config_before_sha256": bundle.config_before_sha256,
                "identity_sidecar_replaced": (bundle.identity_sidecar_after_bytes is not None),
                "identity_sidecar_before_sha256": (bundle.identity_sidecar_before_sha256),
                "migrated_identity_sidecar_before_sha256": (
                    bundle.migrated_identity_sidecar_before_sha256
                ),
                "scope_note_preconditions_sha256": _scope_preconditions_sha256(
                    scope_note_preconditions
                ),
                "identity_sidecar_case_canonicalization_count": (
                    bundle.identity_sidecar_case_canonicalization_count
                ),
                "identity_sidecar_case_canonicalization_sha256": (
                    bundle.identity_sidecar_case_canonicalization_sha256
                ),
            }
        )
        pending = _PendingBatch(
            batch_id=bundle.manifest_sha256,
            manifest_sha256=bundle.manifest_sha256,
            confirmation_token=confirmation_token,
            projected_report_sha256=projected_report_sha256,
            op=operation.op,
            tool=operation.tool,
            actor=_bounded_actor(operation.actor),
            parameters=cleaned_parameters,
            members=tuple(members),
            scope_note_preconditions=scope_note_preconditions,
            identity_sidecar_case_canonicalizations=(
                bundle.identity_sidecar_case_canonicalizations
            ),
        )
        _validate_pending(pending)
        return pending, payloads

    def _prepare_config_member(
        self,
        bundle: ValidatedOrganizationBundle,
        index: int,
    ) -> tuple[_PendingMember, bytes] | None:
        config_payload = bundle.config_payload
        if config_payload is None:
            return None
        if bundle.config_path is None or bundle.manifest.config is None:
            raise BatchConflictError("validated config payload is incomplete")
        if bundle.config_before_sha256 != bundle.manifest.config.expected_sha256:
            raise BatchConflictError("validated config baseline differs from manifest CAS")
        config_rel_path = self._relative_path(bundle.config_path)
        if config_rel_path != _CONFIG_REL_PATH:
            raise BatchConflictError("config target must be .datacron/VAULT.yaml")
        return (
            _PendingMember(
                operation_id=_operation_id(bundle.manifest_sha256, index),
                kind="config_replace_exact",
                source_rel_path=None,
                target_rel_path=config_rel_path,
                source_before_hash=None,
                target_before_hash=bundle.config_before_sha256,
                after_hash=config_payload.sha256,
                note_id=None,
                before_aliases=None,
                aliases=None,
                stage_name=_stage_name(index),
                created_parent_dirs=self._missing_parent_dirs(config_rel_path),
            ),
            config_payload.raw_bytes,
        )

    def _validate_identity_sidecar_paths(
        self,
        bundle: ValidatedOrganizationBundle,
    ) -> str:
        sidecar_rel_path = self._relative_path(bundle.identity_sidecar_path)
        if sidecar_rel_path != _IDENTITY_SIDECAR_REL_PATH:
            raise BatchConflictError("identity sidecar target must be .datacron/ulids.json")
        migrated_rel_path = self._relative_path(bundle.migrated_identity_sidecar_path)
        if migrated_rel_path != _MIGRATED_IDENTITY_SIDECAR_REL_PATH:
            raise BatchConflictError(
                "migrated identity sidecar target must be .datacron/ulids.json.migrated"
            )
        return sidecar_rel_path

    def _prepare_identity_sidecar_member(
        self,
        bundle: ValidatedOrganizationBundle,
        index: int,
        sidecar_rel_path: str,
    ) -> tuple[_PendingMember, bytes] | None:
        if (bundle.identity_sidecar_after_bytes is None) != (
            bundle.identity_sidecar_after_sha256 is None
        ):
            raise BatchConflictError("validated identity sidecar payload is incomplete")
        after_bytes = bundle.identity_sidecar_after_bytes
        after_hash = bundle.identity_sidecar_after_sha256
        if after_bytes is None or after_hash is None:
            return None
        before_hash = bundle.identity_sidecar_before_sha256
        if before_hash is None:
            raise BatchConflictError("validated identity sidecar payload is incomplete")
        if sha256_bytes(after_bytes) != after_hash:
            raise BatchConflictError("validated identity sidecar payload hash differs")
        return (
            _PendingMember(
                operation_id=_operation_id(bundle.manifest_sha256, index),
                kind="identity_sidecar_replace_exact",
                source_rel_path=None,
                target_rel_path=sidecar_rel_path,
                source_before_hash=None,
                target_before_hash=before_hash,
                after_hash=after_hash,
                note_id=None,
                before_aliases=None,
                aliases=None,
                stage_name=_stage_name(index),
                created_parent_dirs=self._missing_parent_dirs(sidecar_rel_path),
            ),
            after_bytes,
        )

    def _prepare_note_member(
        self,
        manifest_sha256: str,
        index: int,
        resolved: ResolvedOrganizationOperation,
    ) -> _PendingMember:
        target_rel_path = self._relative_path(resolved.target_path)
        result_identity = resolved.result_identity
        expected_identity = resolved.expected_identity
        note_id = result_identity.id
        kind = resolved.kind
        source_rel_path = (
            self._relative_path(resolved.source_path)
            if kind == "move_replace_exact" and resolved.source_path is not None
            else None
        )
        if kind == "create_exact":
            source_before_hash = None
            target_before_hash = None
        elif kind == "replace_exact":
            source_before_hash = None
            target_before_hash = resolved.before_sha256
        elif kind == "move_replace_exact":
            if source_rel_path is None:
                raise BatchConflictError("move_replace_exact requires a source path")
            source_before_hash = resolved.before_sha256
            target_before_hash = None
        else:
            raise BatchConflictError(f"unsupported organization operation: {kind}")
        return _PendingMember(
            operation_id=_operation_id(manifest_sha256, index),
            kind=kind,
            source_rel_path=source_rel_path,
            target_rel_path=target_rel_path,
            source_before_hash=source_before_hash,
            target_before_hash=target_before_hash,
            after_hash=resolved.after_sha256,
            note_id=note_id,
            before_aliases=(expected_identity.aliases if expected_identity is not None else None),
            aliases=result_identity.aliases,
            stage_name=_stage_name(index),
            created_parent_dirs=self._missing_parent_dirs(target_rel_path),
        )

    @staticmethod
    def _validate_pending_receipt_size(pending: _PendingBatch) -> None:
        if len(_pending_bytes(pending)) > _MAX_PENDING_BYTES:
            raise BatchConflictError(
                f"pending organization batch exceeds {_MAX_PENDING_BYTES} bytes"
            )

    def _validate_member_paths(self, members: Iterable[_PendingMember]) -> None:
        effects: set[str] = set()
        for member in members:
            target_identity = _path_identity(member.target_rel_path)
            if target_identity in effects:
                raise BatchConflictError(f"duplicate batch path effect: {member.target_rel_path}")
            effects.add(target_identity)
            if member.source_rel_path is None:
                continue
            source_identity = _path_identity(member.source_rel_path)
            if source_identity in effects:
                raise BatchConflictError(f"duplicate batch path effect: {member.source_rel_path}")
            effects.add(source_identity)

    def _validate_before_states(self, pending: _PendingBatch) -> None:
        if not self._journal.history_enabled:
            raise BatchConflictError("organization batches require full operation history")
        baseline_error = self._durable_baseline_error(pending)
        if baseline_error is not None:
            raise BatchConflictError(baseline_error)
        scope_precondition_error = self._scope_precondition_error(pending)
        if scope_precondition_error is not None:
            raise BatchConflictError(scope_precondition_error)
        for state in _path_states(pending):
            actual_hash = self._disk_hash(state.rel_path)
            if actual_hash != state.before_hash:
                raise BatchConflictError(
                    "batch precondition changed for "
                    f"{state.rel_path}: expected {state.before_hash}, actual {actual_hash}"
                )

    def _stage_payloads(
        self,
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
        fault_injector: BatchFaultInjector | None,
    ) -> None:
        stage_dir = self._assert_internal_path(
            self._stage_dir(pending.batch_id),
            allow_missing=True,
        )
        _ensure_directory_durable(stage_dir)
        for member in pending.members:
            data = payloads[member.stage_name]
            if len(data) > MAX_PAYLOAD_BYTES:
                raise BatchConflictError(
                    f"payload exceeds {MAX_PAYLOAD_BYTES} bytes for {member.target_rel_path}"
                )
            if sha256_bytes(data) != member.after_hash:
                raise BatchConflictError(f"payload hash changed for {member.target_rel_path}")
            stage_path = self._assert_internal_path(
                stage_dir / member.stage_name,
                allow_missing=True,
            )
            written_hash = atomic_durable_write(stage_path, data)
            if written_hash != member.after_hash:
                raise OperationLogError("staged payload hash differs from after_hash")
            _inject(fault_injector, "after_stage_write")

    def _store_before_history(
        self,
        pending: _PendingBatch,
        fault_injector: BatchFaultInjector | None,
    ) -> None:
        for state in _path_states(pending):
            if state.before_hash is None:
                continue
            before_bytes = _read_bounded_file(
                self._absolute_path(state.rel_path),
                limit=MAX_PAYLOAD_BYTES,
                label=f"batch history baseline {state.rel_path}",
            )
            stored_hash = self._store_history(before_bytes)
            if stored_hash != state.before_hash:
                raise BatchConflictError(f"history baseline changed for {state.rel_path}")
            _inject(fault_injector, "after_history_write")

    def _roll_forward_bytes(
        self,
        pending: _PendingBatch,
        fault_injector: BatchFaultInjector | None,
        *,
        pre_mutation: Callable[[], None] | None = None,
    ) -> None:
        blocked = self._classify_blocked(pending)
        if blocked:
            raise _recovery_required(blocked)
        for member in pending.members:
            current_hash = self._disk_hash(member.target_rel_path)
            if current_hash == member.after_hash:
                continue
            for parent_rel_path in member.created_parent_dirs:
                if pre_mutation is not None:
                    pre_mutation()
                _ensure_directory_durable(self._absolute_path(parent_rel_path))
            stage_path = self._assert_internal_path(
                self._stage_dir(pending.batch_id) / member.stage_name,
                allow_missing=True,
            )
            if not stage_path.exists() or not stage_path.is_file():
                raise RecoveryRequiredError(
                    f"staged payload missing for batch member {member.operation_id}"
                )
            after_bytes = _read_bounded_file(
                stage_path,
                limit=MAX_PAYLOAD_BYTES,
                label=f"staged payload {member.stage_name}",
            )
            if sha256_bytes(after_bytes) != member.after_hash:
                raise RecoveryRequiredError(
                    f"staged payload hash mismatch for batch member {member.operation_id}"
                )
            if pre_mutation is not None:
                pre_mutation()
            written_hash = atomic_durable_write(
                self._absolute_path(member.target_rel_path), after_bytes
            )
            if written_hash != member.after_hash:
                raise OperationLogError("durable batch target hash differs from after_hash")
            _inject(fault_injector, "after_member_write")
        sources = sorted(
            (member for member in pending.members if member.source_rel_path is not None),
            key=lambda item: _path_identity(item.source_rel_path or ""),
        )
        for member in sources:
            source_rel_path = member.source_rel_path
            if source_rel_path is None or self._disk_hash(source_rel_path) is None:
                continue
            if pre_mutation is not None:
                pre_mutation()
            self._delete_file_durable(self._absolute_path(source_rel_path))
            _inject(fault_injector, "after_source_delete")

    def _rollback_exact(self, pending: _PendingBatch) -> None:
        blocked = self._classify_blocked(pending)
        if blocked:
            raise _recovery_required(blocked)
        source_states = sorted(
            (state for state in _path_states(pending) if state.is_source),
            key=lambda state: _path_identity(state.rel_path),
            reverse=True,
        )
        target_states = tuple(
            reversed(tuple(state for state in _path_states(pending) if not state.is_source))
        )
        for state in (*source_states, *target_states):
            current_hash = self._disk_hash(state.rel_path)
            if current_hash == state.before_hash:
                continue
            target = self._absolute_path(state.rel_path)
            if state.before_hash is None:
                self._delete_file_durable(target)
                continue
            before_bytes = self._read_history(state.before_hash)
            written_hash = atomic_durable_write(target, before_bytes)
            if written_hash != state.before_hash:
                raise OperationLogError("batch rollback did not restore exact before bytes")
        created_directories = sorted(
            {rel_path for member in pending.members for rel_path in member.created_parent_dirs},
            key=lambda rel_path: (len(Path(rel_path).parts), _path_identity(rel_path)),
            reverse=True,
        )
        for rel_path in created_directories:
            self._remove_empty_directory(self._absolute_path(rel_path))
        for state in _path_states(pending):
            if self._disk_hash(state.rel_path) != state.before_hash:
                raise RecoveryRequiredError(
                    f"Recovery required: batch rollback incomplete for {state.rel_path}"
                )
        self._remove_pending(pending.batch_id)
        self._remove_stage(pending.batch_id)
        _LOGGER.warning("organization batch rolled back batch_id=%s", pending.batch_id)

    def _append_records(
        self,
        pending: _PendingBatch,
        fault_injector: BatchFaultInjector | None,
        *,
        pre_mutation: Callable[[], None] | None = None,
    ) -> None:
        existing_records = {record.operation_id: record for record in self._read_records()}
        for member in pending.members:
            existing = existing_records.get(member.operation_id)
            if existing is not None:
                if not self._record_matches_member(existing, pending, member):
                    raise RecoveryRequiredError(
                        "Recovery required: existing organization operation record "
                        "differs from pending batch"
                    )
                continue
            record = OperationRecord(
                operation_id=member.operation_id,
                timestamp=self._next_timestamp(),
                op=pending.op,
                tool=pending.tool,
                note_id=member.note_id,
                rel_path=member.target_rel_path,
                before_hash=member.result_before_hash,
                after_hash=member.after_hash,
                actor=pending.actor,
                parameters=self._record_parameters(pending, member),
                history_stored=member.result_before_hash is not None,
            )
            if pre_mutation is not None:
                pre_mutation()
            self._append_record(record)
            existing_records[member.operation_id] = record
            _inject(fault_injector, "after_operation_record")

    def _classify_blocked(
        self,
        pending: _PendingBatch,
        *,
        records: Mapping[str, OperationRecord] | None = None,
    ) -> tuple[BlockedOperation, ...]:
        preflight_reason = self._recovery_preflight_reason(pending)
        if preflight_reason is not None:
            return (
                self._blocked_member(
                    pending.members[0],
                    reason=preflight_reason,
                ),
            )
        receipt_error, receipt_present = self._receipt_state(pending)
        if receipt_error is not None:
            return (
                self._blocked_member(
                    pending.members[0],
                    reason=_REASON_RECEIPT_MISMATCH,
                ),
            )
        resolved_records = (
            dict(records)
            if records is not None
            else {record.operation_id: record for record in self._read_records()}
        )
        for member in pending.members:
            record = resolved_records.get(member.operation_id)
            if record is None:
                if not receipt_present:
                    continue
            elif self._record_matches_member(record, pending, member):
                continue
            return (
                self._blocked_member(
                    member,
                    reason=_REASON_OPERATION_RECORD_MISMATCH,
                ),
            )
        blocked: list[BlockedOperation] = []
        for state in _path_states(pending):
            disk_hash = self._disk_hash(state.rel_path)
            if disk_hash in {state.before_hash, state.after_hash}:
                continue
            blocked.append(
                BlockedOperation(
                    operation_id=state.member.operation_id,
                    rel_path=state.rel_path,
                    reason="pending_batch_disk_hash_mismatch",
                    expected_before_hash=state.before_hash,
                    expected_after_hash=state.after_hash or state.member.after_hash,
                    disk_hash=disk_hash,
                    # Batch repair is intentionally whole-transaction work.  The
                    # ordinary single-note `ops repair` command cannot safely
                    # resolve one member while the batch receipt remains pending.
                    restore_before_available=False,
                    adopt_disk_available=False,
                )
            )
        return tuple(blocked)

    def _recovery_preflight_reason(self, pending: _PendingBatch) -> str | None:
        validators = (
            (self._pending_policy_error, _REASON_SCOPE_VIOLATION),
            (self._durable_baseline_error, _REASON_BASELINE_MISMATCH),
            (self._scope_precondition_error, _REASON_SCOPE_PRECONDITION),
            (self._stage_error, _REASON_STAGE_INVALID),
        )
        for validator, reason in validators:
            if validator(pending) is not None:
                return reason
        return None

    def _pending_policy_error(self, pending: _PendingBatch) -> str | None:
        try:
            organization_scope, admission_policy = self._live_organization_policy()
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            OperationLogError,
        ) as exc:
            return f"active live organization scope is unavailable: {exc}"
        for member in pending.members:
            if member.kind in {
                "config_replace_exact",
                "identity_sidecar_replace_exact",
            }:
                expected_target = (
                    _CONFIG_REL_PATH
                    if member.kind == "config_replace_exact"
                    else _IDENTITY_SIDECAR_REL_PATH
                )
                if member.target_rel_path != expected_target:
                    return "internal recovery target is not canonical"
                continue
            rel_paths = [member.target_rel_path]
            if member.source_rel_path is not None:
                rel_paths.append(member.source_rel_path)
            for rel_path in rel_paths:
                if not _rel_path_belongs_to_scope(rel_path, organization_scope):
                    return (
                        f"batch note path {rel_path!r} is outside live "
                        f"organization.scope {organization_scope!r}"
                    )
                if not _rel_path_is_admitted(rel_path, admission_policy):
                    return f"batch note path {rel_path!r} is excluded from live admission"
                candidate = self._absolute_path(rel_path)
                try:
                    assert_within_paths(
                        candidate,
                        self._write_paths,
                        kind="write",
                    )
                except PathConfinementError:
                    return f"batch note path {rel_path!r} is outside DATACRON_WRITE_PATHS"
        return None

    def _live_organization_scope(self) -> str:
        scope, _policy = self._live_organization_policy()
        return scope

    def _live_organization_policy(self) -> tuple[str, NoteAdmissionPolicy]:
        config_path = self._absolute_path(_CONFIG_REL_PATH)
        raw_bytes = _read_bounded_file(
            config_path,
            limit=MAX_PAYLOAD_BYTES,
            label="live VAULT.yaml",
        )
        _document, config = _config_document(raw_bytes, label="live VAULT.yaml")
        organization = config.organization
        if organization is None or not organization.rules or organization.scope is None:
            raise ValueError("live VAULT.yaml does not declare an active organization.scope")
        scope = organization.scope
        scope_path = self._absolute_path(f"{scope}/__organization_scope_guard__.md").parent
        if not scope_path.is_dir():
            raise ValueError(f"organization.scope does not exist as a directory: {scope!r}")
        policy = NoteAdmissionPolicy(
            excluded_folders=SKIPPED_FOLDERS | frozenset(config.excluded_folders),
            excluded_files=frozenset(config.excluded_files),
        )
        return scope, policy

    def _durable_baseline_error(self, pending: _PendingBatch) -> str | None:
        checks = (
            self._internal_member_baseline_error(
                pending,
                parameter_key="config_before_sha256",
                rel_path=_CONFIG_REL_PATH,
                member_kind="config_replace_exact",
                label="VAULT.yaml",
                required=True,
            ),
            self._internal_member_baseline_error(
                pending,
                parameter_key="identity_sidecar_before_sha256",
                rel_path=_IDENTITY_SIDECAR_REL_PATH,
                member_kind="identity_sidecar_replace_exact",
                label="primary identity sidecar",
                required=False,
            ),
            self._exact_baseline_error(
                pending,
                parameter_key="migrated_identity_sidecar_before_sha256",
                rel_path=_MIGRATED_IDENTITY_SIDECAR_REL_PATH,
                label="migrated identity sidecar",
            ),
        )
        for error in checks:
            if error is not None:
                return error
        return None

    def _internal_member_baseline_error(
        self,
        pending: _PendingBatch,
        *,
        parameter_key: str,
        rel_path: str,
        member_kind: BatchMemberKind,
        label: str,
        required: bool,
    ) -> str | None:
        before_value = pending.parameters.get(parameter_key)
        if required and not isinstance(before_value, str):
            return f"{label} baseline parameter is missing"
        if before_value is not None and not isinstance(before_value, str):
            return f"{label} baseline parameter is invalid"
        member = next(
            (item for item in pending.members if item.kind == member_kind),
            None,
        )
        member_before = member.target_before_hash if member is not None else None
        if member is not None and member_before is None:
            return f"{label} member baseline is missing"
        allowed_hashes: set[str | None] = (
            {before_value} if member is None else {member_before, member.after_hash}
        )
        if self._disk_hash(rel_path) not in allowed_hashes:
            return f"live {label} differs from its pending baseline"
        return None

    def _exact_baseline_error(
        self,
        pending: _PendingBatch,
        *,
        parameter_key: str,
        rel_path: str,
        label: str,
    ) -> str | None:
        before_value = pending.parameters.get(parameter_key)
        if before_value is not None and not isinstance(before_value, str):
            return f"{label} baseline parameter is invalid"
        if self._disk_hash(rel_path) != before_value:
            return f"live {label} differs from its pending baseline"
        return None

    def _scope_precondition_error(self, pending: _PendingBatch) -> str | None:
        try:
            live_notes = self._live_scope_note_hashes()
        except (
            OSError,
            OperationLogError,
            PathConfinementError,
            RecoveryRequiredError,
            ValueError,
        ) as exc:
            return f"live organization scope inventory is unavailable: {exc}"
        baseline = {
            _path_identity(item.rel_path): item for item in pending.scope_note_preconditions
        }
        effect_paths: set[str] = set()
        for member in pending.members:
            if member.kind not in {
                "create_exact",
                "replace_exact",
                "move_replace_exact",
            }:
                continue
            effect_paths.add(_path_identity(member.target_rel_path))
            if member.source_rel_path is not None:
                effect_paths.add(_path_identity(member.source_rel_path))
        for identity, item in baseline.items():
            if identity in effect_paths:
                continue
            live_item = live_notes.get(identity)
            if live_item is None or live_item[1] != item.sha256:
                return f"scope note changed or disappeared: {item.rel_path!r}"
        for identity, (rel_path, content_hash) in live_notes.items():
            if identity in effect_paths:
                continue
            baseline_item = baseline.get(identity)
            if baseline_item is None or baseline_item.sha256 != content_hash:
                return f"scope note appeared or changed: {rel_path!r}"
        return None

    def _live_scope_note_hashes(self) -> dict[str, tuple[str, str]]:
        return {
            identity: (rel_path, _stream_sha256(path))
            for identity, (rel_path, path) in self._live_scope_note_paths().items()
        }

    def _live_scope_note_paths(self) -> dict[str, tuple[str, Path]]:
        scope, policy = self._live_organization_policy()
        scope_root = self._absolute_path(scope)
        return self._collect_live_admitted_note_paths(scope_root, policy)

    def _live_admitted_note_paths(self) -> dict[str, tuple[str, Path]]:
        _scope, policy = self._live_organization_policy()
        return self._collect_live_admitted_note_paths(self._vault_root, policy)

    def _collect_live_admitted_note_paths(
        self,
        root: Path,
        policy: NoteAdmissionPolicy,
    ) -> dict[str, tuple[str, Path]]:
        pending_directories = [root]
        notes: dict[str, tuple[str, Path]] = {}
        while pending_directories:
            directory = pending_directories.pop()
            for candidate in sorted(directory.iterdir()):
                safe_candidate = assert_path_chain_without_links(
                    candidate,
                    anchor=self._vault_root,
                    allow_missing=False,
                )
                if safe_candidate.is_dir():
                    if (
                        safe_candidate.name.startswith(".")
                        or safe_candidate.name.casefold() in policy.excluded_folders
                    ):
                        continue
                    pending_directories.append(safe_candidate)
                    continue
                if (
                    not safe_candidate.is_file()
                    or not safe_candidate.name.casefold().endswith(".md")
                    or safe_candidate.name.casefold() in policy.excluded_files
                ):
                    continue
                rel_path = self._relative_path(safe_candidate)
                identity = _path_identity(rel_path)
                if identity in notes:
                    raise OperationLogError(
                        f"live scope contains case-colliding notes: {rel_path!r}"
                    )
                notes[identity] = (rel_path, safe_candidate)
        return notes

    def _stage_error(self, pending: _PendingBatch) -> str | None:
        stage_dir = self._assert_internal_path(
            self._stage_dir(pending.batch_id),
            allow_missing=True,
        )
        if not stage_dir.is_dir():
            return "batch stage directory is missing"
        entries_error = self._stage_entries_error(stage_dir, pending)
        if entries_error is not None:
            return entries_error
        payloads, payload_error = self._read_stage_payloads(stage_dir, pending)
        if payload_error is not None:
            return payload_error
        total_bytes = sum(len(payload) for payload in payloads.values())
        if total_bytes != pending.parameters.get("total_payload_bytes"):
            return "staged payload byte total differs from pending receipt"
        validators = (
            self._note_stage_error,
            self._source_history_identity_error,
            self._projected_identity_stage_error,
            self._config_stage_error,
            self._identity_sidecar_stage_error,
        )
        for validator in validators:
            validation_error = validator(pending, payloads)
            if validation_error is not None:
                return validation_error
        return None

    def _stage_entries_error(
        self,
        stage_dir: Path,
        pending: _PendingBatch,
    ) -> str | None:
        expected_names = {member.stage_name for member in pending.members}
        actual_names: set[str] = set()
        for entry in sorted(stage_dir.iterdir()):
            safe_entry = self._assert_internal_path(entry, allow_missing=False)
            if not safe_entry.is_file():
                return f"unexpected non-file stage entry: {safe_entry.name}"
            actual_names.add(safe_entry.name)
        if actual_names != expected_names:
            return "batch stage members differ from pending receipt"
        return None

    def _read_stage_payloads(
        self,
        stage_dir: Path,
        pending: _PendingBatch,
    ) -> tuple[dict[str, bytes], str | None]:
        payloads: dict[str, bytes] = {}
        for member in pending.members:
            stage_path = self._assert_internal_path(
                stage_dir / member.stage_name,
                allow_missing=False,
            )
            try:
                after_bytes = _read_bounded_file(
                    stage_path,
                    limit=MAX_PAYLOAD_BYTES,
                    label=f"staged payload {member.stage_name}",
                )
            except OperationLogError as exc:
                return {}, str(exc)
            if sha256_bytes(after_bytes) != member.after_hash:
                return {}, f"staged payload hash mismatch for {member.operation_id}"
            payloads[member.stage_name] = after_bytes
        return payloads, None

    @staticmethod
    def _note_stage_error(
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
    ) -> str | None:
        note_kinds = {"create_exact", "replace_exact", "move_replace_exact"}
        for member in pending.members:
            if member.kind not in note_kinds:
                continue
            try:
                text = payloads[member.stage_name].decode("utf-8", errors="strict")
                metadata, _body = parse_organization_note_strict(text)
                aliases = tuple(coerce_string_list(metadata.get("aliases")))
            except (FrontmatterError, UnicodeDecodeError, ValueError) as exc:
                return f"staged note payload is invalid for {member.operation_id}: {exc}"
            if metadata.get("id") != member.note_id or aliases != member.aliases:
                return f"staged note identity differs for {member.operation_id}"
        return None

    def _source_history_identity_error(
        self,
        pending: _PendingBatch,
        _payloads: Mapping[str, bytes],
    ) -> str | None:
        for member in pending.members:
            if member.kind == "replace_exact":
                before_hash = member.target_before_hash
            elif member.kind == "move_replace_exact":
                before_hash = member.source_before_hash
            else:
                continue
            if before_hash is None or member.before_aliases is None:
                return f"pending source identity is incomplete for {member.operation_id}"
            try:
                before_bytes = self._read_history(before_hash)
                text = before_bytes.decode("utf-8", errors="strict")
                metadata, _body = parse_organization_note_strict(text)
                aliases = tuple(coerce_string_list(metadata.get("aliases")))
            except (
                FrontmatterError,
                OperationLogError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                return f"historical note identity is invalid for {member.operation_id}: {exc}"
            if metadata.get("id") != member.note_id or aliases != member.before_aliases:
                return f"historical note identity differs for {member.operation_id}"
        return None

    def _projected_identity_stage_error(
        self,
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
    ) -> str | None:
        try:
            baseline, projected, results, sidecar_mappings = self._recovery_identity_projection(
                pending, payloads
            )
            collision_error = _projected_id_collision_error(projected)
            if collision_error is not None:
                return collision_error
            sidecar_error = _projected_sidecar_identity_error(
                projected,
                sidecar_mappings,
            )
            if sidecar_error is not None:
                return sidecar_error
            return _projected_alias_resolution_error(
                baseline,
                projected,
                results,
            )
        except (
            FrontmatterError,
            OSError,
            OperationLogError,
            PathConfinementError,
            RecoveryRequiredError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            return f"projected note identity inventory is invalid: {exc}"

    def _recovery_identity_projection(
        self,
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
    ) -> tuple[
        tuple[_RecoveryIdentity, ...],
        tuple[_RecoveryIdentity, ...],
        tuple[_RecoveryIdentity, ...],
        dict[str, str],
    ]:
        sidecar_mappings = self._projected_sidecar_mappings(pending)
        effect_paths = _note_effect_path_identities(pending.members)
        baseline: list[_RecoveryIdentity] = []
        projected: dict[str, _RecoveryIdentity] = {}
        for identity, (rel_path, path) in self._live_admitted_note_paths().items():
            if identity in effect_paths:
                continue
            item = _recovery_identity_from_bytes(
                rel_path,
                _read_bounded_file(
                    path,
                    limit=MAX_PAYLOAD_BYTES,
                    label=f"admitted note {rel_path}",
                ),
                id_mappings=sidecar_mappings,
            )
            baseline.append(item)
            projected[identity] = item
        results: list[_RecoveryIdentity] = []
        for member in pending.members:
            if member.kind not in {
                "create_exact",
                "replace_exact",
                "move_replace_exact",
            }:
                continue
            source_item = self._historical_recovery_identity(member)
            if source_item is not None:
                baseline.append(source_item)
            result = _recovery_identity_from_bytes(
                member.target_rel_path,
                payloads[member.stage_name],
                id_mappings=sidecar_mappings,
                expected_note_id=member.note_id,
                expected_aliases=member.aliases,
            )
            projected[_path_identity(member.target_rel_path)] = result
            results.append(result)
        return (
            tuple(baseline),
            tuple(projected.values()),
            tuple(results),
            sidecar_mappings,
        )

    def _historical_recovery_identity(
        self,
        member: _PendingMember,
    ) -> _RecoveryIdentity | None:
        rel_path: str | None
        if member.kind == "replace_exact":
            rel_path = member.target_rel_path
            before_hash = member.target_before_hash
        elif member.kind == "move_replace_exact":
            rel_path = member.source_rel_path
            before_hash = member.source_before_hash
        else:
            return None
        if (
            rel_path is None
            or before_hash is None
            or member.note_id is None
            or member.before_aliases is None
        ):
            raise OperationLogError("pending source identity is incomplete")
        return _recovery_identity_from_bytes(
            rel_path,
            self._read_history(before_hash),
            id_mappings={},
            expected_note_id=member.note_id,
            expected_aliases=member.before_aliases,
        )

    def _live_sidecar_mappings(self) -> dict[str, str]:
        merged = self._optional_sidecar_mapping(
            _IDENTITY_SIDECAR_REL_PATH,
            label="live primary ULID sidecar",
        )
        merged.update(
            self._optional_sidecar_mapping(
                _MIGRATED_IDENTITY_SIDECAR_REL_PATH,
                label="live migrated ULID sidecar",
            )
        )
        return merged

    def _projected_sidecar_mappings(
        self,
        pending: _PendingBatch,
    ) -> dict[str, str]:
        sidecar_member = next(
            (
                member
                for member in pending.members
                if member.kind == "identity_sidecar_replace_exact"
            ),
            None,
        )
        primary_before = self._identity_sidecar_before_mapping(
            pending,
            sidecar_member,
        )
        migrated = self._migrated_sidecar_mapping(pending)
        canonical_primary_before = self._canonicalized_sidecar_before(
            pending,
            primary_before,
            migrated,
        )
        primary_after, _changed = _derive_sidecar_transition(
            canonical_primary_before,
            pending.members,
            migrated=migrated,
        )
        merged = dict(primary_after)
        merged.update(migrated)
        return merged

    def _canonicalized_sidecar_before(
        self,
        pending: _PendingBatch,
        primary_before: Mapping[str, str],
        migrated: Mapping[str, str],
    ) -> dict[str, str]:
        frontmatter_ids, aliases = self._recovery_case_canonicalization_inventory(pending)
        operation_paths = tuple(
            rel_path
            for member in pending.members
            if member.kind
            in {
                "create_exact",
                "replace_exact",
                "move_replace_exact",
            }
            for rel_path in (
                member.target_rel_path,
                *((member.source_rel_path,) if member.source_rel_path is not None else ()),
            )
        )
        operation_result_ids = tuple(
            member.note_id
            for member in pending.members
            if member.kind
            in {
                "create_exact",
                "replace_exact",
                "move_replace_exact",
            }
            and member.note_id is not None
        )
        operation_result_aliases = tuple(
            alias
            for member in pending.members
            if member.kind
            in {
                "create_exact",
                "replace_exact",
                "move_replace_exact",
            }
            and member.aliases is not None
            for alias in member.aliases
        )
        try:
            canonical, derived = canonicalize_identity_sidecar_case_collisions(
                primary_before,
                migrated,
                live_frontmatter_ids=frontmatter_ids,
                live_aliases=aliases,
                operation_paths=operation_paths,
                operation_result_ids=operation_result_ids,
                operation_result_aliases=operation_result_aliases,
            )
        except OrganizationManifestError as exc:
            raise OperationLogError(
                f"identity sidecar case canonicalization cannot be re-derived: {exc}"
            ) from exc
        if derived != pending.identity_sidecar_case_canonicalizations:
            raise OperationLogError(
                "identity sidecar case canonicalizations differ from pending receipt"
            )
        return canonical

    def _recovery_case_canonicalization_inventory(
        self,
        pending: _PendingBatch,
    ) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
        effect_paths = _note_effect_path_identities(pending.members)
        identities: dict[str, _RecoveryIdentity] = {}
        for identity, (rel_path, path) in self._live_admitted_note_paths().items():
            if identity in effect_paths:
                continue
            identities[rel_path] = _recovery_identity_from_bytes(
                rel_path,
                _read_bounded_file(
                    path,
                    limit=MAX_PAYLOAD_BYTES,
                    label=f"admitted note {rel_path}",
                ),
                id_mappings={},
            )
        for member in pending.members:
            historical = self._historical_recovery_identity(member)
            if historical is not None:
                identities[historical.rel_path] = historical
        return (
            {rel_path: item.frontmatter_id for rel_path, item in identities.items()},
            {rel_path: item.aliases for rel_path, item in identities.items()},
        )

    def _optional_sidecar_mapping(
        self,
        rel_path: str,
        *,
        label: str,
    ) -> dict[str, str]:
        path = self._absolute_path(rel_path)
        if not path.exists():
            return {}
        return _sidecar_mapping(
            _read_bounded_file(path, limit=MAX_PAYLOAD_BYTES, label=label),
            label=label,
        )

    def _identity_sidecar_stage_error(
        self,
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
    ) -> str | None:
        sidecar_member = next(
            (
                member
                for member in pending.members
                if member.kind == "identity_sidecar_replace_exact"
            ),
            None,
        )
        try:
            before = self._identity_sidecar_before_mapping(
                pending,
                sidecar_member,
            )
            migrated = self._migrated_sidecar_mapping(pending)
            canonical_before = self._canonicalized_sidecar_before(
                pending,
                before,
                migrated,
            )
            expected_after, changed = _derive_sidecar_transition(
                canonical_before,
                pending.members,
                migrated=migrated,
            )
            changed = changed or canonical_before != before
        except (OperationLogError, UnicodeDecodeError, ValueError) as exc:
            return str(exc)
        if sidecar_member is None:
            if changed:
                return "derived move transition lacks an identity sidecar member"
            return None
        if not changed:
            return "identity sidecar member has no derived move transition"
        after_bytes = payloads[sidecar_member.stage_name]
        if after_bytes != _canonical_sidecar_bytes(expected_after):
            return "staged identity sidecar differs from the exact move transition"
        return None

    def _identity_sidecar_before_mapping(
        self,
        pending: _PendingBatch,
        sidecar_member: _PendingMember | None,
    ) -> dict[str, str]:
        if sidecar_member is not None:
            before_hash = sidecar_member.target_before_hash
            if before_hash is None:
                raise OperationLogError(
                    "identity sidecar recovery member lacks an exact baseline hash"
                )
            return _sidecar_mapping(
                self._read_history(before_hash),
                label="historical ULID sidecar baseline",
            )
        baseline = pending.parameters.get("identity_sidecar_before_sha256")
        if baseline is None:
            return {}
        return _sidecar_mapping(
            _read_bounded_file(
                self._absolute_path(_IDENTITY_SIDECAR_REL_PATH),
                limit=MAX_PAYLOAD_BYTES,
                label="live primary ULID sidecar baseline",
            ),
            label="live primary ULID sidecar baseline",
        )

    def _migrated_sidecar_mapping(
        self,
        pending: _PendingBatch,
    ) -> dict[str, str]:
        baseline = pending.parameters.get("migrated_identity_sidecar_before_sha256")
        if baseline is None:
            return {}
        migrated_path = self._absolute_path(_MIGRATED_IDENTITY_SIDECAR_REL_PATH)
        return _sidecar_mapping(
            _read_bounded_file(
                migrated_path,
                limit=MAX_PAYLOAD_BYTES,
                label="migrated ULID sidecar baseline",
            ),
            label="migrated ULID sidecar baseline",
        )

    def _config_stage_error(
        self,
        pending: _PendingBatch,
        payloads: Mapping[str, bytes],
    ) -> str | None:
        config_member = next(
            (member for member in pending.members if member.kind == "config_replace_exact"),
            None,
        )
        if config_member is None:
            return None
        try:
            live_scope = self._live_organization_scope()
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            OperationLogError,
        ) as exc:
            return f"active live organization scope is unavailable: {exc}"
        after_bytes = payloads[config_member.stage_name]
        before_hash = config_member.target_before_hash
        if before_hash is None:
            return "config recovery member lacks an exact baseline hash"
        target_scope, transition_error = self._config_transition_scope(
            before_hash,
            after_bytes,
            stage_name=config_member.stage_name,
        )
        if transition_error is not None:
            return transition_error
        if target_scope is None or _platform_path_key(target_scope) != _platform_path_key(
            live_scope
        ):
            return "organization-apply-v1 forbids organization.scope changes"
        return None

    def _config_transition_scope(
        self,
        before_hash: str,
        after_bytes: bytes,
        *,
        stage_name: str,
    ) -> tuple[str | None, str | None]:
        try:
            before_document, before_config = _config_document(
                self._read_history(before_hash),
                label="historical VAULT.yaml baseline",
            )
            target_document, target_config = _config_document(
                after_bytes,
                label=f"staged payload {stage_name}",
            )
        except (
            UnicodeDecodeError,
            ValueError,
            OperationLogError,
        ) as exc:
            return None, str(exc)
        before_non_organization = {
            key: value for key, value in before_document.items() if key != "organization"
        }
        target_non_organization = {
            key: value for key, value in target_document.items() if key != "organization"
        }
        if not _yaml_values_equal_exact(
            target_non_organization,
            before_non_organization,
        ):
            return None, "staged VAULT.yaml may change only top-level organization"
        before_organization = before_config.organization
        if (
            before_organization is None
            or not before_organization.rules
            or before_organization.scope is None
        ):
            return None, "historical VAULT.yaml lacks an active organization.scope"
        target_organization = target_config.organization
        if (
            target_organization is None
            or not target_organization.rules
            or target_organization.scope is None
        ):
            return None, "staged VAULT.yaml does not declare an active organization.scope"
        if _platform_path_key(before_organization.scope) != _platform_path_key(
            target_organization.scope
        ):
            return None, "organization-apply-v1 forbids organization.scope changes"
        return target_organization.scope, None

    def _receipt_state(self, pending: _PendingBatch) -> tuple[str | None, bool]:
        path = self._assert_internal_path(
            self._committed_path(pending.batch_id),
            allow_missing=True,
        )
        if not path.exists():
            return None, False
        if not path.is_file():
            return "committed receipt is not a regular file", True
        try:
            result = _read_result(path)
        except (BatchConflictError, OperationLogError) as exc:
            return str(exc), True
        expected = pending.to_result(already_committed=False)
        if result != expected:
            return "committed receipt differs from pending batch", True
        return None, True

    def _read_committed_for_pending(
        self,
        pending: _PendingBatch,
    ) -> BatchApplyResult | None:
        error, present = self._receipt_state(pending)
        if error is not None:
            raise RecoveryRequiredError(
                f"Recovery required: committed receipt differs from pending batch: {error}"
            )
        if not present:
            return None
        path = self._assert_internal_path(
            self._committed_path(pending.batch_id),
            allow_missing=False,
        )
        result = _read_result(path)
        if result != pending.to_result(already_committed=False):
            raise RecoveryRequiredError(
                "Recovery required: committed receipt changed during recovery"
            )
        self._verify_result_records(result)
        return result

    def _cross_batch_blockers(
        self,
        batches: tuple[_PendingBatch, ...],
    ) -> tuple[BlockedOperation, ...]:
        if len(batches) < 2:
            return ()
        # One pending receipt authenticates a complete organization-scope
        # snapshot plus the config and both identity-sidecar baselines.  Any
        # committed member from another receipt can invalidate one of those
        # predicates (including an absent CREATE target, which is not listed in
        # scope_note_preconditions).  The public writer prevents concurrent
        # publication, so multiple receipts are an exceptional recovery state:
        # fail closed on every member before the first byte is mutated.
        return tuple(
            self._blocked_member(
                member,
                reason=_REASON_CROSS_BATCH_EFFECT,
            )
            for pending in batches
            for member in pending.members
        )

    def _record_matches_member(
        self,
        record: OperationRecord,
        pending: _PendingBatch,
        member: _PendingMember,
    ) -> bool:
        return (
            record.operation_id == member.operation_id
            and record.op == pending.op
            and record.tool == pending.tool
            and record.note_id == member.note_id
            and record.rel_path == member.target_rel_path
            and record.before_hash == member.result_before_hash
            and record.after_hash == member.after_hash
            and record.actor == pending.actor
            and record.parameters == self._record_parameters(pending, member)
            and record.history_stored is (member.result_before_hash is not None)
        )

    @staticmethod
    def _record_parameters(
        pending: _PendingBatch,
        member: _PendingMember,
    ) -> dict[str, JsonScalar]:
        parameters = dict(pending.parameters)
        parameters.update(
            {
                "batch_member_kind": member.kind,
                "source_rel_path": member.source_rel_path,
                "target_before_hash": member.target_before_hash,
            }
        )
        return parameters

    def _blocked_member(
        self,
        member: _PendingMember,
        *,
        reason: str,
        rel_path: str | None = None,
    ) -> BlockedOperation:
        return BlockedOperation(
            operation_id=member.operation_id,
            rel_path=rel_path or member.target_rel_path,
            reason=reason,
            expected_before_hash=member.result_before_hash,
            expected_after_hash=member.after_hash,
            disk_hash=self._disk_hash(rel_path or member.target_rel_path),
            restore_before_available=False,
            adopt_disk_available=False,
        )

    def _assert_journal_roots(self) -> None:
        for rel_path in (_HISTORY_REL_ROOT, ".datacron/oplog"):
            self._assert_internal_path(
                self._vault_root / Path(rel_path),
                allow_missing=True,
            )

    def _assert_operations_path(self) -> None:
        self._assert_journal_roots()
        self._assert_internal_path(
            self._vault_root / Path(_OPERATIONS_REL_PATH),
            allow_missing=True,
        )

    def _store_history(self, data: bytes) -> str:
        self._assert_journal_roots()
        self._assert_internal_path(
            self._vault_root / Path(_HISTORY_REL_ROOT) / sha256_bytes(data),
            allow_missing=True,
        )
        return self._journal.store_history(data)

    def _read_history(self, content_hash: str) -> bytes:
        self._assert_journal_roots()
        self._assert_internal_path(
            self._vault_root / Path(_HISTORY_REL_ROOT) / content_hash,
            allow_missing=True,
        )
        return self._journal.read_history(content_hash)

    def _read_records(self) -> list[OperationRecord]:
        self._assert_operations_path()
        return self._journal.read_records()

    def _next_timestamp(self) -> str:
        self._assert_operations_path()
        return self._journal.next_timestamp()

    def _append_record(self, record: OperationRecord) -> None:
        self._assert_operations_path()
        self._journal.append_record(record)

    def _write_pending(self, pending: _PendingBatch) -> None:
        receipt = _pending_bytes(pending)
        if len(receipt) > _MAX_PENDING_BYTES:
            raise BatchConflictError(
                f"pending organization batch exceeds {_MAX_PENDING_BYTES} bytes"
            )
        self._assert_internal_path(self._pending_root, allow_missing=True)
        _ensure_directory_durable(self._pending_root)
        pending_path = self._assert_internal_path(
            self._pending_path(pending.batch_id),
            allow_missing=True,
        )
        atomic_durable_write(pending_path, receipt)

    def _write_result(self, result: BatchApplyResult) -> None:
        receipt = _result_bytes(result)
        if len(receipt) > _MAX_RESULT_BYTES:
            raise BatchConflictError(f"organization batch result exceeds {_MAX_RESULT_BYTES} bytes")
        self._assert_internal_path(self._committed_root, allow_missing=True)
        _ensure_directory_durable(self._committed_root)
        committed_path = self._assert_internal_path(
            self._committed_path(result.batch_id),
            allow_missing=True,
        )
        atomic_durable_write(committed_path, receipt)

    def _verify_result_records(self, result: BatchApplyResult) -> None:
        operation_prefix = f"organization-{result.manifest_sha256}-"
        batch_records = tuple(
            record
            for record in self._read_records()
            if record.operation_id.startswith(operation_prefix)
            or record.parameters.get("manifest_sha256") == result.manifest_sha256
        )
        expected_ids = {member.operation_id for member in result.members}
        actual_ids = {record.operation_id for record in batch_records}
        if len(batch_records) != len(expected_ids) or actual_ids != expected_ids:
            raise RecoveryRequiredError(
                "Recovery required: organization receipt lacks the complete operation-record set"
            )
        records = {record.operation_id: record for record in batch_records}
        expected_operation_count = sum(
            member.kind != "identity_sidecar_replace_exact" for member in result.members
        )
        expected_config_replaced = any(
            member.kind == "config_replace_exact" for member in result.members
        )
        expected_sidecar_replaced = any(
            member.kind == "identity_sidecar_replace_exact" for member in result.members
        )
        for member in result.members:
            record = records.get(member.operation_id)
            if record is None:
                raise RecoveryRequiredError(
                    "Recovery required: organization receipt lacks an operation record"
                )
            if (
                record.op != "apply_organization_manifest"
                or record.tool != record.op
                or record.rel_path != member.target_rel_path
                or record.before_hash != member.before_hash
                or record.after_hash != member.after_hash
                or record.note_id != member.note_id
                or record.parameters.get("manifest_sha256") != result.manifest_sha256
                or record.parameters.get("batch_id") != result.batch_id
                or record.parameters.get("confirmation_token") != result.confirmation_token
                or record.parameters.get("projected_report_sha256")
                != result.projected_report_sha256
                or record.parameters.get("payload_set_sha256") != result.payload_set_sha256
                or record.parameters.get("scope_digest") != result.scope_digest
                or record.parameters.get("config_before_sha256") != result.config_before_sha256
                or record.parameters.get("batch_member_kind") != member.kind
                or record.parameters.get("source_rel_path") != member.source_rel_path
                or type(record.parameters.get("member_count")) is not int
                or record.parameters.get("member_count") != len(result.members)
                or type(record.parameters.get("operation_count")) is not int
                or record.parameters.get("operation_count") != expected_operation_count
                or record.parameters.get("config_replaced") is not expected_config_replaced
                or record.parameters.get("identity_sidecar_replaced")
                is not expected_sidecar_replaced
                or record.parameters.get("identity_sidecar_case_canonicalization_count")
                != result.identity_sidecar_case_canonicalization_count
                or record.parameters.get("identity_sidecar_case_canonicalization_sha256")
                != result.identity_sidecar_case_canonicalization_sha256
                or record.history_stored is not (member.before_hash is not None)
            ):
                raise RecoveryRequiredError(
                    "Recovery required: organization receipt differs from operation log"
                )

    def _read_pending_snapshot(self, path: Path) -> _PendingSnapshot:
        safe_path = self._assert_internal_path(path, allow_missing=False)
        try:
            raw_bytes = _read_bounded_file(
                safe_path,
                limit=_MAX_PENDING_BYTES,
                label="pending organization batch",
            )
            pending = _parse_pending(raw_bytes)
        except (BatchConflictError, OperationLogError) as exc:
            raise RecoveryRequiredError(
                f"Recovery required: corrupt pending organization batch {safe_path.name}"
            ) from exc
        if safe_path.stem != pending.batch_id:
            raise OperationLogError("pending batch filename differs from batch id")
        return _PendingSnapshot(
            pending=pending,
            path=safe_path,
            raw_bytes=raw_bytes,
        )

    def _read_pending_stable(self, path: Path) -> _PendingBatch:
        snapshot = self._read_pending_snapshot(path)
        self._assert_pending_snapshot(snapshot)
        return snapshot.pending

    def _assert_pending_snapshot(self, snapshot: _PendingSnapshot) -> None:
        safe_path = self._assert_internal_path(snapshot.path, allow_missing=False)
        if not safe_path.is_file():
            raise RecoveryRequiredError(
                "Recovery required: pending organization batch disappeared or changed"
            )
        current = _read_bounded_file(
            safe_path,
            limit=_MAX_PENDING_BYTES,
            label="pending organization batch",
        )
        if current != snapshot.raw_bytes:
            raise RecoveryRequiredError(
                "Recovery required: pending organization batch changed during recovery"
            )

    def _pending_is_published_exact(self, pending: _PendingBatch) -> bool:
        path = self._assert_internal_path(
            self._pending_path(pending.batch_id),
            allow_missing=True,
        )
        if not path.exists():
            return False
        if not path.is_file():
            raise RecoveryRequiredError(
                "Recovery required: pending organization batch is not a regular file"
            )
        actual = _read_bounded_file(
            path,
            limit=_MAX_PENDING_BYTES,
            label="pending organization batch",
        )
        if actual != _pending_bytes(pending):
            raise RecoveryRequiredError(
                "Recovery required: pending organization batch publication is ambiguous"
            )
        return True

    def _pending_paths(self) -> tuple[Path, ...]:
        pending_root = self._assert_internal_path(self._pending_root, allow_missing=True)
        if not pending_root.exists():
            return ()
        if not pending_root.is_dir():
            raise RecoveryRequiredError("Recovery required: batch pending root is not a directory")
        paths: list[Path] = []
        for path in sorted(pending_root.iterdir()):
            safe_path = self._assert_internal_path(path, allow_missing=False)
            if _ATOMIC_RECEIPT_TEMP_PATTERN.fullmatch(safe_path.name) and safe_path.is_file():
                continue
            if not safe_path.is_file() or safe_path.suffix != ".json":
                raise RecoveryRequiredError(
                    f"Recovery required: unexpected batch pending entry {safe_path.name}"
                )
            paths.append(safe_path)
        return tuple(paths)

    def _cleanup_atomic_receipt_temps(self) -> None:
        for root in (self._pending_root, self._committed_root):
            safe_root = self._assert_internal_path(root, allow_missing=True)
            if not safe_root.exists():
                continue
            if not safe_root.is_dir():
                raise RecoveryRequiredError(
                    "Recovery required: organization batch receipt root is not a directory"
                )
            for candidate in sorted(safe_root.iterdir()):
                if not _ATOMIC_RECEIPT_TEMP_PATTERN.fullmatch(candidate.name):
                    continue
                safe_candidate = self._assert_internal_path(
                    candidate,
                    allow_missing=False,
                )
                if not safe_candidate.is_file():
                    raise RecoveryRequiredError(
                        "Recovery required: atomic receipt temporary is not a regular file"
                    )
                self._delete_file_durable(safe_candidate)

    def _remove_pending(self, batch_id: str) -> None:
        path = self._assert_internal_path(self._pending_path(batch_id), allow_missing=True)
        if not path.exists():
            return
        path.unlink()
        durable_flush_directory(path.parent)

    def _remove_stage(self, batch_id: str) -> None:
        stage_dir = self._assert_internal_path(self._stage_dir(batch_id), allow_missing=True)
        if not stage_dir.exists():
            return
        if not stage_dir.is_dir():
            raise RecoveryRequiredError("Recovery required: batch stage is not a directory")
        for path in sorted(stage_dir.iterdir()):
            safe_path = self._assert_internal_path(path, allow_missing=False)
            if not safe_path.is_file():
                raise OperationLogError(f"unexpected batch stage entry: {path}")
            safe_path.unlink()
        durable_flush_directory(stage_dir)
        stage_dir.rmdir()
        durable_flush_directory(stage_dir.parent)

    def _remove_orphan_stages(self, pending_ids: set[str]) -> None:
        stage_root = self._assert_internal_path(self._stage_root, allow_missing=True)
        if not stage_root.exists():
            return
        if not stage_root.is_dir():
            raise RecoveryRequiredError("Recovery required: batch stage root is not a directory")
        for stage_dir in sorted(stage_root.iterdir()):
            safe_stage_dir = self._assert_internal_path(stage_dir, allow_missing=False)
            if not _HASH_PATTERN.fullmatch(safe_stage_dir.name):
                raise RecoveryRequiredError(
                    "Recovery required: unexpected organization batch stage directory"
                )
            if not safe_stage_dir.is_dir() or safe_stage_dir.name in pending_ids:
                continue
            self._remove_stage(safe_stage_dir.name)

    @staticmethod
    def _delete_file_durable(path: Path) -> None:
        if not path.exists():
            return
        if not path.is_file():
            raise OperationLogError(f"batch path is not a regular file: {path}")
        path.unlink()
        durable_flush_directory(path.parent)

    @staticmethod
    def _remove_empty_directory(path: Path) -> None:
        if not path.is_dir():
            return
        try:
            path.rmdir()
        except OSError:
            return
        durable_flush_directory(path.parent)

    def _disk_hash(self, rel_path: str) -> str | None:
        path = self._absolute_path(rel_path)
        if not path.exists():
            return None
        if not path.is_file():
            raise BatchConflictError(f"batch path is not a regular file: {rel_path}")
        return _stream_sha256(path)

    def _absolute_path(self, rel_path: str) -> Path:
        candidate = self._vault_root / Path(rel_path)
        try:
            safe = assert_path_chain_without_links(
                candidate,
                anchor=self._vault_root,
                allow_missing=True,
            )
        except (ValueError, OSError) as exc:
            raise RecoveryRequiredError(f"Recovery required: unsafe batch path {rel_path}") from exc
        self._relative_path(safe)
        return safe

    def _relative_path(self, path: Path) -> str:
        try:
            safe = assert_path_chain_without_links(
                path.expanduser(),
                anchor=self._vault_root,
                allow_missing=True,
            )
            relative = safe.relative_to(self._vault_root)
        except (OSError, ValueError) as exc:
            raise BatchConflictError(f"batch path is outside vault root: {path}") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise BatchConflictError(f"invalid vault-relative batch path: {path}")
        return relative.as_posix()

    def _missing_parent_dirs(self, target_rel_path: str) -> tuple[str, ...]:
        target = self._absolute_path(target_rel_path)
        missing: list[str] = []
        current = target.parent
        while current != self._vault_root and not current.exists():
            missing.append(self._relative_path(current))
            current = current.parent
        if not current.is_dir():
            raise BatchConflictError(f"batch target parent is not a directory: {target_rel_path}")
        return tuple(reversed(missing))

    def _stage_dir(self, batch_id: str) -> Path:
        return self._stage_root / batch_id

    def _pending_path(self, batch_id: str) -> Path:
        return self._pending_root / f"{batch_id}.json"

    def _committed_path(self, batch_id: str) -> Path:
        return self._committed_root / f"{batch_id}.json"

    def _assert_internal_path(self, path: Path, *, allow_missing: bool) -> Path:
        try:
            return assert_path_chain_without_links(
                path,
                anchor=self._vault_root,
                allow_missing=allow_missing,
            )
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                f"Recovery required: unsafe organization batch sidecar path {path}"
            ) from exc


def _operation_sort_key(
    resolved: ResolvedOrganizationOperation,
) -> tuple[str, str, str]:
    source = str(resolved.source_path) if resolved.source_path is not None else ""
    return (
        str(resolved.target_path).replace("\\", "/").casefold(),
        resolved.kind,
        source.replace("\\", "/").casefold(),
    )


def _operation_id(manifest_sha256: str, index: int) -> str:
    return f"organization-{manifest_sha256}-{index:04d}"


def _stage_name(index: int) -> str:
    return f"{index:04d}.after"


def _path_identity(rel_path: str) -> str:
    return rel_path.replace("\\", "/").casefold()


def _path_states(pending: _PendingBatch) -> tuple[_PathState, ...]:
    targets = tuple(
        _PathState(
            member=member,
            rel_path=member.target_rel_path,
            before_hash=member.target_before_hash,
            after_hash=member.after_hash,
            is_source=False,
        )
        for member in pending.members
    )
    sources = tuple(
        _PathState(
            member=member,
            rel_path=member.source_rel_path,
            before_hash=member.source_before_hash,
            after_hash=None,
            is_source=True,
        )
        for member in pending.members
        if member.source_rel_path is not None
    )
    return targets + sources


def _clean_parameters(parameters: Mapping[str, JsonScalar]) -> dict[str, JsonScalar]:
    cleaned: dict[str, JsonScalar] = {}
    for key, value in parameters.items():
        if not isinstance(key, str):
            raise TypeError("operation parameter keys must be strings")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError("operation parameters must be scalar JSON values")
        cleaned[key] = value
    return cleaned


def _prepare_scope_note_preconditions(
    items: Iterable[OrganizationScopeNotePrecondition],
) -> tuple[_ScopeNotePrecondition, ...]:
    prepared = tuple(
        sorted(
            (_ScopeNotePrecondition(rel_path=item.rel_path, sha256=item.sha256) for item in items),
            key=lambda item: (_path_identity(item.rel_path), item.rel_path),
        )
    )
    if len(prepared) > _MAX_SCOPE_NOTE_PRECONDITIONS:
        raise BatchConflictError("organization batch exceeds the scope precondition limit")
    seen: set[str] = set()
    for item in prepared:
        try:
            _validate_pending_note_path(item.rel_path)
            _require_hash(item.sha256, "scope note precondition sha256")
        except (BatchConflictError, OperationLogError) as exc:
            raise BatchConflictError("invalid scope note precondition") from exc
        identity = _path_identity(item.rel_path)
        if identity in seen:
            raise BatchConflictError("duplicate scope note precondition path")
        seen.add(identity)
    return prepared


def _scope_preconditions_sha256(
    items: Iterable[_ScopeNotePrecondition],
) -> str:
    payload = [{"rel_path": item.rel_path, "sha256": item.sha256} for item in items]
    return sha256_bytes(_json_bytes(payload))


def _bounded_actor(actor: str) -> str:
    normalized = actor.strip() or "mcp-client:unidentified"
    if len(normalized) > _MAX_ACTOR_LENGTH:
        raise BatchConflictError(f"organization batch actor exceeds {_MAX_ACTOR_LENGTH} characters")
    return normalized


def _pending_parameter_hash(
    parameters: Mapping[str, JsonScalar],
    key: str,
) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise OperationLogError(f"pending batch parameter {key} is not a SHA-256")
    return value


def _note_effect_path_identities(
    members: Iterable[_PendingMember],
) -> set[str]:
    effects: set[str] = set()
    for member in members:
        if member.kind not in {
            "create_exact",
            "replace_exact",
            "move_replace_exact",
        }:
            continue
        effects.add(_path_identity(member.target_rel_path))
        if member.source_rel_path is not None:
            effects.add(_path_identity(member.source_rel_path))
    return effects


def _fallback_note_id(rel_path: str) -> str:
    digest = hashlib.sha256(f"datacron-rel-path-id\x00{rel_path}".encode()).digest()
    return str(ULID.from_bytes(digest[:16]))


def _recovery_identity_from_bytes(
    rel_path: str,
    raw_bytes: bytes,
    *,
    id_mappings: Mapping[str, str],
    expected_note_id: str | None = None,
    expected_aliases: tuple[str, ...] | None = None,
) -> _RecoveryIdentity:
    text = raw_bytes.decode("utf-8", errors="strict")
    metadata, body = parse_organization_note_strict(text)
    frontmatter_id = metadata.get("id")
    note_id = expected_note_id
    if note_id is None:
        note_id = (
            frontmatter_id
            if isinstance(frontmatter_id, str) and len(frontmatter_id) == 26
            else id_mappings.get(rel_path, _fallback_note_id(rel_path))
        )
    aliases = expected_aliases
    if aliases is None:
        aliases = tuple(
            coerce_string_list(
                metadata.get("aliases"),
                keep_empty_scalar=True,
            )
        )
    return _RecoveryIdentity(
        rel_path=rel_path,
        note_id=note_id,
        frontmatter_id=(frontmatter_id if isinstance(frontmatter_id, str) else None),
        title=resolve_note_title(
            metadata,
            body,
            Path(rel_path),
            h1_pattern=_H1_PATTERN,
            empty_h1_falls_back=True,
        ),
        aliases=aliases,
    )


def _projected_id_collision_error(
    projected: Iterable[_RecoveryIdentity],
) -> str | None:
    ids: dict[str, str] = {}
    for item in projected:
        normalized_id = item.note_id.casefold()
        previous_path = ids.get(normalized_id)
        if previous_path is not None and previous_path != item.rel_path:
            return (
                f"projected note id {item.note_id!r} collides between "
                f"{previous_path!r} and {item.rel_path!r}"
            )
        ids[normalized_id] = item.rel_path
    return None


def _projected_sidecar_identity_error(
    projected: tuple[_RecoveryIdentity, ...],
    mappings: Mapping[str, str],
) -> str | None:
    for result in projected:
        for mapped_path, mapped_id in mappings.items():
            if mapped_id.casefold() != result.note_id.casefold():
                continue
            if _path_identity(mapped_path) != _path_identity(result.rel_path):
                return (
                    f"projected note id {result.note_id!r} is reserved by "
                    f"sidecar path {mapped_path!r}"
                )
    return None


def _projected_alias_resolution_error(
    baseline: tuple[_RecoveryIdentity, ...],
    projected: tuple[_RecoveryIdentity, ...],
    results: tuple[_RecoveryIdentity, ...],
) -> str | None:
    baseline_index = _recovery_alias_index(baseline)
    projected_index = _recovery_alias_index(projected)
    result_paths = {item.rel_path.casefold() for item in results}
    for item in projected:
        for alias in item.aliases:
            normalized = alias.strip().lower()
            is_result = item.rel_path.casefold() in result_paths
            owned_before = baseline_index.get(normalized) == item.note_id
            if not is_result and not owned_before:
                continue
            if projected_index.get(normalized) != item.note_id:
                return (
                    f"projected alias {alias!r} no longer resolves to "
                    f"{item.note_id!r} for {item.rel_path!r}"
                )
    return None


def _recovery_alias_index(
    items: tuple[_RecoveryIdentity, ...],
) -> dict[str, str | None]:
    return build_tiered_alias_index(
        items,
        identity=lambda item: item.note_id,
        title=lambda item: (item.title,),
        stem=lambda item: (item.stem,),
        aliases=lambda item: item.aliases,
        normalize=lambda value: value.strip().lower(),
    )


def _sidecar_mapping(data: bytes, *, label: str) -> dict[str, str]:
    try:
        payload = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperationLogError(f"invalid {label} JSON") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()
    ):
        raise OperationLogError(f"{label} must contain only string pairs")
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _canonical_sidecar_bytes(mappings: Mapping[str, str]) -> bytes:
    rendered = json.dumps(
        mappings,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"{rendered}\n".encode()


def _derive_sidecar_transition(
    before: Mapping[str, str],
    members: Iterable[_PendingMember],
    *,
    migrated: Mapping[str, str],
) -> tuple[dict[str, str], bool]:
    updated = dict(before)
    normalized: dict[str, tuple[str, str]] = {}
    for rel_path, mapping_note_id in before.items():
        key = _platform_path_key(PurePosixPath(rel_path).as_posix())
        if key in normalized:
            raise OperationLogError(
                f"historical ULID sidecar contains a case-colliding path: {rel_path!r}"
            )
        normalized[key] = (rel_path, mapping_note_id)
    normalized_migrated: dict[str, str] = {}
    for rel_path in migrated:
        key = _platform_path_key(PurePosixPath(rel_path).as_posix())
        if key in normalized_migrated:
            raise OperationLogError(
                f"migrated ULID sidecar contains a case-colliding path: {rel_path!r}"
            )
        normalized_migrated[key] = rel_path
    changed = False
    for member in members:
        if member.kind != "move_replace_exact":
            continue
        source_rel_path = member.source_rel_path
        member_note_id = member.note_id
        if source_rel_path is None or member_note_id is None:
            raise OperationLogError("pending move lacks exact sidecar identity inputs")
        source_key = _platform_path_key(PurePosixPath(source_rel_path).as_posix())
        target_key = _platform_path_key(PurePosixPath(member.target_rel_path).as_posix())
        if source_key in normalized_migrated:
            raise OperationLogError(
                f"move source is reserved by migrated ULID sidecar: {source_rel_path!r}"
            )
        source_mapping = normalized.get(source_key)
        if source_mapping is None:
            continue
        mapped_path, mapped_id = source_mapping
        if mapped_id != member_note_id:
            raise OperationLogError(
                f"historical ULID sidecar identity differs for {source_rel_path!r}"
            )
        if target_key in normalized or target_key in normalized_migrated:
            raise OperationLogError(
                f"historical ULID sidecar target already exists: {member.target_rel_path!r}"
            )
        del updated[mapped_path]
        updated[member.target_rel_path] = mapped_id
        normalized.pop(source_key)
        normalized[target_key] = (member.target_rel_path, mapped_id)
        changed = True
    return updated, changed


def _pending_bytes(pending: _PendingBatch) -> bytes:
    payload = {
        "schema": _BATCH_SCHEMA,
        "batch_id": pending.batch_id,
        "manifest_sha256": pending.manifest_sha256,
        "confirmation_token": pending.confirmation_token,
        "projected_report_sha256": pending.projected_report_sha256,
        "op": pending.op,
        "tool": pending.tool,
        "actor": pending.actor,
        "parameters": pending.parameters,
        "scope_note_preconditions": [
            {
                "rel_path": item.rel_path,
                "sha256": item.sha256,
            }
            for item in pending.scope_note_preconditions
        ],
        "identity_sidecar_case_canonicalizations": (
            _identity_sidecar_case_canonicalization_payloads(
                pending.identity_sidecar_case_canonicalizations
            )
        ),
        "members": [
            {
                "operation_id": member.operation_id,
                "kind": member.kind,
                "source_rel_path": member.source_rel_path,
                "target_rel_path": member.target_rel_path,
                "source_before_hash": member.source_before_hash,
                "target_before_hash": member.target_before_hash,
                "after_hash": member.after_hash,
                "note_id": member.note_id,
                "before_aliases": (
                    list(member.before_aliases) if member.before_aliases is not None else None
                ),
                "aliases": (list(member.aliases) if member.aliases is not None else None),
                "stage_name": member.stage_name,
                "created_parent_dirs": list(member.created_parent_dirs),
            }
            for member in pending.members
        ],
    }
    return _json_bytes(payload)


def _parse_pending(data: bytes) -> _PendingBatch:
    payload = _json_object(data, "pending batch")
    _require_exact_keys(payload, _PENDING_KEYS, "pending batch")
    if payload.get("schema") != _BATCH_SCHEMA:
        raise OperationLogError("unsupported pending batch schema")
    batch_id = _required_hash(payload, "batch_id")
    manifest_sha256 = _required_hash(payload, "manifest_sha256")
    confirmation_token = _required_hash(payload, "confirmation_token")
    projected_report_sha256 = _required_hash(payload, "projected_report_sha256")
    if batch_id != manifest_sha256:
        raise OperationLogError("pending batch id differs from manifest hash")
    members_payload = payload.get("members")
    if not isinstance(members_payload, list) or not members_payload:
        raise OperationLogError("pending batch members must be a non-empty array")
    members = tuple(_parse_pending_member(item) for item in members_payload)
    scope_note_preconditions = _parse_scope_note_preconditions(
        payload.get("scope_note_preconditions")
    )
    identity_sidecar_case_canonicalizations = _parse_identity_sidecar_case_canonicalizations(
        payload.get("identity_sidecar_case_canonicalizations")
    )
    parameters_payload = payload.get("parameters")
    if not isinstance(parameters_payload, dict):
        raise OperationLogError("pending batch parameters must be an object")
    parameters = _clean_parameters(parameters_payload)
    pending = _PendingBatch(
        batch_id=batch_id,
        manifest_sha256=manifest_sha256,
        confirmation_token=confirmation_token,
        projected_report_sha256=projected_report_sha256,
        op=_required_string(payload, "op"),
        tool=_required_string(payload, "tool"),
        actor=_required_actor(payload),
        parameters=parameters,
        members=members,
        scope_note_preconditions=scope_note_preconditions,
        identity_sidecar_case_canonicalizations=(identity_sidecar_case_canonicalizations),
    )
    _validate_pending(pending)
    return pending


def _parse_pending_member(payload: object) -> _PendingMember:
    if not isinstance(payload, dict):
        raise OperationLogError("pending batch member must be an object")
    _require_exact_keys(payload, _PENDING_MEMBER_KEYS, "pending batch member")
    kind = payload.get("kind")
    allowed_kinds = {
        "create_exact",
        "replace_exact",
        "move_replace_exact",
        "config_replace_exact",
        "identity_sidecar_replace_exact",
    }
    if kind not in allowed_kinds:
        raise OperationLogError("invalid pending batch member kind")
    source_rel_path = _optional_string(payload, "source_rel_path")
    note_id = _optional_string(payload, "note_id")
    before_aliases = _optional_string_tuple(payload, "before_aliases")
    aliases = _optional_string_tuple(payload, "aliases")
    member = _PendingMember(
        operation_id=_required_string(payload, "operation_id"),
        kind=kind,
        source_rel_path=source_rel_path,
        target_rel_path=_required_string(payload, "target_rel_path"),
        source_before_hash=_optional_hash(payload, "source_before_hash"),
        target_before_hash=_optional_hash(payload, "target_before_hash"),
        after_hash=_required_hash(payload, "after_hash"),
        note_id=note_id,
        before_aliases=before_aliases,
        aliases=aliases,
        stage_name=_required_string(payload, "stage_name"),
        created_parent_dirs=_string_tuple(payload, "created_parent_dirs"),
    )
    if member.kind == "move_replace_exact" and member.source_rel_path is None:
        raise OperationLogError("move pending member requires a source path")
    return member


def _parse_scope_note_preconditions(
    payload: object,
) -> tuple[_ScopeNotePrecondition, ...]:
    if not isinstance(payload, list):
        raise OperationLogError("scope_note_preconditions must be an array")
    if len(payload) > _MAX_SCOPE_NOTE_PRECONDITIONS:
        raise OperationLogError("pending batch exceeds the scope precondition limit")
    result: list[_ScopeNotePrecondition] = []
    for item in payload:
        if not isinstance(item, dict):
            raise OperationLogError("scope note precondition must be an object")
        _require_exact_keys(
            item,
            _SCOPE_NOTE_PRECONDITION_KEYS,
            "scope note precondition",
        )
        result.append(
            _ScopeNotePrecondition(
                rel_path=_required_string(item, "rel_path"),
                sha256=_required_hash(item, "sha256"),
            )
        )
    return tuple(result)


def _identity_sidecar_case_canonicalization_payloads(
    canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...],
) -> list[dict[str, str]]:
    return [
        {
            "stale_path": item.stale_path,
            "stale_id": item.stale_id,
            "live_path": item.live_path,
            "live_id": item.live_id,
        }
        for item in canonicalizations
    ]


def _parse_identity_sidecar_case_canonicalizations(
    payload: object,
) -> tuple[IdentitySidecarCaseCanonicalization, ...]:
    if not isinstance(payload, list):
        raise OperationLogError("identity_sidecar_case_canonicalizations must be an array")
    if len(payload) > _MAX_IDENTITY_SIDECAR_CASE_CANONICALIZATIONS:
        raise OperationLogError(
            "pending batch exceeds the identity sidecar case canonicalization limit"
        )
    result: list[IdentitySidecarCaseCanonicalization] = []
    for item in payload:
        if not isinstance(item, dict):
            raise OperationLogError("identity sidecar case canonicalization must be an object")
        _require_exact_keys(
            item,
            _IDENTITY_SIDECAR_CASE_CANONICALIZATION_KEYS,
            "identity sidecar case canonicalization",
        )
        result.append(
            IdentitySidecarCaseCanonicalization(
                stale_path=_required_string(item, "stale_path"),
                stale_id=_required_string(item, "stale_id"),
                live_path=_required_string(item, "live_path"),
                live_id=_required_string(item, "live_id"),
            )
        )
    return tuple(result)


def _validate_pending(pending: _PendingBatch) -> None:
    if len(pending.members) > _MAX_PENDING_MEMBERS:
        raise OperationLogError("pending batch exceeds the member limit")
    if len(pending.scope_note_preconditions) > _MAX_SCOPE_NOTE_PRECONDITIONS:
        raise OperationLogError("pending batch exceeds the scope precondition limit")
    if (
        len(pending.identity_sidecar_case_canonicalizations)
        > _MAX_IDENTITY_SIDECAR_CASE_CANONICALIZATIONS
    ):
        raise OperationLogError(
            "pending batch exceeds the identity sidecar case canonicalization limit"
        )
    if pending.op != "apply_organization_manifest" or pending.tool != pending.op:
        raise OperationLogError("pending batch operation identity is not canonical")
    _validate_batch_parameters(pending)
    _validate_pending_member_set(pending)
    _validate_scope_note_preconditions(pending)
    _validate_identity_sidecar_case_canonicalizations(
        pending.identity_sidecar_case_canonicalizations
    )


def _validate_pending_member_set(pending: _PendingBatch) -> None:
    effects: set[str] = set()
    config_count = 0
    identity_sidecar_count = 0
    for index, member in enumerate(pending.members):
        _validate_pending_member_structure(pending.manifest_sha256, index, member)
        if member.kind == "config_replace_exact":
            config_count += 1
        if member.kind == "identity_sidecar_replace_exact":
            identity_sidecar_count += 1
            if index != len(pending.members) - 1:
                raise OperationLogError("pending identity sidecar member must be last")
        target_identity = _path_identity(member.target_rel_path)
        if target_identity in effects:
            raise OperationLogError("pending batch contains duplicate path effects")
        effects.add(target_identity)
        if member.kind == "move_replace_exact":
            source_rel_path = member.source_rel_path
            if source_rel_path is None:
                raise OperationLogError("pending move lacks a source path")
            _validate_pending_note_path(source_rel_path)
            source_identity = _path_identity(source_rel_path)
            if source_identity in effects:
                raise OperationLogError("pending batch contains duplicate path effects")
            effects.add(source_identity)
    if config_count > 1:
        raise OperationLogError("pending batch contains multiple config members")
    if identity_sidecar_count > 1:
        raise OperationLogError("pending batch contains multiple identity sidecar members")


def _validate_scope_note_preconditions(pending: _PendingBatch) -> None:
    ordered = tuple(
        sorted(
            pending.scope_note_preconditions,
            key=lambda item: (_path_identity(item.rel_path), item.rel_path),
        )
    )
    if ordered != pending.scope_note_preconditions:
        raise OperationLogError("scope note preconditions are not canonically ordered")
    preconditions: dict[str, _ScopeNotePrecondition] = {}
    for item in pending.scope_note_preconditions:
        _validate_pending_note_path(item.rel_path)
        _require_hash(item.sha256, "scope note precondition sha256")
        identity = _path_identity(item.rel_path)
        if identity in preconditions:
            raise OperationLogError("scope note preconditions contain duplicate paths")
        preconditions[identity] = item
    for member in pending.members:
        if member.kind == "create_exact":
            if _path_identity(member.target_rel_path) in preconditions:
                raise OperationLogError("pending create target exists in scope preconditions")
        elif member.kind == "replace_exact":
            replace_item = preconditions.get(_path_identity(member.target_rel_path))
            if replace_item is None or replace_item.sha256 != member.target_before_hash:
                raise OperationLogError("pending replace baseline differs from scope preconditions")
        elif member.kind == "move_replace_exact":
            source_rel_path = member.source_rel_path
            if source_rel_path is None:
                raise OperationLogError("pending move lacks a source path")
            source_item = preconditions.get(_path_identity(source_rel_path))
            if source_item is None or source_item.sha256 != member.source_before_hash:
                raise OperationLogError("pending move source differs from scope preconditions")
            if _path_identity(member.target_rel_path) in preconditions:
                raise OperationLogError("pending move target exists in scope preconditions")


def _validate_identity_sidecar_case_canonicalizations(
    canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...],
) -> None:
    ordered = tuple(
        sorted(
            canonicalizations,
            key=lambda item: (item.stale_path.casefold(), item.stale_path),
        )
    )
    if ordered != canonicalizations:
        raise OperationLogError(
            "identity sidecar case canonicalizations are not canonically ordered"
        )
    stale_paths: set[str] = set()
    live_paths: set[str] = set()
    ids: set[str] = set()
    for item in canonicalizations:
        _validate_pending_note_path(item.stale_path)
        _validate_pending_note_path(item.live_path)
        if (
            item.stale_path == item.live_path
            or item.stale_path.casefold() != item.live_path.casefold()
        ):
            raise OperationLogError(
                "identity sidecar case canonicalization paths are not distinct case aliases"
            )
        for label, note_id in (("stale", item.stale_id), ("live", item.live_id)):
            if _EXISTING_NOTE_ID_PATTERN.fullmatch(note_id) is None:
                raise OperationLogError(
                    "identity sidecar case canonicalization "
                    f"{label} id is not a bounded 26-character identity"
                )
        stale_path_key = item.stale_path.casefold()
        live_path_key = item.live_path.casefold()
        if stale_path_key in stale_paths or live_path_key in live_paths:
            raise OperationLogError(
                "identity sidecar case canonicalizations contain duplicate paths"
            )
        stale_paths.add(stale_path_key)
        live_paths.add(live_path_key)
        stale_id_key = item.stale_id.casefold()
        live_id_key = item.live_id.casefold()
        if stale_id_key == live_id_key or stale_id_key in ids or live_id_key in ids:
            raise OperationLogError(
                "identity sidecar case canonicalizations contain duplicate identities"
            )
        ids.update((stale_id_key, live_id_key))


def _validate_batch_parameters(pending: _PendingBatch) -> None:
    parameters = pending.parameters
    _require_exact_keys(parameters, _BATCH_PARAMETER_KEYS, "pending batch parameters")
    _validate_parameter_hashes(pending)
    _validate_parameter_counts(pending)
    _validate_parameter_flags(pending)
    _validate_parameter_member_baselines(pending)


def _validate_parameter_hashes(pending: _PendingBatch) -> None:
    parameters = pending.parameters
    expected_hashes = {
        "batch_id": pending.batch_id,
        "confirmation_token": pending.confirmation_token,
        "manifest_sha256": pending.manifest_sha256,
        "projected_report_sha256": pending.projected_report_sha256,
    }
    for key, expected in expected_hashes.items():
        value = parameters.get(key)
        if value != expected:
            raise OperationLogError(f"pending batch parameter {key} differs from receipt")
    for key in ("payload_set_sha256", "scope_digest"):
        value = parameters.get(key)
        if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
            raise OperationLogError(f"pending batch parameter {key} is not a SHA-256")
    config_before_sha256 = parameters.get("config_before_sha256")
    if not isinstance(config_before_sha256, str) or not _HASH_PATTERN.fullmatch(
        config_before_sha256
    ):
        raise OperationLogError("pending batch config_before_sha256 is not a SHA-256")
    for key in (
        "identity_sidecar_before_sha256",
        "migrated_identity_sidecar_before_sha256",
    ):
        value = parameters.get(key)
        if value is not None and (not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value)):
            raise OperationLogError(f"pending batch parameter {key} is not null or SHA-256")
    scope_preconditions_sha256 = parameters.get("scope_note_preconditions_sha256")
    if scope_preconditions_sha256 != _scope_preconditions_sha256(pending.scope_note_preconditions):
        raise OperationLogError("pending batch scope precondition digest differs from list")
    case_canonicalization_sha256 = parameters.get("identity_sidecar_case_canonicalization_sha256")
    if case_canonicalization_sha256 != hash_identity_sidecar_case_canonicalizations(
        pending.identity_sidecar_case_canonicalizations
    ):
        raise OperationLogError(
            "pending batch identity sidecar case canonicalization digest differs from list"
        )


def _validate_parameter_counts(pending: _PendingBatch) -> None:
    parameters = pending.parameters
    member_count = parameters.get("member_count")
    operation_count = parameters.get("operation_count")
    if type(member_count) is not int or member_count != len(pending.members):
        raise OperationLogError("pending batch member_count differs from members")
    expected_operation_count = sum(
        member.kind != "identity_sidecar_replace_exact" for member in pending.members
    )
    if (
        expected_operation_count > MAX_OPERATION_COUNT
        or type(operation_count) is not int
        or operation_count != expected_operation_count
    ):
        raise OperationLogError("pending batch operation_count differs from manifest members")
    total_payload_bytes = parameters.get("total_payload_bytes")
    if (
        type(total_payload_bytes) is not int
        or total_payload_bytes < 0
        or total_payload_bytes > MAX_TOTAL_PAYLOAD_BYTES
    ):
        raise OperationLogError("pending batch total_payload_bytes is outside limits")
    case_canonicalization_count = parameters.get("identity_sidecar_case_canonicalization_count")
    if type(case_canonicalization_count) is not int or case_canonicalization_count != len(
        pending.identity_sidecar_case_canonicalizations
    ):
        raise OperationLogError(
            "pending batch identity sidecar case canonicalization count differs from list"
        )


def _validate_parameter_flags(pending: _PendingBatch) -> None:
    parameters = pending.parameters
    config_replaced = parameters.get("config_replaced")
    expected_config_replaced = any(
        member.kind == "config_replace_exact" for member in pending.members
    )
    if type(config_replaced) is not bool or config_replaced is not expected_config_replaced:
        raise OperationLogError("pending batch config_replaced differs from members")
    identity_sidecar_replaced = parameters.get("identity_sidecar_replaced")
    expected_identity_sidecar_replaced = any(
        member.kind == "identity_sidecar_replace_exact" for member in pending.members
    )
    if (
        type(identity_sidecar_replaced) is not bool
        or identity_sidecar_replaced is not expected_identity_sidecar_replaced
    ):
        raise OperationLogError("pending batch identity_sidecar_replaced differs from members")
    if pending.identity_sidecar_case_canonicalizations and not expected_identity_sidecar_replaced:
        raise OperationLogError(
            "pending sidecar case canonicalizations lack an identity sidecar member"
        )


def _validate_parameter_member_baselines(pending: _PendingBatch) -> None:
    parameters = pending.parameters
    config_before_sha256 = parameters.get("config_before_sha256")
    config_member = next(
        (member for member in pending.members if member.kind == "config_replace_exact"),
        None,
    )
    if config_member is not None and config_member.target_before_hash != config_before_sha256:
        raise OperationLogError("pending config member differs from config baseline parameter")
    sidecar_member = next(
        (member for member in pending.members if member.kind == "identity_sidecar_replace_exact"),
        None,
    )
    if sidecar_member is not None and sidecar_member.target_before_hash != parameters.get(
        "identity_sidecar_before_sha256"
    ):
        raise OperationLogError(
            "pending identity sidecar member differs from sidecar baseline parameter"
        )


def _validate_pending_member_structure(
    manifest_sha256: str,
    index: int,
    member: _PendingMember,
) -> None:
    if not _STAGE_NAME_PATTERN.fullmatch(member.stage_name) or member.stage_name != _stage_name(
        index
    ):
        raise OperationLogError("pending batch stage name is not canonical")
    if member.operation_id != _operation_id(manifest_sha256, index):
        raise OperationLogError("pending batch operation id is not derived from manifest")
    _validate_member_note_id(member.kind, member.note_id, label="pending batch")
    _validate_member_target_identity(
        member.kind,
        member.target_rel_path,
        member.note_id,
        label="pending",
    )
    _validate_pending_member_aliases(member)
    if member.kind == "move_replace_exact":
        if member.source_rel_path is None or member.source_before_hash is None:
            raise OperationLogError("pending move lacks exact source state")
        if member.target_before_hash is not None:
            raise OperationLogError("pending move target must be absent before apply")
    elif member.source_rel_path is not None or member.source_before_hash is not None:
        raise OperationLogError("non-move pending member contains a source state")
    if member.kind == "create_exact" and member.target_before_hash is not None:
        raise OperationLogError("pending create target must be absent before apply")
    if member.kind in {
        "replace_exact",
        "config_replace_exact",
        "identity_sidecar_replace_exact",
    } and (member.target_before_hash is None):
        raise OperationLogError("pending replace lacks exact target state")
    _validate_created_parent_dirs(member)


def _validate_pending_member_aliases(member: _PendingMember) -> None:
    if member.kind in {
        "config_replace_exact",
        "identity_sidecar_replace_exact",
    }:
        if member.before_aliases is not None or member.aliases is not None:
            raise OperationLogError("pending non-note member contains identity aliases")
        return
    if member.aliases is None:
        raise OperationLogError("pending note member lacks result aliases")
    if member.kind == "create_exact":
        if member.before_aliases is not None:
            raise OperationLogError("pending create contains source aliases")
        return
    if member.before_aliases is None:
        raise OperationLogError("pending existing note lacks source aliases")


def _validate_pending_note_path(rel_path: str) -> None:
    try:
        normalize_vault_rel_path(rel_path)
    except ValueError as exc:
        raise OperationLogError(f"invalid pending note path: {rel_path!r}") from exc


def _validate_member_target_identity(
    kind: BatchMemberKind,
    target_rel_path: str,
    note_id: str | None,
    *,
    label: str,
) -> None:
    if kind in {"config_replace_exact", "identity_sidecar_replace_exact"}:
        expected_target = (
            _CONFIG_REL_PATH if kind == "config_replace_exact" else _IDENTITY_SIDECAR_REL_PATH
        )
        if target_rel_path != expected_target or note_id is not None:
            raise OperationLogError(f"{label} internal member is not canonical")
        return
    _validate_pending_note_path(target_rel_path)
    if note_id is None:
        raise OperationLogError(f"{label} note member lacks a bounded note id")


def _validate_member_note_id(
    kind: BatchMemberKind,
    note_id: str | None,
    *,
    label: str,
) -> None:
    if note_id is None:
        return
    if _EXISTING_NOTE_ID_PATTERN.fullmatch(note_id) is None:
        raise OperationLogError(f"{label} note id is not a bounded 26-character identity")
    if kind == "create_exact" and _CANONICAL_NOTE_ID_PATTERN.fullmatch(note_id) is None:
        raise OperationLogError(f"{label} create note id is not a canonical ULID")


def _validate_created_parent_dirs(member: _PendingMember) -> None:
    target_parent = PurePosixPath(member.target_rel_path).parent
    allowed_parents = {target_parent, *target_parent.parents}
    previous_depth = 0
    for rel_path in member.created_parent_dirs:
        candidate = PurePosixPath(rel_path)
        try:
            normalize_vault_rel_path(f"{candidate.as_posix()}/__batch_guard__.md")
        except ValueError as exc:
            raise OperationLogError(f"invalid pending created parent path: {rel_path!r}") from exc
        if candidate not in allowed_parents or candidate == PurePosixPath("."):
            raise OperationLogError("pending created parent is not an ancestor of target")
        depth = len(candidate.parts)
        if depth <= previous_depth:
            raise OperationLogError("pending created parent paths are not shallow-to-deep")
        previous_depth = depth


def _result_bytes(result: BatchApplyResult) -> bytes:
    payload = {
        "schema": _RESULT_SCHEMA,
        "batch_id": result.batch_id,
        "manifest_sha256": result.manifest_sha256,
        "confirmation_token": result.confirmation_token,
        "projected_report_sha256": result.projected_report_sha256,
        "payload_set_sha256": result.payload_set_sha256,
        "scope_digest": result.scope_digest,
        "config_before_sha256": result.config_before_sha256,
        "identity_sidecar_case_canonicalizations": (
            _identity_sidecar_case_canonicalization_payloads(
                result.identity_sidecar_case_canonicalizations
            )
        ),
        "members": [
            {
                "operation_id": member.operation_id,
                "kind": member.kind,
                "source_rel_path": member.source_rel_path,
                "target_rel_path": member.target_rel_path,
                "before_hash": member.before_hash,
                "after_hash": member.after_hash,
                "note_id": member.note_id,
            }
            for member in result.members
        ],
    }
    return _json_bytes(payload)


def _read_result(path: Path) -> BatchApplyResult:
    payload = _json_object(
        _read_bounded_file(
            path,
            limit=_MAX_RESULT_BYTES,
            label="organization batch result",
        ),
        "batch result",
    )
    _require_exact_keys(payload, _RESULT_KEYS, "batch result")
    if payload.get("schema") != _RESULT_SCHEMA:
        raise OperationLogError("unsupported batch result schema")
    members_payload = payload.get("members")
    if not isinstance(members_payload, list) or not members_payload:
        raise OperationLogError("batch result members must be a non-empty array")
    members = tuple(_parse_result_member(item) for item in members_payload)
    identity_sidecar_case_canonicalizations = _parse_identity_sidecar_case_canonicalizations(
        payload.get("identity_sidecar_case_canonicalizations")
    )
    result = BatchApplyResult(
        batch_id=_required_hash(payload, "batch_id"),
        manifest_sha256=_required_hash(payload, "manifest_sha256"),
        confirmation_token=_required_hash(payload, "confirmation_token"),
        projected_report_sha256=_required_hash(payload, "projected_report_sha256"),
        payload_set_sha256=_required_hash(payload, "payload_set_sha256"),
        scope_digest=_required_hash(payload, "scope_digest"),
        config_before_sha256=_required_hash(payload, "config_before_sha256"),
        members=members,
        identity_sidecar_case_canonicalizations=(identity_sidecar_case_canonicalizations),
    )
    if result.batch_id != result.manifest_sha256:
        raise OperationLogError("batch result id differs from manifest hash")
    _validate_result(result)
    return result


def _parse_result_member(payload: object) -> BatchMemberResult:
    if not isinstance(payload, dict):
        raise OperationLogError("batch result member must be an object")
    _require_exact_keys(payload, _RESULT_MEMBER_KEYS, "batch result member")
    kind = payload.get("kind")
    allowed_kinds = {
        "create_exact",
        "replace_exact",
        "move_replace_exact",
        "config_replace_exact",
        "identity_sidecar_replace_exact",
    }
    if kind not in allowed_kinds:
        raise OperationLogError("invalid batch result member kind")
    return BatchMemberResult(
        operation_id=_required_string(payload, "operation_id"),
        kind=kind,
        source_rel_path=_optional_string(payload, "source_rel_path"),
        target_rel_path=_required_string(payload, "target_rel_path"),
        before_hash=_optional_hash(payload, "before_hash"),
        after_hash=_required_hash(payload, "after_hash"),
        note_id=_optional_string(payload, "note_id"),
    )


def _validate_result(result: BatchApplyResult) -> None:
    if len(result.members) > _MAX_PENDING_MEMBERS:
        raise OperationLogError("batch result exceeds the member limit")
    if (
        len(result.identity_sidecar_case_canonicalizations)
        > _MAX_IDENTITY_SIDECAR_CASE_CANONICALIZATIONS
    ):
        raise OperationLogError(
            "batch result exceeds the identity sidecar case canonicalization limit"
        )
    _validate_identity_sidecar_case_canonicalizations(
        result.identity_sidecar_case_canonicalizations
    )
    effects: set[str] = set()
    config_count = 0
    identity_sidecar_count = 0
    for index, member in enumerate(result.members):
        _validate_result_member_structure(result.manifest_sha256, index, member)
        if member.kind == "config_replace_exact":
            config_count += 1
        if member.kind == "identity_sidecar_replace_exact":
            identity_sidecar_count += 1
            if index != len(result.members) - 1:
                raise OperationLogError("batch result identity sidecar member must be last")
        identities = [member.target_rel_path]
        if member.source_rel_path is not None:
            identities.append(member.source_rel_path)
        for rel_path in identities:
            identity = _path_identity(rel_path)
            if identity in effects:
                raise OperationLogError("batch result contains duplicate path effects")
            effects.add(identity)
    if config_count > 1:
        raise OperationLogError("batch result contains multiple config members")
    if identity_sidecar_count > 1:
        raise OperationLogError("batch result contains multiple identity sidecar members")
    if result.identity_sidecar_case_canonicalizations and identity_sidecar_count != 1:
        raise OperationLogError(
            "batch result sidecar case canonicalizations lack an identity sidecar member"
        )


def _validate_result_member_structure(
    manifest_sha256: str,
    index: int,
    member: BatchMemberResult,
) -> None:
    if member.operation_id != _operation_id(manifest_sha256, index):
        raise OperationLogError("batch result operation id is not derived from manifest")
    _validate_member_note_id(member.kind, member.note_id, label="batch result")
    _validate_member_target_identity(
        member.kind,
        member.target_rel_path,
        member.note_id,
        label="batch result",
    )
    if member.kind == "move_replace_exact":
        if member.source_rel_path is None or member.before_hash is None:
            raise OperationLogError("batch result move lacks exact source state")
        _validate_pending_note_path(member.source_rel_path)
    elif member.source_rel_path is not None:
        raise OperationLogError("non-move batch result contains a source path")
    if member.kind == "create_exact" and member.before_hash is not None:
        raise OperationLogError("batch result create must have an absent before state")
    if (
        member.kind
        in {
            "replace_exact",
            "config_replace_exact",
            "identity_sidecar_replace_exact",
        }
        and member.before_hash is None
    ):
        raise OperationLogError("batch result replace lacks exact before state")


def _json_bytes(payload: object) -> bytes:
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{rendered}\n".encode("ascii")


def _json_object(data: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(
            data.decode("ascii", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationLogError(f"invalid {label} JSON") from exc
    if not isinstance(payload, dict):
        raise OperationLogError(f"{label} must be a JSON object")
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperationLogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise OperationLogError(f"non-finite JSON number is forbidden: {value}")


def _read_bounded_file(path: Path, *, limit: int, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise OperationLogError(f"failed to read {label}: {path}") from exc
    if len(data) > limit:
        raise OperationLogError(f"{label} exceeds {limit} bytes")
    return data


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_STREAM_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise OperationLogError(f"failed to hash batch path: {path}") from exc
    return digest.hexdigest()


def _organization_scope_from_config(raw_bytes: bytes, *, label: str) -> str:
    _document, config = _config_document(raw_bytes, label=label)
    organization = config.organization
    if organization is None or not organization.rules or organization.scope is None:
        raise ValueError(f"{label} does not declare an active organization.scope")
    return organization.scope


def _config_document(
    raw_bytes: bytes,
    *,
    label: str,
) -> tuple[dict[str, object], VaultConfig]:
    text = raw_bytes.decode("utf-8", errors="strict")
    return parse_organization_config_document(text, label=label)


def _yaml_values_equal_exact(before: object, after: object) -> bool:
    if type(before) is not type(after):
        return False
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(
            _yaml_values_equal_exact(value, after[key]) for key, value in before.items()
        )
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _yaml_values_equal_exact(left, right) for left, right in zip(before, after, strict=True)
        )
    return bool(before == after)


def _platform_path_key(rel_path: str) -> str:
    normalized = PurePosixPath(rel_path.replace("\\", "/")).as_posix()
    return normalized.casefold() if os.name == "nt" else normalized


def _rel_path_belongs_to_scope(rel_path: str, scope: str) -> bool:
    path_key = _platform_path_key(rel_path)
    scope_key = _platform_path_key(scope).rstrip("/")
    return path_key.startswith(f"{scope_key}/")


def _rel_path_is_admitted(rel_path: str, policy: NoteAdmissionPolicy) -> bool:
    parts = PurePosixPath(rel_path).parts
    if not parts:
        return False
    if any(
        part.startswith(".") or part.casefold() in policy.excluded_folders for part in parts[:-1]
    ):
        return False
    return parts[-1].casefold() not in policy.excluded_files


def _require_exact_keys(
    payload: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise OperationLogError(
            f"{label} fields differ from schema; missing={missing}, unexpected={unexpected}"
        )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationLogError(f"{key} must be a non-empty string")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OperationLogError(f"{key} must be a non-empty string or null")
    return value


def _required_actor(payload: Mapping[str, object]) -> str:
    actor = _required_string(payload, "actor")
    if actor != actor.strip() or len(actor) > _MAX_ACTOR_LENGTH:
        raise OperationLogError(f"actor must be trimmed and at most {_MAX_ACTOR_LENGTH} characters")
    return actor


def _optional_string_tuple(
    payload: Mapping[str, object],
    key: str,
) -> tuple[str, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise OperationLogError(f"{key} must be a string array or null")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or any(ord(character) < 32 for character in item)
        ):
            raise OperationLogError(f"{key} contains an invalid alias")
        normalized = item.casefold()
        if normalized in seen:
            raise OperationLogError(f"{key} contains duplicate aliases")
        seen.add(normalized)
        result.append(item)
    return tuple(result)


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise OperationLogError(f"{key} must be a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise OperationLogError(f"{key} must contain non-empty strings")
        result.append(item)
    return tuple(result)


def _required_hash(payload: Mapping[str, object], key: str) -> str:
    value = _required_string(payload, key)
    _require_hash(value, key)
    return value


def _optional_hash(payload: Mapping[str, object], key: str) -> str | None:
    value = _optional_string(payload, key)
    if value is not None:
        _require_hash(value, key)
    return value


def _require_hash(value: str, label: str) -> None:
    if not _HASH_PATTERN.fullmatch(value):
        raise BatchConflictError(f"{label} must be a lowercase 64-character SHA-256")


def _recovery_required(blocked: tuple[BlockedOperation, ...]) -> RecoveryRequiredError:
    first = blocked[0]
    return RecoveryRequiredError(
        "Recovery required: organization batch contains "
        f"{len(blocked)} divergent path(s); first operation_id={first.operation_id}"
    )


def _inject(fault_injector: BatchFaultInjector | None, point: str) -> None:
    if fault_injector is not None:
        fault_injector(point)


def _ensure_directory_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise OperationLogError(f"batch directory parent is not a directory: {current}")
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        durable_flush_directory(created.parent)
