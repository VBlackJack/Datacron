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

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

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
