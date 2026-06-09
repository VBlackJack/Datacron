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
"""Tests for the SQLite FTS5 store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import aiosqlite
import pytest

from datacron.core.models import Chunk, ChunkType, Note
from datacron.indexing.fts5_store import SQLiteFTS5Store

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_NOTE_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_OTHER_NOTE_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "vault" / ".datacron" / "index" / "datacron.db"


async def _ulid_rows(db_path: Path) -> dict[str, str]:
    async with (
        aiosqlite.connect(db_path) as connection,
        connection.execute("SELECT rel_path, note_id FROM ulid_paths ORDER BY rel_path;") as cursor,
    ):
        rows = await cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


async def _note_count(db_path: Path) -> int:
    async with (
        aiosqlite.connect(db_path) as connection,
        connection.execute("SELECT COUNT(*) FROM notes;") as cursor,
    ):
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_open_creates_schema(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    store = SQLiteFTS5Store()

    await store.open(db_path)
    await store.close()

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table');"
            )
        }
    assert {"notes", "chunks_fts", "ulid_paths"} <= tables


async def test_migration_imports_ulids_and_keeps_sidecar_readable(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    sidecar_dir = db_path.parent.parent
    sidecar_dir.mkdir(parents=True)
    ulids_path = sidecar_dir / "ulids.json"
    ulids_path.write_text(
        json.dumps(
            {
                "welcome.md": _NOTE_ID,
                "folder/other.md": _OTHER_NOTE_ID,
            }
        ),
        encoding="utf-8",
    )

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()

    assert ulids_path.exists()
    assert (sidecar_dir / "ulids.json.migrated").exists()
    assert await _ulid_rows(db_path) == {
        "folder/other.md": _OTHER_NOTE_ID,
        "welcome.md": _NOTE_ID,
    }


async def test_migration_is_idempotent_when_migrated_sidecar_exists(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    sidecar_dir = db_path.parent.parent
    sidecar_dir.mkdir(parents=True)
    ulids_path = sidecar_dir / "ulids.json"
    migrated_path = sidecar_dir / "ulids.json.migrated"
    ulids_path.write_text(json.dumps({"welcome.md": _NOTE_ID}), encoding="utf-8")
    migrated_path.write_text(json.dumps({"already.md": _OTHER_NOTE_ID}), encoding="utf-8")

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()

    assert ulids_path.exists()
    assert await _ulid_rows(db_path) == {"welcome.md": _NOTE_ID}


async def test_migration_no_ulids_sidecar_is_noop(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()

    assert not (db_path.parent.parent / "ulids.json.migrated").exists()
    assert await _ulid_rows(db_path) == {}


async def test_migration_restores_primary_sidecar_from_migrated_file(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    sidecar_dir = db_path.parent.parent
    sidecar_dir.mkdir(parents=True)
    migrated_path = sidecar_dir / "ulids.json.migrated"
    migrated_path.write_text(json.dumps({"welcome.md": _NOTE_ID}), encoding="utf-8")

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()

    assert (sidecar_dir / "ulids.json").exists()
    assert await _ulid_rows(db_path) == {"welcome.md": _NOTE_ID}


async def test_migration_conflict_uses_insert_or_ignore(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    sidecar_dir = db_path.parent.parent
    sidecar_dir.mkdir(parents=True)

    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.close()

    async with aiosqlite.connect(db_path) as connection:
        await connection.execute(
            "INSERT INTO ulid_paths(rel_path, note_id) VALUES (?, ?);",
            ("welcome.md", _NOTE_ID),
        )
        await connection.commit()

    (sidecar_dir / "ulids.json").write_text(
        json.dumps(
            {
                "welcome.md": _OTHER_NOTE_ID,
                "new.md": "01HQXR7K9YZ8M2N3PQRSTV4WX7",
            }
        ),
        encoding="utf-8",
    )

    reopened = SQLiteFTS5Store()
    await reopened.open(db_path)
    await reopened.close()

    assert await _ulid_rows(db_path) == {
        "new.md": "01HQXR7K9YZ8M2N3PQRSTV4WX7",
        "welcome.md": _NOTE_ID,
    }
    assert (sidecar_dir / "ulids.json.migrated").exists()


async def test_upsert_get_list_search_and_stats(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(
        id=_NOTE_ID,
        rel_path="welcome.md",
        title="Welcome",
        frontmatter={"title": "Welcome"},
    )
    first = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::intro::0000",
        header_path="Intro",
        section_title="Intro",
        chunk_type=ChunkType.HEADING,
        content="Kafka adoption overview",
        ordinal=0,
        wikilinks_out=["Architecture"],
    )
    second = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::intro::0001",
        header_path="Intro",
        section_title="Intro",
        content="Postgres and SQLite notes",
        ordinal=1,
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [first, second])

    found = await store.get_chunk(first.chunk_id)
    listed = await store.list_chunks_for_note(note.id)
    streamed = [chunk async for chunk in store.iter_all_chunks()]
    results = await store.search("kafka", limit=5)
    stats = await store.stats()
    await store.close()

    assert found == first
    assert listed == [first, second]
    assert streamed == [first, second]
    assert len(results) == 1
    assert results[0].chunk == first
    assert "**Kafka**" in results[0].snippet
    assert results[0].score > 0
    assert stats.note_count == 1
    assert stats.chunk_count == 2
    assert stats.last_indexed_at is not None
    assert stats.db_size_bytes > 0


async def test_upsert_replaces_existing_chunks(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    old_chunk = chunk_factory(note=note, chunk_id=f"{note.id}::::0000", content="Old Kafka")
    new_chunk = chunk_factory(note=note, chunk_id=f"{note.id}::::0001", content="New Kafka")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [old_chunk])
    await store.upsert_note(note, [new_chunk])

    assert await store.get_chunk(old_chunk.chunk_id) is None
    assert await store.list_chunks_for_note(note.id) == [new_chunk]
    await store.close()


async def test_delete_note_removes_note_chunks_and_ulid_path(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    chunk = chunk_factory(note=note, chunk_id=f"{note.id}::::0000")
    store = SQLiteFTS5Store()
    await store.open(db_path)

    await store.upsert_note(note, [chunk])
    await store.delete_note(note.id)

    assert await store.get_chunk(chunk.chunk_id) is None
    assert await store.list_chunks_for_note(note.id) == []
    await store.close()
    assert await _note_count(db_path) == 0
    assert await _ulid_rows(db_path) == {}


async def test_search_empty_query_and_non_positive_limit_are_empty(tmp_path: Path) -> None:
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    assert await store.search("") == []
    assert await store.search("kafka", limit=0) == []

    await store.close()


async def test_search_multi_term_query_uses_implicit_and_not_phrase(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    non_adjacent = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="alpha words in the middle beta",
    )
    only_alpha = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0001",
        content="alpha only",
        ordinal=1,
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [non_adjacent, only_alpha])
    results = await store.search("alpha beta", limit=5)

    assert [result.chunk for result in results] == [non_adjacent]
    await store.close()


@pytest.mark.parametrize("query", ["f(x):", "a-b", 'said "alpha"'])
async def test_search_special_characters_are_treated_as_literals(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
    query: str,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content='Formula f(x): and token a-b because she said "alpha".',
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [chunk])
    results = await store.search(query, limit=5)

    assert len(results) == 1
    assert results[0].chunk == chunk
    await store.close()


async def test_list_indexed_notes_and_wikilink_chunks(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md", content="See [[Other]].")
    chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="See [[Other]].",
        wikilinks_out=["Other"],
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [chunk])

    assert await store.list_indexed_notes() == {"welcome.md": (_NOTE_ID, note.content_hash)}
    assert await store.list_chunks_with_wikilinks() == [chunk]
    await store.close()


async def test_upsert_replaces_stale_note_id_for_same_path(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    old_note = note_factory(id=_NOTE_ID, rel_path="welcome.md", content="Old")
    old_chunk = chunk_factory(note=old_note, chunk_id=f"{old_note.id}::::0000", content="Old")
    new_note = note_factory(id=_OTHER_NOTE_ID, rel_path="welcome.md", content="New")
    new_chunk = chunk_factory(note=new_note, chunk_id=f"{new_note.id}::::0000", content="New")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(old_note, [old_chunk])
    await store.upsert_note(new_note, [new_chunk])

    stats = await store.stats()
    assert stats.note_count == 1
    assert await store.get_chunk(old_chunk.chunk_id) is None
    assert await store.get_chunk(new_chunk.chunk_id) == new_chunk
    await store.close()


async def test_upsert_rejects_chunk_for_different_note(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    other_note = note_factory(id=_OTHER_NOTE_ID, rel_path="other.md")
    chunk = chunk_factory(note=other_note, chunk_id=f"{other_note.id}::::0000")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    with pytest.raises(ValueError, match="belongs to note"):
        await store.upsert_note(note, [chunk])

    await store.close()
