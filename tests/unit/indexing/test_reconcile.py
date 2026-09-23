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
"""Tests for the shared incremental index reconcile."""

from __future__ import annotations

import gc
import json
import os
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import aiosqlite
import pytest

from datacron.core.models import Note
from datacron.core.vault import FilesystemVaultReader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteFTS5Store]:
    s = SQLiteFTS5Store()
    await s.open(tmp_path / "index" / "datacron.db")
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def reader(tmp_vault: Path) -> FilesystemVaultReader:
    return FilesystemVaultReader(tmp_vault)


@pytest.fixture
def chunker() -> MarkdownChunker:
    return MarkdownChunker()


def _spy_read_note(
    monkeypatch: pytest.MonkeyPatch, reader: FilesystemVaultReader
) -> Callable[[], int]:
    """Wrap ``reader.read_note`` to count invocations; return a getter."""
    calls = {"n": 0}
    original = reader.read_note

    async def counting(path: Path) -> Note:
        calls["n"] += 1
        return await original(path)

    monkeypatch.setattr(reader, "read_note", counting)
    return lambda: calls["n"]


Reconcile = Callable[..., Awaitable[object]]


async def test_first_pass_indexes_all(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker
) -> None:
    total = len(await reader.stat_notes())
    assert total > 0

    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["reindexed_notes"] == total
    assert stats["skipped_notes"] == 0
    assert stats["deleted_notes"] == 0
    assert await store.get_generation() == 1


async def test_reports_note_progress(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker
) -> None:
    updates: list[tuple[int, int]] = []
    total = len(await reader.stat_notes())

    await reconcile(
        store,
        reader,
        chunker,
        mtime_gate=True,
        progress=lambda completed, count: updates.append((completed, count)),
    )

    assert updates == [(completed, total) for completed in range(total + 1)]


