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

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import aiosqlite
import pytest

from datacron.core.models import Chunk, ChunkType, Note
from datacron.core.temporal import TemporalMeta
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
    assert {"notes", "chunks_fts", "ulid_paths", "index_meta"} <= tables


async def test_generation_counter_and_legacy_default(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    store = SQLiteFTS5Store()
    await store.open(db_path)
    assert await store.get_generation() == 0
    await store.set_generation(4)
    assert await store.increment_generation() == 5
    assert (await store.stats()).generation == 5
    await store.close()

    legacy_path = tmp_path / "legacy.db"
    sqlite3.connect(legacy_path).close()
    legacy = SQLiteFTS5Store()
    await legacy.open(legacy_path, read_only=True)
    assert await legacy.get_generation() == 0
    await legacy.close()


async def test_read_only_open_requires_prebuilt_index_without_creating_parent(
    tmp_path: Path,
) -> None:
    db_path = _db_path(tmp_path)
    store = SQLiteFTS5Store()

    with pytest.raises(FileNotFoundError, match="requires a prebuilt index"):
        await store.open(db_path, read_only=True)

    assert not db_path.parent.exists()


async def test_immutable_read_only_open_refuses_mutation_and_sidecars(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    writable = SQLiteFTS5Store()
    await writable.open(db_path)
    await writable.close()
    before = (
        hashlib.sha256(db_path.read_bytes()).hexdigest(),
        db_path.stat().st_mtime_ns,
    )

    read_only = SQLiteFTS5Store()
    await read_only.open(db_path, read_only=True)
    assert (await read_only.stats()).note_count == 0
    with pytest.raises(PermissionError, match="immutable read-only index"):
        await read_only.delete_note(_NOTE_ID)
    await read_only.close()

    after = (
        hashlib.sha256(db_path.read_bytes()).hexdigest(),
        db_path.stat().st_mtime_ns,
    )
    assert before == after
    assert not db_path.with_name(f"{db_path.name}-wal").exists()
    assert not db_path.with_name(f"{db_path.name}-shm").exists()


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


async def test_get_note_rel_path_uses_indexed_identity(
    tmp_path: Path,
    note_factory: NoteFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="folder/welcome.md")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [])

    assert await store.get_note_rel_path(note.id) == note.rel_path
    assert await store.get_note_rel_path(_OTHER_NOTE_ID) is None
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


async def test_search_with_empty_query_expansion_matches_default(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    first = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="alpha beta",
    )
    second = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0001",
        content="alpha only",
        ordinal=1,
    )
    default_store = SQLiteFTS5Store()
    empty_map_store = SQLiteFTS5Store(term_map={})
    await default_store.open(tmp_path / "default" / "datacron.db")
    await empty_map_store.open(tmp_path / "empty-map" / "datacron.db")

    await default_store.upsert_note(note, [first, second])
    await empty_map_store.upsert_note(note, [first, second])

    default_results = await default_store.search("alpha beta", limit=5)
    empty_map_results = await empty_map_store.search("alpha beta", limit=5)

    assert empty_map_results == default_results
    await default_store.close()
    await empty_map_store.close()


async def test_search_expands_curated_terms_at_query_time(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="monitoring.md")
    chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="OSCARE monitoring guide",
    )
    plain_store = SQLiteFTS5Store()
    expanded_store = SQLiteFTS5Store(term_map={"supervision": ["monitoring"]})
    await plain_store.open(tmp_path / "plain" / "datacron.db")
    await expanded_store.open(tmp_path / "expanded" / "datacron.db")

    await plain_store.upsert_note(note, [chunk])
    await expanded_store.upsert_note(note, [chunk])

    assert await plain_store.search("supervision", limit=5) == []
    expanded_results = await expanded_store.search("supervision", limit=5)

    assert [result.chunk for result in expanded_results] == [chunk]
    await plain_store.close()
    await expanded_store.close()


async def test_search_expansion_keeps_multi_term_queries_precise(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="monitoring.md")
    target = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::target::0000",
        content="OSCARE monitoring setup",
    )
    monitoring_only = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::monitoring::0001",
        content="monitoring alerts",
        ordinal=1,
    )
    oscare_only = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::oscare::0002",
        content="OSCARE onboarding",
        ordinal=2,
    )
    unrelated = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::unrelated::0003",
        content="backup restore",
        ordinal=3,
    )
    store = SQLiteFTS5Store(term_map={"supervision": ["monitoring"]})
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [target, monitoring_only, oscare_only, unrelated])
    results = await store.search("supervision oscare", limit=10)

    assert results[0].chunk == target
    assert unrelated not in [result.chunk for result in results]
    assert len(results) < 4
    await store.close()


