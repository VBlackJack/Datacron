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

import os
import subprocess
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.models import Note
from datacron.core.paths import PathConfinementError
from datacron.core.scope import (
    AccessMode,
    ConjunctiveVaultScope,
    NoteAdmissionError,
    NoteAdmissionPolicy,
    ScopedVaultReader,
    SingleTenantVaultScope,
)
from datacron.core.vault import SKIPPED_FOLDERS, FilesystemVaultReader
from datacron.mcp.server import build_app

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

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        if not self.allows_note_rel_path(rel_path):
            raise NoteAdmissionError(f"Path is not admitted: {rel_path}")
        return Path(rel_path)

    def allows_note_rel_path(self, rel_path: str) -> bool:
        return rel_path.startswith("allowed/")


class _PermissiveScope:
    def __init__(self, vault: Path) -> None:
        self._vault = vault.resolve()

    def authorize_path(self, path: Path, access: AccessMode) -> Path:
        del access
        return path.resolve()

    def authorize_rel_path(self, rel_path: str, access: AccessMode) -> Path:
        del access
        return (self._vault / rel_path).resolve()

    def allows_rel_path(self, rel_path: str, access: AccessMode) -> bool:
        del rel_path, access
        return True

    def authorize_note_rel_path(self, rel_path: str) -> Path:
        return (self._vault / rel_path).resolve()

    def allows_note_rel_path(self, rel_path: str) -> bool:
        del rel_path
        return True


class _RedirectingReader:
    def __init__(self, delegate: FilesystemVaultReader, target: Path) -> None:
        self._delegate = delegate
        self._target = target

    async def read_note(self, path: Path) -> Note:
        del path
        return await self._delegate.read_note(self._target)

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        return await self._delegate.list_notes(folder=folder, limit=limit)

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        return await self._delegate.stat_notes()

    async def resolve_alias(self, alias: str) -> str | None:
        return await self._delegate.resolve_alias(alias)

    async def invalidate_alias_cache(self) -> None:
        await self._delegate.invalidate_alias_cache()


