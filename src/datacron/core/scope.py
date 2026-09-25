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

import logging
import os
import stat
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path, PurePosixPath
from typing import (
    TYPE_CHECKING,
    Final,
    Literal,
    Protocol,
    TypeVar,
    cast,
    final,
    runtime_checkable,
)

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
from datacron.core.paths import (
    PathConfinementError,
    assert_vault_rel_path,
    assert_within_paths,
    assert_within_resolved_roots,
)
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
    "admits_note_path",
    "assert_path_chain_without_links",
    "authorize_note_write",
]

AccessMode = Literal["read", "write"]
NoteMutation = Callable[[str], str]
NotePathLookup = Callable[[str], Awaitable[str | None]]
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class _HasRelPath(Protocol):
    """Anything the read scope filters by its vault-relative path."""

    @property
    def rel_path(self) -> str: ...


_HasRelPathT = TypeVar("_HasRelPathT", bound=_HasRelPath)


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

    def admits_walked_note(self, rel_path: str, path: Path) -> bool:
        """Return whether one ``(rel_path, path)`` pair from a vault walk is a note."""
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
        self._resolved_roots: tuple[Path, ...] = (self._vault_root,)

    @property
    def admission_policy(self) -> NoteAdmissionPolicy:
        """Return the immutable note policy enforced by this scope."""
        return self._admission_policy

    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        # The vault root was resolved when this scope was built, so re-resolving it
        # on every call bought nothing and cost a realpath per authorized path.
        resolved = assert_within_resolved_roots(path, self._resolved_roots, kind=access)
        if access == "write":
            return assert_within_paths(resolved, self._settings.write_paths, kind="write")
        return resolved

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        """Resolve and authorize a vault-relative path.

        The caller named a vault-relative path, and the refusal names the same
        one. The confinement check phrases its refusal with the resolved absolute
        path and every allowed root, which suits a local operator and not an MCP
        client: it handed the host's user name and directory layout to whoever
        asked. The resolved form is logged here, locally, and only the caller's
        spelling travels on.
        """
        assert_vault_rel_path(rel_path)
        try:
            return self.authorize_path(self._vault_root / rel_path, access)
        except PathConfinementError as exc:
            _LOGGER.debug("Refused %s access to %r: %s", access, rel_path, exc)
            raise type(exc)(f"Path {rel_path!r} is outside the allowed {access} scope.") from exc

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        try:
            self.authorize_rel_path(rel_path, access)
        except PathConfinementError:
            return False
        return True

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        """Return a confined, admitted, existing Markdown note path."""
        try:
            resolved = self._authorize_note_location(rel_path, "read")
        except PathConfinementError as exc:
            raise NoteAdmissionError(f"Path escapes the admitted vault: {rel_path!r}") from exc
        if not resolved.is_file():
            raise NoteAdmissionError(f"Path is not a live note: {rel_path!r}")
        return resolved

    def admits_note_path(self, rel_path: str) -> bool:
        """Return whether ``rel_path`` names a place note admission covers.

        Unlike :meth:`allows_note_rel_path` the note need not exist. A journal
        record outlives the note it describes, through a move or a deletion, and
        its path and heading must still be withheld when the policy excludes that
        place, and served when it does not.
        """
        try:
            self._authorize_note_location(rel_path, "read")
        except (NoteAdmissionError, PathConfinementError):
            return False
        return True

    def authorize_note_write_rel_path(self, rel_path: str) -> Path:
        """Return the confined, admitted path of a note about to be written.

        Writes used to pass confinement alone, so a note under an excluded folder
        or with an excluded name was created, appended to and patched although no
        read would ever serve it, and a patch naming a wrong heading answered with
        the headings of that note. The note may not exist yet, which is the one
        difference from :meth:`authorize_note_rel_path`.

        A write also refuses any link or reparse point on the way to the note.
        Resolution would otherwise follow a junction inside the vault to a folder
        the lexical spelling does not name.

        Every refusal depends on the spelling and the policy, never on whether the
        note exists, so the refusal cannot be used to probe excluded notes.

        Raises:
            NoteAdmissionError: If the path, or the place it resolves to, is
                outside note admission.
            PathConfinementError: If the path escapes the vault, is outside the
                write scope, or crosses a link. A link is reported with this base
                type, the one write tools have always answered an escape with.
        """
        # Traversal, drives and absolute paths are confinement refusals first, as
        # they always were; admission then judges a path that stays in the vault.
        assert_vault_rel_path(rel_path)
        lexical_parts = PurePosixPath(rel_path.replace("\\", "/")).parts
        self._assert_admitted_parts(lexical_parts, rel_path=rel_path)
        try:
            assert_path_chain_without_links(
                self._vault_root / rel_path,
                anchor=self._vault_root,
                allow_missing=True,
            )
        except LinkedPathError as exc:
            _LOGGER.debug("Refused a write through a linked path %r: %s", rel_path, exc)
            raise PathConfinementError(
                f"Path {rel_path!r} crosses a link or reparse point; writes do not follow links."
            ) from exc
        return self._authorize_note_location(rel_path, "write")

    def _authorize_note_location(self, rel_path: str, access: AccessMode) -> Path:
        """Admit both spellings of a note path, without requiring the file."""
        lexical_parts = PurePosixPath(rel_path.replace("\\", "/")).parts
        self._assert_admitted_parts(lexical_parts, rel_path=rel_path)
        resolved = self.authorize_rel_path(rel_path, access)
        canonical_rel_path = resolved.relative_to(self._vault_root)
        self._assert_admitted_parts(canonical_rel_path.parts, rel_path=rel_path)
        return resolved

    def allows_note_rel_path(self, rel_path: str) -> bool:
        """Return whether ``rel_path`` passes note admission."""
        try:
            self.authorize_note_rel_path(rel_path)
        except (NoteAdmissionError, PathConfinementError):
            return False
        return True

    def admits_walked_note(self, rel_path: str, path: Path) -> bool:
        """Return whether one ``(rel_path, path)`` pair from a vault walk is a note.

        The caller holds two descriptions of one file and both must be admitted,
        which is why this asks for both rather than for whichever is handier. The
        answer is the one :meth:`authorize_path` and :meth:`authorize_note_rel_path`
        give together, and it used to be computed exactly that way: two realpaths
        per note, on a sweep that visits every note in the vault.

        The second one is provably redundant whenever the pair agrees. Resolving
        confines ``path`` to a realpath, and a realpath resolves to itself, so once
        ``rel_path`` is the resolved path's own vault-relative spelling, resolving
        ``vault_root / rel_path`` can only return the path already in hand. The two
        admission checks then read the same components, and the liveness check the
        same file. The pair a walk produces always agrees, because the walk derives
        one from the other by the same expression this compares against.

        Agreeing means the identical string, not an equivalent one. Normalising the
        two sides towards each other would make the proof platform-dependent, and
        it did: a backslash is a separator on Windows and an ordinary filename
        character everywhere else, so ``notes\\note.md`` names one file here and a
        different one on Linux, where this admitted a pair the two-resolution form
        refused.

        A pair that disagrees is not assumed to be anything. It goes through the
        general comparison, which resolves the ``rel_path`` side and requires the
        two to meet: a caller handing in a path and an unrelated spelling of it,
        or a delegate answering from outside the vault, is refused exactly as
        before. Only the resolved side is reused there, so nothing resolves twice.
        """
        try:
            resolved = self.authorize_path(path, "read")
        except PathConfinementError:
            return False
        # authorize_path proved the resolved path is under this scope's only root,
        # which is the vault root, so the relative spelling always exists.
        canonical = str(PurePosixPath(*resolved.relative_to(self._vault_root).parts))
        if canonical != rel_path:
            return self._admits_resolved_pair(rel_path, resolved)
        try:
            assert_vault_rel_path(rel_path)
            self._assert_admitted_parts(
                PurePosixPath(canonical).parts,
                rel_path=rel_path,
            )
        except (NoteAdmissionError, PathConfinementError):
            return False
        return resolved.is_file()

    def _admits_resolved_pair(self, rel_path: str, resolved: Path) -> bool:
        """Admit a pair whose two sides have to be resolved separately to compare."""
        try:
            return resolved == self.authorize_note_rel_path(rel_path)
        except (NoteAdmissionError, PathConfinementError):
            return False

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

    def admits_note_path(self, rel_path: str) -> bool:
        """Canonical note admission, live or not, narrowed by the restriction."""
        return self._canonical.admits_note_path(rel_path) and self._restriction.allows_rel_path(
            rel_path, "read"
        )

    def authorize_note_write_rel_path(self, rel_path: str) -> Path:
        """Canonical note write admission, narrowed by the restriction's write scope."""
        canonical = self._canonical.authorize_note_write_rel_path(rel_path)
        restricted = self._restriction.authorize_rel_path(rel_path, "write")
        self._assert_same_path(canonical, restricted)
        return canonical

    def admits_walked_note(self, rel_path: str, path: Path) -> bool:
        if not (
            self._canonical.admits_walked_note(rel_path, path)
            and self._restriction.admits_walked_note(rel_path, path)
        ):
            return False
        # The call site this serves compared the two scopes' resolutions, not only
        # their verdicts, because it went through authorize_path. An injected scope
        # that admits a pair while resolving it somewhere else is exactly what that
        # comparison is for, so it is kept rather than traded for the plain AND
        # that allows_note_rel_path settles for.
        try:
            self._assert_same_path(
                self._canonical.authorize_path(path, "read"),
                self._restriction.authorize_path(path, "read"),
            )
        except PathConfinementError:
            return False
        return True

    @staticmethod
    def _assert_same_path(canonical: Path, restricted: Path) -> None:
        if canonical != restricted:
            raise PathConfinementError(
                "Injected scope resolved a path outside the canonical vault scope."
            )