async def test_search_multi_term_query_preserves_and_when_hits_exist(
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
    results = await store.search("alpha beta", limit=1)

    assert [result.chunk for result in results] == [non_adjacent]
    await store.close()


async def test_search_multi_term_query_falls_back_to_or_when_and_has_no_hits(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="oscare.md")
    target = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::target::0000",
        content="OSCARE tenant setup guide",
        ordinal=0,
    )
    other = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::other::0001",
        content="API key rotation",
        ordinal=1,
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    await store.upsert_note(note, [target, other])

    results = await store.search("demander tenant OSCARE cle API", limit=10)

    assert results
    assert target in [result.chunk for result in results]
    await store.close()


async def test_search_multi_term_query_tops_up_sparse_and_results_with_or(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="oscare.md")
    false_positive = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::false-positive::0000",
        content="demander tenant OSCARE cle API support log",
        ordinal=0,
    )
    target = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::target::0001",
        content="OSCARE tenant setup guide",
        ordinal=1,
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    await store.upsert_note(note, [false_positive, target])

    results = await store.search("demander tenant OSCARE cle API", limit=5)

    assert results[0].chunk == false_positive
    assert target in [result.chunk for result in results[1:]]
    assert len({result.chunk.chunk_id for result in results}) == len(results)
    await store.close()


@pytest.mark.parametrize("query", ["f(x):", "a-b", 'said "alpha"', '"unterminated'])
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
        content='Formula f(x): and token a-b because she said "alpha". literal unterminated.',
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


async def test_upsert_stores_and_lists_fs_mtime(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md", content="Body")
    chunk = chunk_factory(note=note, chunk_id=f"{note.id}::::0000", content="Body")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [chunk], fs_mtime_ns=1_234_567_890)
    assert await store.list_indexed_notes_with_mtime() == {
        "welcome.md": (_NOTE_ID, note.content_hash, 1_234_567_890)
    }

    # Without an mtime the stored value is NULL -> None ("always re-read").
    await store.upsert_note(note, [chunk])
    assert await store.list_indexed_notes_with_mtime() == {
        "welcome.md": (_NOTE_ID, note.content_hash, None)
    }
    await store.close()


async def test_list_temporal_metadata_reads_frontmatter_json(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    current = note_factory(
        id=_NOTE_ID,
        rel_path="current.md",
        frontmatter={"confidence": "low", "supersedes": [_OTHER_NOTE_ID, ""]},
    )
    current_chunk = chunk_factory(note=current, chunk_id=f"{current.id}::::0000")
    plain = note_factory(id=_OTHER_NOTE_ID, rel_path="plain.md", frontmatter={})
    plain_chunk = chunk_factory(note=plain, chunk_id=f"{plain.id}::::0000")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(current, [current_chunk])
    await store.upsert_note(plain, [plain_chunk])

    assert await store.list_temporal_metadata() == {
        _NOTE_ID: TemporalMeta(confidence="low", supersedes=[_OTHER_NOTE_ID]),
        _OTHER_NOTE_ID: TemporalMeta(confidence=None, supersedes=[]),
    }
    await store.close()


async def test_record_mtime_updates_only_mtime(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md", content="Body")
    chunk = chunk_factory(note=note, chunk_id=f"{note.id}::::0000", content="Body")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(note, [chunk])
    await store.record_mtime(_NOTE_ID, 999)

    entry = (await store.list_indexed_notes_with_mtime())["welcome.md"]
    assert entry == (_NOTE_ID, note.content_hash, 999)
    await store.close()


async def test_legacy_db_without_fs_mtime_is_migrated(tmp_path: Path) -> None:
    """A notes table created before fs_mtime existed gains the column on open."""
    db_path = _db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_hash = "0" * 64
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE notes (
                note_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                title TEXT NOT NULL,
                frontmatter_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO notes VALUES (?, 'legacy.md', 'Legacy', '{}', ?, ?, ?, ?);",
            (
                _NOTE_ID,
                legacy_hash,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()

    store = SQLiteFTS5Store()
    await store.open(db_path)

    # Migration ran: the column exists and the legacy row reads back with None.
    assert await store.list_indexed_notes_with_mtime() == {
        "legacy.md": (_NOTE_ID, legacy_hash, None)
    }
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
