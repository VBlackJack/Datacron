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
"""Single insertion point for vault read and write scope policy."""

from __future__ import annotations

import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast, final, runtime_checkable

from datacron.core.config import (
    SIDECAR_DIR_NAME,
    VAULT_CONFIG_FILENAME,
    Settings,
    VaultConfig,
    load_vault_config,
)
from datacron.core.durability import RecoveryRequiredError, WritePolicy
from datacron.core.models import Note
from datacron.core.operation_log import OperationContext, OperationRecord
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.protocols import VaultReader, VaultWriter
from datacron.core.recovery import (
    BlockedOperation,
    RecoveryRepairAction,
    RecoveryRepairResult,
)
from datacron.core.vault import SKIPPED_FOLDERS, NoteAdmissionPolicy

if TYPE_CHECKING:
    from datacron.core.batch_transaction import (
        BatchApplyResult,
        BatchFaultInjector,
        BatchPrecommitValidator,
    )
    from datacron.organization.manifest import ValidatedOrganizationBundle

__all__ = [
    "AccessMode",
    "ConjunctiveVaultScope",
    "LinkedPathError",
    "NoteAdmissionError",
    "NoteAdmissionPolicy",
    "OrganizationBatchWriter",
    "ScopedVaultReader",
    "ScopedVaultWriter",
    "SingleTenantVaultScope",
    "VaultScope",
    "assert_path_chain_without_links",
]

AccessMode = Literal["read", "write"]
NoteMutation = Callable[[str], str]
NotePathLookup = Callable[[str], Awaitable[str | None]]
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400


class LinkedPathError(PathConfinementError):
    """Raised when a filesystem path crosses a link or reparse point."""