def authorize_note_write(scope: VaultScope, rel_path: str) -> Path:
    """Authorize a note write through ``scope``, note admission included.

    The two scopes this module builds admit a note that does not exist yet. Any
    other scope is known only through the protocol, whose note admission requires
    a live note, so it is asked for both confinement and admission, and a creation
    through it fails closed instead of skipping the policy.
    """
    if isinstance(scope, (SingleTenantVaultScope, ConjunctiveVaultScope)):
        return scope.authorize_note_write_rel_path(rel_path)
    resolved = scope.authorize_rel_path(rel_path, "write")
    scope.authorize_note_rel_path(rel_path)
    return resolved


def admits_note_path(scope: VaultScope, rel_path: str) -> bool:
    """Return whether ``scope`` admits ``rel_path`` as a note location, live or not.

    A scope known only through the protocol answers for live notes alone, so a
    record whose note is gone is withheld from it rather than guessed about.
    """
    if isinstance(scope, (SingleTenantVaultScope, ConjunctiveVaultScope)):
        return scope.admits_note_path(rel_path)
    return scope.allows_note_rel_path(rel_path)


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

    def defer_identity_writes(self) -> AbstractAsyncContextManager[None]:
        """Delegate the identity write scope to the reader this one wraps."""
        return self._delegate.defer_identity_writes()

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

    async def note_paths(self) -> dict[str, Path]:
        self._scope.authorize_rel_path("", "read")
        notes = await self._delegate.note_paths()
        return {
            rel_path: path
            for rel_path, path in notes.items()
            if self._matches_stat_admission(rel_path, path)
        }

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
        return self._scope.admits_walked_note(rel_path, path)


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

    def _readable_by_rel_path(self, items: Sequence[_HasRelPathT]) -> list[_HasRelPathT]:
        """Keep the items note admission admits, deciding each path once.

        Confinement alone let the record of a write to an excluded note return
        its path, its heading and whether a restore point exists, although every
        read of that note is refused. Records are filtered by note admission, and
        without requiring the note to exist still, since a record outlives a move.

        Admission resolves the candidate and every allowed root on
        each call, which on Windows is a file-open syscall apiece. These
        sequences come from the operation journal, which holds one record per
        committed write and is never compacted, so the same ``rel_path``
        repeats across every operation on a note: deciding it once turns a
        per-record cost into a per-note one. After ten thousand writes the
        difference is twenty thousand syscalls before the first record reaches
        a caller that asked for one note.

        ``search.py`` caches the same decision the same way for wikilink chunks.
        """
        admitted: dict[str, bool] = {}
        kept: list[_HasRelPathT] = []
        for item in items:
            decision = admitted.get(item.rel_path)
            if decision is None:
                decision = admits_note_path(self._scope, item.rel_path)
                admitted[item.rel_path] = decision
            if decision:
                kept.append(item)
        return kept

    @property
    def recovery_blocked(self) -> tuple[BlockedOperation, ...]:
        """Return only blocked operations visible in this read scope."""
        return tuple(self._readable_by_rel_path(self._delegate.recovery_blocked))

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
        authorize_note_write(self._scope, rel_path)
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
        authorize_note_write(self._scope, rel_path)
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
        authorize_note_write(self._scope, rel_path)
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
        return tuple(self._readable_by_rel_path(blocked))

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
        return self._readable_by_rel_path(records)

    async def present_history_hashes(self, hashes: Iterable[str | None]) -> set[str]:
        # Presence of a history blob is keyed by content hash, not by path, and the
        # caller only learns about a hash from a record this scope already returned.
        return await self._delegate.present_history_hashes(hashes)

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
