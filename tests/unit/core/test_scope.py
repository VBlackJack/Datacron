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
"""Tests for scoped alias authorization without vault enumeration."""

from __future__ import annotations

from pathlib import Path

from datacron.core.models import Note
from datacron.core.scope import AccessMode, ScopedVaultReader

_NOTE_ID = "01J00000000000000000000091"


class _CountingReader:
    def __init__(self) -> None:
        self.list_notes_calls = 0

    async def read_note(self, path: Path) -> Note:
        raise AssertionError(f"unexpected read_note call for {path}")

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        self.list_notes_calls += 1
        return []

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        return {}

    async def resolve_alias(self, alias: str) -> str | None:
        return _NOTE_ID if alias == "target" else None

    async def invalidate_alias_cache(self) -> None:
        return None


class _AllowedFolderScope:
    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        return path

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        return Path(rel_path)

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        return rel_path.startswith("allowed/")


async def test_resolve_alias_uses_index_path_without_listing_notes() -> None:
    delegate = _CountingReader()

    async def indexed_path(note_id: str) -> str | None:
        return "allowed/target.md" if note_id == _NOTE_ID else None

    reader = ScopedVaultReader(delegate, _AllowedFolderScope(), indexed_path)

    assert await reader.resolve_alias("target") == _NOTE_ID
    assert delegate.list_notes_calls == 0


async def test_resolve_alias_rejects_indexed_note_outside_scope_without_listing_notes() -> None:
    delegate = _CountingReader()

    async def indexed_path(note_id: str) -> str | None:
        return "private/target.md" if note_id == _NOTE_ID else None

    reader = ScopedVaultReader(delegate, _AllowedFolderScope(), indexed_path)

    assert await reader.resolve_alias("target") is None
    assert delegate.list_notes_calls == 0