def _create_directory_link(link: Path, target: Path) -> None:
    """Create a directory symlink, using an NTFS junction when needed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {exc}")
    command_shell = os.environ.get("COMSPEC")
    assert command_shell is not None, "COMSPEC is required to create an NTFS junction"
    process = subprocess.run(
        [command_shell, "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


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


def _settings(vault: Path) -> Settings:
    return Settings(read_paths=[vault], vault_root=vault)


def _scope(
    vault: Path,
    *,
    excluded_folders: frozenset[str] = frozenset(),
    excluded_files: frozenset[str] = frozenset(),
) -> SingleTenantVaultScope:
    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | excluded_folders,
        excluded_files=excluded_files,
    )
    return SingleTenantVaultScope(vault, _settings(vault), policy)


def test_note_admission_error_has_stable_independent_contract() -> None:
    error = NoteAdmissionError("not admitted")

    assert error.code == "note_not_admitted"
    assert not isinstance(error, ValueError | FileNotFoundError | PathConfinementError)


@pytest.mark.parametrize(
    "rel_path",
    [
        "plain.txt",
        "NODE_MODULES/pkg/README.md",
        "NODE_MODULES\\pkg\\README.md",
        ".private/note.md",
        "nested/.private/note.md",
        "archive/00_index.MD",
        "archive\\00_index.MD",
    ],
)
def test_note_admission_rejects_lexical_exclusions_case_insensitively(
    tmp_path: Path,
    rel_path: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    scope = _scope(
        vault,
        excluded_folders=frozenset({"Archive"}),
        excluded_files=frozenset({"00_INDEX.md"}),
    )

    with pytest.raises(NoteAdmissionError):
        scope.authorize_note_rel_path(rel_path)
    assert scope.allows_note_rel_path(rel_path) is False


def test_note_admission_requires_a_live_file_and_allows_hidden_markdown_file(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    hidden = vault / ".hidden.md"
    hidden.write_text("# Hidden\n", encoding="utf-8")
    scope = _scope(vault)

    assert scope.authorize_note_rel_path(".hidden.md") == hidden.resolve()
    assert scope.allows_note_rel_path(".hidden.md") is True
    with pytest.raises(NoteAdmissionError):
        scope.authorize_note_rel_path("missing.md")
    assert scope.allows_note_rel_path("missing.md") is False


def test_note_admission_rechecks_canonical_segments_after_symlink(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    archive = vault / "_archive"
    notes = vault / "notes"
    archive.mkdir(parents=True)
    notes.mkdir()
    secret = archive / "secret.md"
    secret.write_text("# Secret\n", encoding="utf-8")
    link = notes / "alias"
    _create_directory_link(link, archive)
    scope = _scope(vault, excluded_folders=frozenset({"_archive"}))

    with pytest.raises(NoteAdmissionError):
        scope.authorize_note_rel_path("notes/alias/secret.md")
    assert scope.allows_note_rel_path("notes/alias/secret.md") is False


def test_note_admission_rejects_symlink_outside_vault(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    outside = tmp_path / "outside" / "outside.md"
    outside.parent.mkdir()
    outside.write_text("# Outside\n", encoding="utf-8")
    link = notes / "outside"
    _create_directory_link(link, outside.parent)
    scope = _scope(vault)

    with pytest.raises(NoteAdmissionError) as captured:
        scope.authorize_note_rel_path("notes/outside/outside.md")
    assert captured.value.code == "note_not_admitted"
    assert scope.allows_note_rel_path("notes/outside/outside.md") is False


def test_default_scope_loads_effective_vault_exclusions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    sidecar = vault / ".datacron"
    sidecar.mkdir(parents=True)
    (sidecar / "VAULT.yaml").write_text(
        "excluded_folders:\n  - zzz_Corbeille\nexcluded_files:\n  - 00_INDEX.md\n",
        encoding="utf-8",
    )

    scope = SingleTenantVaultScope(vault, _settings(vault))

    assert scope.admission_policy.excluded_folders == frozenset(
        {*(name.casefold() for name in SKIPPED_FOLDERS), "zzz_corbeille"}
    )
    assert scope.admission_policy.excluded_files == frozenset({"00_index.md"})


async def test_build_app_shares_one_effective_policy_with_scope_and_reader(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    sidecar = vault / ".datacron"
    sidecar.mkdir(parents=True)
    (sidecar / "VAULT.yaml").write_text(
        "excluded_folders:\n  - CustomArchive\nexcluded_files:\n  - 00_INDEX.md\n",
        encoding="utf-8",
    )

    app = build_app(settings=_settings(vault), vault_root=vault)

    assert isinstance(app.scope, SingleTenantVaultScope)
    assert isinstance(app.vault_reader, ScopedVaultReader)
    assert app.scope.admission_policy is app.vault_reader.admission_policy
    assert app.scope.admission_policy.excluded_folders == frozenset(
        {*(name.casefold() for name in SKIPPED_FOLDERS), "customarchive"}
    )
    assert app.scope.admission_policy.excluded_files == frozenset({"00_index.md"})


async def test_injected_scope_can_only_narrow_canonical_admission(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    excluded = vault / "node_modules" / "package" / "README.md"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("# Excluded\n", encoding="utf-8")

    app = build_app(
        settings=_settings(vault),
        vault_root=vault,
        scope=_PermissiveScope(vault),
    )

    assert isinstance(app.scope, ConjunctiveVaultScope)
    assert isinstance(app.vault_reader, ScopedVaultReader)
    assert app.scope.admission_policy is app.vault_reader.admission_policy
    assert app.scope.allows_note_rel_path("node_modules/package/README.md") is False
    with pytest.raises(NoteAdmissionError):
        app.scope.authorize_note_rel_path("node_modules/package/README.md")


async def test_scoped_reader_revalidates_injected_reader_result_as_a_note(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    allowed = vault / "allowed.md"
    allowed.write_text(f"---\nid: {_NOTE_ID}\n---\n# Allowed\n", encoding="utf-8")
    excluded = vault / "_archive" / "secret.md"
    excluded.parent.mkdir()
    excluded.write_text(f"---\nid: {_NOTE_ID}\n---\n# Secret\n", encoding="utf-8")
    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | frozenset({"_archive"}),
        excluded_files=frozenset(),
    )
    scope = SingleTenantVaultScope(vault, _settings(vault), policy)
    delegate = FilesystemVaultReader(vault, read_only=True, admission_policy=policy)
    reader = ScopedVaultReader(
        _RedirectingReader(delegate, excluded),
        scope,
        admission_policy=policy,
    )

    with pytest.raises(NoteAdmissionError):
        await reader.read_note(allowed)


async def test_scoped_reader_rejects_redirect_between_two_admitted_notes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    requested = vault / "requested.md"
    requested.write_text(f"---\nid: {_NOTE_ID}\n---\n# Requested\n", encoding="utf-8")
    redirected = vault / "redirected.md"
    redirected.write_text(
        "---\nid: 01J00000000000000000000092\n---\n# Redirected\n",
        encoding="utf-8",
    )
    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS,
        excluded_files=frozenset(),
    )
    scope = SingleTenantVaultScope(vault, _settings(vault), policy)
    delegate = FilesystemVaultReader(vault, read_only=True, admission_policy=policy)
    reader = ScopedVaultReader(
        _RedirectingReader(delegate, redirected),
        scope,
        admission_policy=policy,
    )

    with pytest.raises(NoteAdmissionError):
        await reader.read_note(requested)


async def test_scoped_reader_rejects_foreign_reader_list_and_stat(tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    (served / "same.md").write_text(
        f"---\nid: {_NOTE_ID}\n---\n# Served\n",
        encoding="utf-8",
    )
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "same.md").write_text(
        "---\nid: 01J00000000000000000000092\n---\n# Foreign\n",
        encoding="utf-8",
    )
    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS,
        excluded_files=frozenset(),
    )
    scope = SingleTenantVaultScope(served, _settings(served), policy)
    foreign_reader = FilesystemVaultReader(
        foreign,
        read_only=True,
        admission_policy=policy,
    )
    reader = ScopedVaultReader(foreign_reader, scope, admission_policy=policy)

    assert await reader.list_notes() == []
    assert await reader.stat_notes() == {}


async def test_filesystem_reader_uses_shared_case_insensitive_policy(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    excluded = vault / "CUSTOMARCHIVE"
    excluded.mkdir(parents=True)
    (excluded / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (vault / "00_index.MD").write_text("# Index\n", encoding="utf-8")
    visible = vault / "visible.md"
    visible.write_text("# Visible\n", encoding="utf-8")
    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | frozenset({"CustomArchive"}),
        excluded_files=frozenset({"00_INDEX.md"}),
    )
    reader = FilesystemVaultReader(vault, read_only=True, admission_policy=policy)

    notes = await reader.list_notes()

    assert reader.admission_policy is policy
    assert [note.rel_path for note in notes] == ["visible.md"]