def assert_path_chain_without_links(
    path: Path,
    *,
    anchor: Path | None = None,
    allow_missing: bool = False,
) -> Path:
    """Return an absolute path after rejecting linked path components.

    This guard inspects the lexical path before calling ``Path.resolve``. That
    order is intentional: resolving first would erase the evidence that a
    symlink, junction, or other Windows reparse point was traversed.

    Args:
        path: Candidate path. Relative paths are rejected.
        anchor: Optional lexical ancestor that must contain ``path``.
        allow_missing: Permit the first missing component and its descendants.

    Returns:
        The absolute lexical path. No link has been followed.

    Raises:
        FileNotFoundError: If a component is missing and ``allow_missing`` is
            false.
        LinkedPathError: If the path is relative, escapes ``anchor``, or any
            existing component is a symlink or reparse point.
        OSError: If a component cannot be inspected safely.
    """
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise LinkedPathError(f"Path must be absolute: {path!s}")
    absolute = Path(os.path.abspath(os.fspath(expanded)))

    if anchor is not None:
        expanded_anchor = anchor.expanduser()
        if not expanded_anchor.is_absolute():
            raise LinkedPathError(f"Path anchor must be absolute: {anchor!s}")
        absolute_anchor = Path(os.path.abspath(os.fspath(expanded_anchor)))
        try:
            absolute.relative_to(absolute_anchor)
        except ValueError as exc:
            raise LinkedPathError(
                f"Path {absolute} escapes lexical anchor {absolute_anchor}."
            ) from exc

    chain = (*reversed(absolute.parents), absolute)
    for component in chain:
        try:
            component_stat = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                break
            raise
        attributes = getattr(component_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(component_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise LinkedPathError(f"Linked path component is forbidden: {component}")
    return absolute


class NoteAdmissionError(Exception):
    """Raised when a path is not an admissible live Markdown note."""

    code: Final[str] = "note_not_admitted"


@runtime_checkable
class VaultScope(Protocol):
    """Authorize vault paths without coupling callers to an ACL implementation."""

    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        """Return a confined absolute path or raise ``PathConfinementError``."""
        ...

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        """Resolve and authorize a vault-relative path."""
        ...

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        """Return whether the relative path belongs to this scope."""
        ...

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        """Resolve an admissible live Markdown note or raise."""
        ...

    def allows_note_rel_path(self, rel_path: str) -> bool:
        """Return whether the relative path identifies an admissible live note."""
        ...


class OrganizationBatchWriter(VaultWriter, Protocol):
    """Narrow extension implemented only by organization-capable vault writers."""

    async def get_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Return a durable organization receipt for idempotent replay."""
        ...

    async def resolve_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Recover pending work and return one receipt under the global lock."""
        ...

    async def get_organization_removed_identity_ids(
        self,
        result: BatchApplyResult,
    ) -> tuple[str, ...]:
        """Return identity IDs removed by a committed sidecar transition."""
        ...

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
        """Apply one validated exact-byte organization batch under global lock."""
        ...

    async def validate_organization_manifest_capacity(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> None:
        """Prove the exact durable receipt fits before returning a confirmation token."""
        ...

    async def has_pending_organization_batches(self) -> bool:
        """Return whether journaled organization recovery is pending."""
        ...


@final
class SingleTenantVaultScope:
    """Allow one complete local vault, with writes restricted by configuration."""

    def __init__(
        self,
        vault_root: Path,
        settings: Settings,
        admission_policy: NoteAdmissionPolicy | None = None,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._settings = settings
        if admission_policy is None:
            config_path = self._vault_root / SIDECAR_DIR_NAME / VAULT_CONFIG_FILENAME
            config = load_vault_config(config_path) or VaultConfig()
            admission_policy = NoteAdmissionPolicy(
                excluded_folders=SKIPPED_FOLDERS | frozenset(config.excluded_folders),
                excluded_files=frozenset(config.excluded_files),
            )
        self._admission_policy = admission_policy

    @property
    def admission_policy(self) -> NoteAdmissionPolicy:
        """Return the immutable note policy enforced by this scope."""
        return self._admission_policy

    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        resolved = assert_within_paths(path, [self._vault_root], kind=access)
        if access == "write":
            return assert_within_paths(resolved, self._settings.write_paths, kind="write")
        return resolved

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        return self.authorize_path(self._vault_root / rel_path, access)

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        try:
            self.authorize_rel_path(rel_path, access)
        except PathConfinementError:
            return False
        return True

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        """Return a confined, admitted, existing Markdown note path."""
        lexical_parts = PurePosixPath(rel_path.replace("\\", "/")).parts
        self._assert_admitted_parts(lexical_parts, rel_path=rel_path)
        try:
            resolved = self.authorize_rel_path(rel_path, "read")
        except PathConfinementError as exc:
            raise NoteAdmissionError(f"Path escapes the admitted vault: {rel_path!r}") from exc
        canonical_rel_path = resolved.relative_to(self._vault_root)
        self._assert_admitted_parts(canonical_rel_path.parts, rel_path=rel_path)
        if not resolved.is_file():
            raise NoteAdmissionError(f"Path is not a live note: {rel_path!r}")
        return resolved

    def allows_note_rel_path(self, rel_path: str) -> bool:
        """Return whether ``rel_path`` passes note admission."""
        try:
            self.authorize_note_rel_path(rel_path)
        except (NoteAdmissionError, PathConfinementError):
            return False
        return True

    def _assert_admitted_parts(self, parts: tuple[str, ...], *, rel_path: str) -> None:
        if not parts or not parts[-1].casefold().endswith(".md"):
            raise NoteAdmissionError(f"Path is not a Markdown note: {rel_path!r}")
        parent_parts = parts[:-1]
        if any(
            part.startswith(".") or part.casefold() in self._admission_policy.excluded_folders
            for part in parent_parts
        ):
            raise NoteAdmissionError(f"Path has an excluded parent: {rel_path!r}")
        if parts[-1].casefold() in self._admission_policy.excluded_files:
            raise NoteAdmissionError(f"Path names an excluded file: {rel_path!r}")


@final
class ConjunctiveVaultScope:
    """Enforce canonical vault admission plus an injected restriction.

    Dependency-injected scopes may narrow the served vault, but they cannot
    replace the canonical admission boundary assembled from ``VAULT.yaml``.
    """

    def __init__(
        self,
        canonical: SingleTenantVaultScope,
        restriction: VaultScope,
    ) -> None:
        self._canonical = canonical
        self._restriction = restriction

    @property
    def admission_policy(self) -> NoteAdmissionPolicy:
        """Return the canonical policy shared with the production reader."""
        return self._canonical.admission_policy

    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        canonical = self._canonical.authorize_path(path, access)
        restricted = self._restriction.authorize_path(path, access)
        self._assert_same_path(canonical, restricted)
        return canonical

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        canonical = self._canonical.authorize_rel_path(rel_path, access)
        restricted = self._restriction.authorize_rel_path(rel_path, access)
        self._assert_same_path(canonical, restricted)
        return canonical

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        return self._canonical.allows_rel_path(
            rel_path,
            access,
        ) and self._restriction.allows_rel_path(rel_path, access)

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        canonical = self._canonical.authorize_note_rel_path(rel_path)
        restricted = self._restriction.authorize_note_rel_path(rel_path)
        self._assert_same_path(canonical, restricted)
        return canonical

    def allows_note_rel_path(self, rel_path: str) -> bool:
        return self._canonical.allows_note_rel_path(
            rel_path
        ) and self._restriction.allows_note_rel_path(rel_path)

    @staticmethod
    def _assert_same_path(canonical: Path, restricted: Path) -> None:
        if canonical != restricted:
            raise PathConfinementError(
                "Injected scope resolved a path outside the canonical vault scope."
            )


@final
class ScopedVaultReader:
    """Mediate every ``VaultReader`` filesystem operation through one scope."""

    def __init__(
        self,
        delegate: VaultReader,
        scope: VaultScope,
        note_path_lookup: NotePathLookup | None = None,
        admission_policy: NoteAdmissionPolicy | None = None,
    ) -> None:
        self._delegate = delegate
        self._scope = scope
        self._note_path_lookup = note_path_lookup
        self._admission_policy = admission_policy

    @property
    def admission_policy(self) -> NoteAdmissionPolicy | None:
        """Return the shared production admission policy, when configured."""
        return self._admission_policy

    def bind_note_path_lookup(self, lookup: NotePathLookup) -> None:
        """Bind the existing index lookup used to authorize resolved note IDs."""
        self._note_path_lookup = lookup

    async def read_note(self, path: Path) -> Note:
        resolved = self._scope.authorize_path(path, "read")
        note = await self._delegate.read_note(resolved)
        if not self._matches_note_admission(note, expected_path=resolved):
            raise NoteAdmissionError(
                f"Reader returned a path outside note admission: {note.rel_path!r}"
            )
        return note

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        self._scope.authorize_rel_path(folder or "", "read")
        notes = await self._delegate.list_notes(folder=folder)
        allowed = [note for note in notes if self._matches_note_admission(note)]
        return allowed if limit is None else allowed[:limit]

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        self._scope.authorize_rel_path("", "read")
        notes = await self._delegate.stat_notes()
        return {
            rel_path: value
            for rel_path, value in notes.items()
            if self._matches_stat_admission(rel_path, value[0])
        }

    async def resolve_alias(self, alias: str) -> str | None:
        resolved_id = await self._delegate.resolve_alias(alias)
        if resolved_id is None:
            return None
        if self._note_path_lookup is not None:
            rel_path = await self._note_path_lookup(resolved_id)
            if rel_path is not None:
                return resolved_id if self._scope.allows_note_rel_path(rel_path) else None
        notes = await self.list_notes()
        return resolved_id if any(note.id == resolved_id for note in notes) else None

    async def invalidate_alias_cache(self) -> None:
        await self._delegate.invalidate_alias_cache()

    def _matches_note_admission(
        self,
        note: Note,
        *,
        expected_path: Path | None = None,
    ) -> bool:
        try:
            returned = self._scope.authorize_path(note.path, "read")
            admitted = self._scope.authorize_note_rel_path(note.rel_path)
        except (NoteAdmissionError, PathConfinementError):
            return False
        return returned == admitted and (expected_path is None or returned == expected_path)

    def _matches_stat_admission(self, rel_path: str, path: Path) -> bool:
        try:
            returned = self._scope.authorize_path(path, "read")
            admitted = self._scope.authorize_note_rel_path(rel_path)
        except (NoteAdmissionError, PathConfinementError):
            return False
        return returned == admitted


@final
class ScopedVaultWriter:
    """Mediate every note writer operation and audit read through one scope."""

    def __init__(
        self,
        delegate: VaultWriter,
        scope: VaultScope,
        write_policy: WritePolicy,
    ) -> None:
        self._delegate = delegate
        self._scope = scope
        self._write_policy = write_policy

    @property
    def recovery_blocked(self) -> tuple[BlockedOperation, ...]:
        """Return only blocked operations visible in this read scope."""
        return tuple(
            item
            for item in self._delegate.recovery_blocked
            if self._scope.allows_rel_path(item.rel_path, "read")
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
        self._write_policy.ensure_writable()
        await self._ensure_organization_recovery_scope()
        self._scope.authorize_rel_path(rel_path, "write")
        return await self._delegate.write_note_atomic(
            rel_path,
            content,
            overwrite=overwrite,
            expected_hash=expected_hash,
            note_id=note_id,
            operation=operation,
        )

    async def mutate_note_atomic(
        self,
        rel_path: str,
        mutation: NoteMutation,
        *,
        expected_hash: str | None = None,
        operation: OperationContext | None = None,
    ) -> str:
        self._write_policy.ensure_writable()
        await self._ensure_organization_recovery_scope()
        self._scope.authorize_rel_path(rel_path, "write")
        return await self._delegate.mutate_note_atomic(
            rel_path,
            mutation,
            expected_hash=expected_hash,
            operation=operation,
        )

    async def revert_note_atomic(
        self,
        rel_path: str,
        to_hash: str,
        *,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        self._write_policy.ensure_writable()
        await self._ensure_organization_recovery_scope()
        self._scope.authorize_rel_path(rel_path, "write")
        return await self._delegate.revert_note_atomic(
            rel_path,
            to_hash,
            expected_hash=expected_hash,
            operation=operation,
        )

    async def recover_operations(self) -> int:
        self._write_policy.ensure_writable()
        if type(self._scope) is not SingleTenantVaultScope:
            if await self.has_pending_organization_batches():
                raise RecoveryRequiredError(
                    "Recovery required: pending organization batches require the canonical "
                    "single-tenant vault scope"
                )
            return 0
        return await self._delegate.recover_operations()

    async def inspect_recovery(self) -> tuple[BlockedOperation, ...]:
        blocked = await self._delegate.inspect_recovery()
        return tuple(item for item in blocked if self._scope.allows_rel_path(item.rel_path, "read"))

    async def repair_recovery(
        self,
        operation_id: str,
        action: RecoveryRepairAction,
        *,
        expected_disk_hash: str,
        actor: str,
    ) -> RecoveryRepairResult:
        self._write_policy.ensure_writable()
        await self._ensure_organization_recovery_scope()
        blocked = await self.inspect_recovery()
        selected = next(
            (item for item in blocked if item.operation_id == operation_id),
            None,
        )
        if selected is None:
            raise FileNotFoundError(f"blocked operation not found in scope: {operation_id}")
        self._scope.authorize_rel_path(selected.rel_path, "write")
        return await self._delegate.repair_recovery(
            operation_id,
            action,
            expected_disk_hash=expected_disk_hash,
            actor=actor,
        )

    async def list_operations(self) -> list[OperationRecord]:
        records = await self._delegate.list_operations()
        return [
            record for record in records if self._scope.allows_rel_path(record.rel_path, "read")
        ]

    async def purge_history(self) -> list[str]:
        self._write_policy.ensure_writable()
        await self._ensure_organization_recovery_scope()
        return await self._delegate.purge_history()

    async def get_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Return a committed organization batch result through the delegate."""
        self._require_canonical_organization_scope()
        delegate = cast("OrganizationBatchWriter", self._delegate)
        return await delegate.get_organization_batch_result(manifest_sha256)

    async def resolve_organization_batch_result(
        self,
        manifest_sha256: str,
    ) -> BatchApplyResult | None:
        """Resolve and read a batch only through the canonical vault scope."""
        self._write_policy.ensure_writable()
        self._require_canonical_organization_scope()
        delegate = cast("OrganizationBatchWriter", self._delegate)
        return await delegate.resolve_organization_batch_result(manifest_sha256)

    async def get_organization_removed_identity_ids(
        self,
        result: BatchApplyResult,
    ) -> tuple[str, ...]:
        """Return sidecar removals through the canonical organization delegate."""
        self._require_canonical_organization_scope()
        delegate = cast("OrganizationBatchWriter", self._delegate)
        return await delegate.get_organization_removed_identity_ids(result)

    async def validate_organization_manifest_capacity(
        self,
        bundle: ValidatedOrganizationBundle,
        *,
        confirmation_token: str,
        projected_report_sha256: str,
        operation: OperationContext,
    ) -> None:
        """Validate the exact future receipt without publishing durable state."""
        self._write_policy.ensure_writable()
        self._require_canonical_organization_scope()
        delegate = cast("OrganizationBatchWriter", self._delegate)
        await delegate.validate_organization_manifest_capacity(
            bundle,
            confirmation_token=confirmation_token,
            projected_report_sha256=projected_report_sha256,
            operation=operation,
        )

    async def has_pending_organization_batches(self) -> bool:
        """Return whether the delegate has an organization recovery receipt."""
        delegate = cast("OrganizationBatchWriter", self._delegate)
        return await delegate.has_pending_organization_batches()

    async def _ensure_organization_recovery_scope(self) -> None:
        if type(self._scope) is SingleTenantVaultScope:
            return
        if await self.has_pending_organization_batches():
            raise RecoveryRequiredError(
                "Recovery required: pending organization batches require the canonical "
                "single-tenant vault scope"
            )

    def _require_canonical_organization_scope(self) -> None:
        if type(self._scope) is not SingleTenantVaultScope:
            raise RecoveryRequiredError(
                "organization batches require the canonical single-tenant vault scope"
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
        """Apply one prevalidated organization bundle through the scoped writer."""
        self._write_policy.ensure_writable()
        self._require_canonical_organization_scope()
        delegate = cast("OrganizationBatchWriter", self._delegate)
        return await delegate.apply_organization_manifest(
            bundle,
            confirmation_token=confirmation_token,
            projected_report_sha256=projected_report_sha256,
            precommit_validator=precommit_validator,
            operation=operation,
            fault_injector=fault_injector,
        )
