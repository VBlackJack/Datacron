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

import asyncio
import os
import subprocess
import time
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from datacron.core.config import Settings
from datacron.core.models import Note
from datacron.core.operation_log import OperationRecord
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.scope import (
    AccessMode,
    ConjunctiveVaultScope,
    NoteAdmissionError,
    NoteAdmissionPolicy,
    ScopedVaultReader,
    ScopedVaultWriter,
    SingleTenantVaultScope,
)
from datacron.core.vault import SKIPPED_FOLDERS, FilesystemVaultReader
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _admitted_note_paths

_NOTE_ID = "01J00000000000000000000091"


class _CountingReader:
    def __init__(self) -> None:
        self.list_notes_calls = 0

    def defer_identity_writes(self) -> AbstractAsyncContextManager[None]:
        return nullcontext()

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

    def defer_identity_writes(self) -> AbstractAsyncContextManager[None]:
        return self._delegate.defer_identity_writes()

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


def test_a_unc_argument_is_refused_before_any_filesystem_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving a UNC path is an outbound SMB connection, so it must never happen.

    The confinement check resolves its argument before comparing it to the
    allowed roots, which on Windows authenticates against the caller's host. The
    lexical screen has to reject the string first, so this test fails the moment
    any resolution is attempted.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    scope = _scope(vault)

    def _forbidden(path: Path) -> Path:
        message = f"path {path} was resolved before confinement refused it"
        raise AssertionError(message)

    monkeypatch.setattr("datacron.core.paths._resolve", _forbidden)

    with pytest.raises(PathConfinementError, match="UNC share"):
        scope.authorize_rel_path("//evil.example.com/share", "read")
    with pytest.raises(NoteAdmissionError):
        scope.authorize_note_rel_path("//evil.example.com/share/x.md")
    assert not scope.allows_rel_path("//evil.example.com/share", "read")
    assert not scope.allows_note_rel_path("//evil.example.com/share/x.md")


def test_an_absolute_or_traversing_argument_is_refused_lexically(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    scope = _scope(vault)

    for rel_path in ("C:/Windows/win.ini", "/etc/passwd", "../outside"):
        assert not scope.allows_rel_path(rel_path, "read")


async def test_scoped_list_operations_decides_each_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal holds many operations per note, so admission is per note.

    allows_rel_path resolves the candidate and every allowed root on each call,
    and this filter runs on the event loop after the journal read returns from
    its worker thread. At ten thousand records that was twenty thousand
    blocking path resolutions before the first record reached a caller who had
    asked about one note.
    """
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    scope = _scope(vault)

    records = [
        OperationRecord(
            operation_id=f"{index:032x}",
            timestamp="2026-09-18T00:00:00+00:00",
            op="patch",
            tool="patch_note_section",
            note_id=None,
            rel_path=f"notes/note{index % 5}.md",
            before_hash=None,
            after_hash="a" * 64,
            actor="test",
            parameters={},
            history_stored=False,
        )
        for index in range(200)
    ]

    class _Delegate:
        async def list_operations(self) -> list[OperationRecord]:
            return records

    writer = ScopedVaultWriter.__new__(ScopedVaultWriter)
    writer._delegate = cast("Any", _Delegate())
    writer._scope = scope
    resolutions = 0
    real_resolve = Path.resolve

    def counting_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        nonlocal resolutions
        resolutions += 1
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    kept = await writer.list_operations()

    assert len(kept) == 200
    # Five distinct notes, two resolutions apiece: the candidate and the root.
    assert resolutions <= 20, f"{resolutions} resolutions for 5 distinct notes"


def test_a_preresolved_root_admits_exactly_what_resolving_it_again_admitted(
    tmp_path: Path,
) -> None:
    """Resolving the vault root per call bought nothing, and must have cost nothing.

    The root is resolved when the scope is built, so resolving it again on every
    authorization was a second realpath for an answer already held. Dropping it is
    only legitimate if the decision is identical, including for the paths that must
    be refused, so the two are compared here over every shape that matters.
    """
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "live.md").write_text("# Live\n", encoding="utf-8")
    (vault / "notes" / "not-markdown.txt").write_text("x\n", encoding="utf-8")
    (vault / ".hidden").mkdir()
    (vault / ".hidden" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.md").write_text("# Elsewhere\n", encoding="utf-8")

    scope = SingleTenantVaultScope(vault, Settings(write_paths=[vault]))

    def _resolving_each_time(_self: SingleTenantVaultScope, path: Path, access: AccessMode) -> Path:
        resolved = assert_within_paths(path, [vault.resolve()], kind=access)
        if access == "write":
            return assert_within_paths(resolved, [vault.resolve()], kind="write")
        return resolved

    candidates = [
        "notes/live.md",
        "notes/missing.md",
        "notes/not-markdown.txt",
        ".hidden/secret.md",
        "notes/../notes/live.md",
        "../outside/elsewhere.md",
        "notes/",
        "",
    ]

    now = [scope.allows_note_rel_path(candidate) for candidate in candidates]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SingleTenantVaultScope, "authorize_path", _resolving_each_time)
        before = [scope.allows_note_rel_path(candidate) for candidate in candidates]

    assert now == before
    # Guard the guard: a comparison of all-False against all-False would also pass.
    assert now[0] is True
    assert now.count(False) == len(candidates) - 1


async def test_listing_notes_does_not_block_the_event_loop_while_it_admits(
    tmp_path: Path,
) -> None:
    """The admission sweep must not stop everything else the server is serving.

    It resolves and stats every indexed path, which is about 340 microseconds each,
    so on a vault of twenty thousand notes it held the loop for seven seconds and no
    other tool call could progress. A page now reuses the sweep's walk and decides
    almost nothing, but the branch that decides is still reached: by the first call
    after a restart, and for any path indexed since the last sweep. This pins that
    it stays off the loop, because moving it back would be invisible otherwise.
    """
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    rel_paths = []
    for index in range(40):
        rel_path = f"notes/note-{index:03d}.md"
        (vault / rel_path).write_text(f"# Note {index}\n", encoding="utf-8")
        rel_paths.append(rel_path)

    scope = SingleTenantVaultScope(vault, Settings(write_paths=[vault]))
    # No sweep has published its walk, so this exercises the branch that decides
    # admission itself, which is the one that must not be moved back onto the loop.
    app = SimpleNamespace(scope=scope, repair_state=SimpleNamespace(live_note_paths=None))
    ticks = 0
    running = True

    async def _heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0)

    slow_paths = list(rel_paths)
    original_allows = SingleTenantVaultScope.allows_note_rel_path

    def _slow_allows(self: SingleTenantVaultScope, rel_path: str) -> bool:
        time.sleep(0.002)
        return original_allows(self, rel_path)

    beat = asyncio.create_task(_heartbeat())
    await asyncio.sleep(0)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SingleTenantVaultScope, "allows_note_rel_path", _slow_allows)
        admitted = await asyncio.to_thread(_admitted_note_paths, cast("Any", app), slow_paths)
    running = False
    await beat

    assert admitted == slow_paths
    # The sweep slept for about eighty milliseconds. A loop that was free to run
    # during it ticks thousands of times; a blocked one cannot tick at all.
    assert ticks > 100, f"the event loop only advanced {ticks} times during the sweep"
