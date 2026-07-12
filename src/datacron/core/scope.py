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

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol, final, runtime_checkable

from datacron.core.config import Settings
from datacron.core.durability import WritePolicy
from datacron.core.models import Note
from datacron.core.operation_log import OperationContext, OperationRecord
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.protocols import VaultReader, VaultWriter

__all__ = [
    "AccessMode",
    "ScopedVaultReader",
    "ScopedVaultWriter",
    "SingleTenantVaultScope",
    "VaultScope",
]

AccessMode = Literal["read", "write"]
NoteMutation = Callable[[str], str]
NotePathLookup = Callable[[str], Awaitable[str | None]]


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


@final
class SingleTenantVaultScope:
    """Allow one complete local vault, with writes restricted by configuration."""

    def __init__(self, vault_root: Path, settings: Settings) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._settings = settings

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


@final
class ScopedVaultReader:
    """Mediate every ``VaultReader`` filesystem operation through one scope."""

    def __init__(
        self,
        delegate: VaultReader,
        scope: VaultScope,
        note_path_lookup: NotePathLookup | None = None,
    ) -> None:
        self._delegate = delegate
        self._scope = scope
        self._note_path_lookup = note_path_lookup

    def bind_note_path_lookup(self, lookup: NotePathLookup) -> None:
        """Bind the existing index lookup used to authorize resolved note IDs."""
        self._note_path_lookup = lookup

    async def read_note(self, path: Path) -> Note:
        resolved = self._scope.authorize_path(path, "read")
        note = await self._delegate.read_note(resolved)
        self._scope.authorize_path(note.path, "read")
        return note

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        self._scope.authorize_rel_path(folder or "", "read")
        notes = await self._delegate.list_notes(folder=folder)
        allowed = [note for note in notes if self._scope.allows_rel_path(note.rel_path, "read")]
        return allowed if limit is None else allowed[:limit]

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        self._scope.authorize_rel_path("", "read")
        notes = await self._delegate.stat_notes()
        return {
            rel_path: value
            for rel_path, value in notes.items()
            if self._scope.allows_rel_path(rel_path, "read")
        }

    async def resolve_alias(self, alias: str) -> str | None:
        resolved_id = await self._delegate.resolve_alias(alias)
        if resolved_id is None:
            return None
        if self._note_path_lookup is not None:
            rel_path = await self._note_path_lookup(resolved_id)
            if rel_path is not None:
                return resolved_id if self._scope.allows_rel_path(rel_path, "read") else None
        notes = await self.list_notes()
        return resolved_id if any(note.id == resolved_id for note in notes) else None

    async def invalidate_alias_cache(self) -> None:
        await self._delegate.invalidate_alias_cache()


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
        self._scope.authorize_rel_path(rel_path, "write")
        return await self._delegate.revert_note_atomic(
            rel_path,
            to_hash,
            expected_hash=expected_hash,
            operation=operation,
        )

    async def recover_operations(self) -> int:
        self._write_policy.ensure_writable()
        return await self._delegate.recover_operations()

    async def list_operations(self) -> list[OperationRecord]:
        records = await self._delegate.list_operations()
        return [
            record for record in records if self._scope.allows_rel_path(record.rel_path, "read")
        ]

    async def purge_history(self) -> list[str]:
        self._write_policy.ensure_writable()
        return await self._delegate.purge_history()