async def test_progress_advances_during_the_pre_pass_for_unchanged_notes(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full verification of an unchanged vault reports each note as it is read."""
    total = len(await reader.stat_notes())
    await reconcile(store, reader, chunker, mtime_gate=True)
    updates: list[tuple[int, int]] = []
    updates_before_each_read: list[int] = []
    original = reader.read_note

    async def observing(path: Path) -> Note:
        updates_before_each_read.append(len(updates))
        return await original(path)

    monkeypatch.setattr(reader, "read_note", observing)

    await reconcile(
        store,
        reader,
        chunker,
        mtime_gate=False,
        progress=lambda completed, count: updates.append((completed, count)),
    )

    assert updates == [(completed, total) for completed in range(total + 1)]
    # The first read sees only the initial report; the last one sees every earlier note.
    assert updates_before_each_read == list(range(1, total + 1))


async def test_generation_advances_only_for_changed_index(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    tmp_vault: Path,
) -> None:
    await reconcile(store, reader, chunker, mtime_gate=True)
    assert await store.get_generation() == 1

    await reconcile(store, reader, chunker, mtime_gate=True)
    assert await store.get_generation() == 1

    target = tmp_vault / "welcome.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    await reconcile(store, reader, chunker, mtime_gate=True)
    assert await store.get_generation() == 2


async def test_unchanged_pass_skips_without_reading(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = len(await reader.stat_notes())
    await reconcile(store, reader, chunker, mtime_gate=True)

    read_count = _spy_read_note(monkeypatch, reader)
    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["reindexed_notes"] == 0
    assert stats["skipped_notes"] == total
    assert read_count() == 0, "mtime-gated skip must not read or hash unchanged notes"


async def test_changed_content_is_reindexed(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker, tmp_vault: Path
) -> None:
    await reconcile(store, reader, chunker, mtime_gate=True)

    target = tmp_vault / "welcome.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n\nNew paragraph.\n", encoding="utf-8")

    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["reindexed_notes"] == 1
    assert stats["deleted_notes"] == 0


async def test_touched_unchanged_refreshes_mtime_then_skips(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    tmp_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch: mtime moved but content identical must refresh the stored mtime.

    Otherwise the note would be read+hashed on every subsequent pass forever.
    """
    await reconcile(store, reader, chunker, mtime_gate=True)

    target = tmp_vault / "welcome.md"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    # Pass after touch: read once (mtime moved), hash matches -> no reindex,
    # but the stored mtime is refreshed via record_mtime.
    read_after_touch = _spy_read_note(monkeypatch, reader)
    touched = await reconcile(store, reader, chunker, mtime_gate=True)
    assert touched["reindexed_notes"] == 0
    assert read_after_touch() == 1, "touched note must be read once to verify its hash"

    # Next pass: the refreshed mtime now matches -> pure skip, no read.
    read_after_refresh = _spy_read_note(monkeypatch, reader)
    settled = await reconcile(store, reader, chunker, mtime_gate=True)
    assert settled["skipped_notes"] == len(await reader.stat_notes())
    assert read_after_refresh() == 0, "refreshed mtime must let the next pass skip the note"


async def test_deleted_file_is_removed(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker, tmp_vault: Path
) -> None:
    await reconcile(store, reader, chunker, mtime_gate=True)

    (tmp_vault / "welcome.md").unlink()
    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["deleted_notes"] == 1
    assert "welcome.md" not in await store.list_indexed_notes_with_mtime()


async def test_an_undecodable_note_is_dropped_from_the_index(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker, tmp_vault: Path
) -> None:
    """Rows of a note re-saved in a legacy encoding describe bytes that are gone.

    They used to be kept like those of a transiently locked file, so every later
    listing or search re-read the note, hit the same decoding error and failed as
    a whole, and no pass ever removed them.
    """
    await reconcile(store, reader, chunker, mtime_gate=True)
    assert "welcome.md" in await store.list_indexed_notes_with_mtime()

    (tmp_vault / "welcome.md").write_bytes("# Café crème\n".encode("cp1252"))
    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["deleted_notes"] == 1
    assert "welcome.md" not in await store.list_indexed_notes_with_mtime()


async def test_a_transiently_unreadable_note_keeps_its_rows(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    tmp_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reconcile(store, reader, chunker, mtime_gate=True)
    original = reader.read_note

    async def locked(path: Path) -> Note:
        if path.name == "welcome.md":
            raise PermissionError("sharing violation")
        return await original(path)

    monkeypatch.setattr(reader, "read_note", locked)
    stats = await reconcile(store, reader, chunker, mtime_gate=False)

    assert stats["deleted_notes"] == 0
    assert "welcome.md" in await store.list_indexed_notes_with_mtime()


async def test_moved_note_keeps_index_without_stale_path(
    store: SQLiteFTS5Store,
    chunker: MarkdownChunker,
    tmp_path: Path,
) -> None:
    """A stable-id note moved to a new path must be reindexed, not deleted."""
    stable_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    vault = tmp_path / "vault"
    vault.mkdir()
    old_path = vault / "old.md"
    new_path = vault / "new.md"
    old_path.write_text(
        f"---\nid: {stable_id}\ntitle: Stable\n---\n# Stable\nbody\n",
        encoding="utf-8",
    )
    reader = FilesystemVaultReader(vault)

    await reconcile(store, reader, chunker, mtime_gate=True)
    old_path.rename(new_path)

    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    indexed = await store.list_indexed_notes_with_mtime()
    chunks = await store.list_chunks_for_note(stable_id)
    assert stats["deleted_notes"] == 1
    assert stats["reindexed_notes"] == 1
    assert set(indexed) == {"new.md"}
    assert indexed["new.md"][0] == stable_id
    assert "old.md" not in indexed
    assert chunks
    assert {chunk.note_rel_path for chunk in chunks} == {"new.md"}


async def test_mtime_gate_false_reads_all_but_skips_on_hash_match(
    store: SQLiteFTS5Store,
    reader: FilesystemVaultReader,
    chunker: MarkdownChunker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = len(await reader.stat_notes())
    await reconcile(store, reader, chunker, mtime_gate=True)

    read_count = _spy_read_note(monkeypatch, reader)
    stats = await reconcile(store, reader, chunker, mtime_gate=False)

    assert read_count() == total, "gate disabled must read every note for full verification"
    assert stats["reindexed_notes"] == 0, "unchanged content must not be re-upserted"
    assert stats["skipped_notes"] == total


async def test_id_change_same_path_deletes_old(
    store: SQLiteFTS5Store, reader: FilesystemVaultReader, chunker: MarkdownChunker, tmp_vault: Path
) -> None:
    """Same path, new ULID (frontmatter id added): the stale note is deleted."""
    target = tmp_vault / "welcome.md"
    await reconcile(store, reader, chunker, mtime_gate=True)
    before = await store.list_indexed_notes_with_mtime()
    old_id = before["welcome.md"][0]

    new_id = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    assert new_id != old_id
    body = target.read_text(encoding="utf-8")
    target.write_text(f"---\nid: {new_id}\n---\n{body}", encoding="utf-8")

    stats = await reconcile(store, reader, chunker, mtime_gate=True)

    assert stats["deleted_notes"] == 1
    assert stats["reindexed_notes"] == 1
    after = await store.list_indexed_notes_with_mtime()
    assert after["welcome.md"][0] == new_id
    assert await store.list_chunks_for_note(old_id) == []


async def test_pre_pass_keeps_a_bounded_number_of_notes_alive(
    store: SQLiteFTS5Store,
    chunker: MarkdownChunker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full pass over the vault holds a number of Note objects that does not grow with it.

    The bound is deliberately not tight. What this test exists to catch is a
    pre-pass that retains the notes it read instead of their identities, and
    that regression shows ``note_count`` live objects, not three. A bound of two
    instead measured the interpreter's transient references, so any unrelated
    test that ran earlier and changed the heap could move it: it failed on
    Python 3.13 on Linux when a test was added to another module, while the code
    under test here never ran in that module at all.
    """
    note_count = 500
    vault = tmp_path / "synthetic"
    vault.mkdir()
    for index in range(note_count):
        (vault / f"note-{index:03d}.md").write_text(
            f"---\nid: 01J5N{index:05d}0000000000000001\n---\n# Note {index}\n\nBody {index}.\n",
            encoding="utf-8",
        )
    reader = FilesystemVaultReader(vault, read_only=True)
    # A Note holds a dict, so it is not hashable; track liveness by object identity.
    alive: weakref.WeakValueDictionary[int, Note] = weakref.WeakValueDictionary()
    peak = 0
    original = reader.read_note

    async def tracking(path: Path) -> Note:
        nonlocal peak
        note = await original(path)
        alive[id(note)] = note
        gc.collect()
        peak = max(peak, len(alive))
        return note

    monkeypatch.setattr(reader, "read_note", tracking)

    stats = await reconcile(store, reader, chunker, mtime_gate=False)

    assert stats["reindexed_notes"] == note_count
    assert peak <= 8, f"{peak} of {note_count} Note objects were alive at once"


def _write_vault(vault: Path, note_count: int) -> None:
    """Fill ``vault`` with notes that carry no frontmatter id."""
    for index in range(note_count):
        (vault / f"note-{index:04d}.md").write_text(
            f"# Note {index}\n\nbody {index}\n", encoding="utf-8"
        )


async def _count_pass_costs(
    tmp_path: Path, vault: Path, chunker: MarkdownChunker
) -> tuple[int, int]:
    """Run one cold pass and return its sidecar rewrites and its durable commits."""
    sidecar_rewrites = 0
    commits = 0
    original_replace = os.replace
    original_commit = aiosqlite.Connection.commit

    def _counting_replace(src: object, dst: object, **kwargs: object) -> None:
        nonlocal sidecar_rewrites
        if str(dst).endswith("ulids.json"):
            sidecar_rewrites += 1
        original_replace(src, dst, **kwargs)  # type: ignore[arg-type]

    async def _counting_commit(self: aiosqlite.Connection) -> None:
        nonlocal commits
        commits += 1
        await original_commit(self)

    store = SQLiteFTS5Store()
    await store.open(tmp_path / f"{vault.name}.db")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", _counting_replace)
            patch.setattr(aiosqlite.Connection, "commit", _counting_commit)
            reader = FilesystemVaultReader(vault)
            stats = await reconcile(store, reader, chunker, mtime_gate=True)
        assert stats["reindexed_notes"] == len(list(vault.glob("*.md")))
    finally:
        await store.close()
    return sidecar_rewrites, commits


async def test_a_pass_writes_the_identity_sidecar_once_whatever_the_vault_size(
    tmp_path: Path, chunker: MarkdownChunker
) -> None:
    """Resolving identities must not rewrite the sidecar once per note.

    The whole mapping was reserialized and the file replaced on every resolved
    identity, so a cold index of a vault of id-less notes wrote it once per note and
    the total bytes grew with the square of the vault. Two vaults of very different
    sizes must now cost the same one write, which no per-note rewrite can satisfy at
    any threshold.
    """
    small = tmp_path / "small"
    large = tmp_path / "large"
    for vault, note_count in ((small, 5), (large, 60)):
        vault.mkdir()
        _write_vault(vault, note_count)

    small_rewrites, _ = await _count_pass_costs(tmp_path, small, chunker)
    large_rewrites, _ = await _count_pass_costs(tmp_path, large, chunker)

    assert small_rewrites == large_rewrites == 1


async def test_a_pass_commits_in_batches_rather_than_once_per_note(
    tmp_path: Path, chunker: MarkdownChunker
) -> None:
    """Indexing a vault must not pay a durable commit per note.

    Every ``upsert_note`` opened and committed its own transaction, so a cold index
    of N notes performed N commits and they dominated its wall clock. The bound here
    is deliberately loose: what it catches is a commit count that tracks the note
    count, and the schema work around the pass contributes a handful of its own.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note_count = 60
    _write_vault(vault, note_count)

    _, commits = await _count_pass_costs(tmp_path, vault, chunker)

    assert commits < note_count // 2, f"{commits} commits for {note_count} notes"


async def test_identities_survive_a_pass_that_fails_before_its_end(
    tmp_path: Path, chunker: MarkdownChunker
) -> None:
    """A pass that raises still persists the identities it resolved.

    Deferring the writes is only safe because the identity of an id-less note is a
    pure function of its path, so nothing is lost either way. The sidecar is still
    flushed on the way out, because leaving a cache closer to the truth costs nothing
    and a half-written pass should not make the next one redo the work.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_vault(vault, 4)
    sidecar = vault / ".datacron" / "ulids.json"

    reader = FilesystemVaultReader(vault)

    async def _pass_that_fails() -> None:
        async with reader.defer_identity_writes():
            for path in sorted(vault.glob("*.md")):
                await reader.read_note(path)
            raise RuntimeError("the pass gave up here")

    with pytest.raises(RuntimeError, match="pass gave up"):
        await _pass_that_fails()

    assert sidecar.is_file()
    assert len(json.loads(sidecar.read_text(encoding="utf-8"))) == 4


async def test_a_failed_write_discards_its_batch_and_leaves_the_store_usable(
    tmp_path: Path, chunker: MarkdownChunker
) -> None:
    """A batch is one transaction, so a failure inside it takes the batch with it.

    That is the weaker guarantee batching buys, and it is only acceptable because the
    index is derived: the discarded notes are reindexed by the next pass. What must
    not happen is a store left inside an open transaction, which would refuse every
    later write.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_vault(vault, 3)
    store = SQLiteFTS5Store()
    await store.open(tmp_path / "index.db")
    try:
        reader = FilesystemVaultReader(vault)
        notes = [await reader.read_note(path) for path in sorted(vault.glob("*.md"))]

        async def _batch_that_fails() -> None:
            async with store.bulk_writes(commit_every=1000):
                await store.upsert_note(notes[0], chunker.chunk(notes[0]))
                raise RuntimeError("the batch gave up here")

        with pytest.raises(RuntimeError, match="batch gave up"):
            await _batch_that_fails()

        assert await store.list_indexed_notes_with_mtime() == {}

        async with store.bulk_writes(commit_every=1000):
            for note in notes:
                await store.upsert_note(note, chunker.chunk(note))
        assert len(await store.list_indexed_notes_with_mtime()) == 3
    finally:
        await store.close()


async def test_a_bulk_scope_refuses_to_nest(tmp_path: Path) -> None:
    """Two scopes would disagree about who owns the open transaction."""
    store = SQLiteFTS5Store()
    await store.open(tmp_path / "index.db")
    try:

        async def _nested() -> None:
            async with store.bulk_writes():
                pass

        async with store.bulk_writes():
            with pytest.raises(RuntimeError, match="already open"):
                await _nested()
    finally:
        await store.close()
