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

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from datacron.core.models import Chunk, ChunkType, Note, SearchResult
from datacron.core.temporal import TemporalMeta
from datacron.indexing.fts5_store import SQLiteFTS5Store

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_NOTE_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_OTHER_NOTE_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_SUBPROCESS_TIMEOUT_SECONDS = 20
_SUBPROCESS_TERMINATION_TIMEOUT_SECONDS = 5


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


async def _replace_note_in_store(db_path: Path, note_json: str, chunk_json: str) -> None:
    writer = SQLiteFTS5Store()
    await writer.open(db_path)
    try:
        note = Note.model_validate_json(note_json)
        chunk = Chunk.model_validate_json(chunk_json)
        await writer.upsert_note(note, [chunk])
        await writer.increment_generation()
    finally:
        await writer.close()


def _replace_note_in_subprocess(db_path: Path, note_json: str, chunk_json: str) -> None:
    asyncio.run(_replace_note_in_store(db_path, note_json, chunk_json))


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


async def test_read_only_open_refuses_mutation_and_sidecars(tmp_path: Path) -> None:
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
    with pytest.raises(PermissionError, match="read-only index"):
        await read_only.delete_note(_NOTE_ID)
    await read_only.close()

    after = (
        hashlib.sha256(db_path.read_bytes()).hexdigest(),
        db_path.stat().st_mtime_ns,
    )
    assert before == after
    assert not db_path.with_name(f"{db_path.name}-wal").exists()
    assert not db_path.with_name(f"{db_path.name}-shm").exists()


async def test_read_only_reader_observes_committed_writer_updates(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    initial_chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="legacyanchor",
    )
    replacement_chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="replacementanchor",
    )
    writer = SQLiteFTS5Store()
    reader = SQLiteFTS5Store()

    await writer.open(db_path)
    try:
        await writer.upsert_note(note, [initial_chunk])
        await writer.increment_generation()
        await reader.open(db_path, read_only=True)

        assert await reader.get_generation() == 1
        assert [result.chunk.content for result in await reader.search("legacyanchor")] == [
            "legacyanchor"
        ]

        await writer.upsert_note(note, [replacement_chunk])
        await writer.increment_generation()

        assert await reader.get_generation() == 2
        assert await reader.search("legacyanchor") == []
        assert [result.chunk.content for result in await reader.search("replacementanchor")] == [
            "replacementanchor"
        ]
    finally:
        await reader.close()
        await writer.close()


async def test_read_only_reader_observes_updates_committed_by_another_process(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    note = note_factory(id=_NOTE_ID, rel_path="welcome.md")
    initial_chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="legacyprocessanchor",
    )
    replacement_chunk = chunk_factory(
        note=note,
        chunk_id=f"{note.id}::::0000",
        content="replacementprocessanchor",
    )
    seed_writer = SQLiteFTS5Store()
    reader = SQLiteFTS5Store()

    await seed_writer.open(db_path)
    try:
        await seed_writer.upsert_note(note, [initial_chunk])
        await seed_writer.increment_generation()
    finally:
        await seed_writer.close()

    await reader.open(db_path, read_only=True)
    process = multiprocessing.get_context("spawn").Process(
        target=_replace_note_in_subprocess,
        args=(db_path, note.model_dump_json(), replacement_chunk.model_dump_json()),
    )
    try:
        assert await reader.get_generation() == 1
        assert [result.chunk.content for result in await reader.search("legacyprocessanchor")] == [
            "legacyprocessanchor"
        ]

        process.start()
        await asyncio.to_thread(process.join, _SUBPROCESS_TIMEOUT_SECONDS)
        assert not process.is_alive(), (
            f"writer subprocess did not finish within {_SUBPROCESS_TIMEOUT_SECONDS} seconds"
        )
        assert process.exitcode == 0

        assert await reader.get_generation() == 2
        assert await reader.search("legacyprocessanchor") == []
        assert [
            result.chunk.content for result in await reader.search("replacementprocessanchor")
        ] == ["replacementprocessanchor"]
    finally:
        await reader.close()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, _SUBPROCESS_TERMINATION_TIMEOUT_SECONDS)
        process.close()


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
    assert await store.get_note_id(note.rel_path) == note.id
    assert await store.get_note_id("missing.md") is None
    await store.close()


async def test_list_note_paths_paginates_in_vault_order_and_filters(
    tmp_path: Path,
    note_factory: NoteFactory,
) -> None:
    notes = [
        note_factory(id=_NOTE_ID, rel_path="zeta.md", tags=["root"]),
        note_factory(id=_OTHER_NOTE_ID, rel_path="alpha.md", tags=["root", "shared"]),
        note_factory(
            id="01HQXR7K9YZ8M2N3PQRSTV4WX7",
            rel_path="a/nested.md",
            tags=["shared"],
        ),
        note_factory(
            id="01HQXR7K9YZ8M2N3PQRSTV4WX8",
            rel_path="a/deep/child.md",
            tags=["deep"],
        ),
        note_factory(id="01HQXR7K9YZ8M2N3PQRSTV4WX9", rel_path="proj/a.md"),
        note_factory(id="01HQXR7K9YZ8M2N3PQRSTV4WXA", rel_path="proj/sub/c.md"),
        note_factory(id="01HQXR7K9YZ8M2N3PQRSTV4WXB", rel_path="proj-x/b.md"),
        note_factory(id="01HQXR7K9YZ8M2N3PQRSTV4WXC", rel_path="proj.old/f.md"),
    ]
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    for note in notes:
        await store.upsert_note(note, [])

    page, total = await store.list_note_paths(folder=None, tags=[], limit=2, offset=1)
    shared, shared_total = await store.list_note_paths(
        folder=None,
        tags=["shared"],
        limit=10,
        offset=0,
    )
    nested, nested_total = await store.list_note_paths(
        folder="a",
        tags=[],
        limit=10,
        offset=0,
    )
    boundary_page, boundary_total = await store.list_note_paths(
        folder=None,
        tags=[],
        limit=3,
        offset=5,
    )
    await store.close()

    assert total == 8
    assert page == ["zeta.md", "a/nested.md"]
    assert shared_total == 2
    assert shared == ["alpha.md", "a/nested.md"]
    assert nested_total == 2
    assert nested == ["a/nested.md", "a/deep/child.md"]
    assert boundary_total == 8
    assert boundary_page == ["proj/sub/c.md", "proj-x/b.md", "proj.old/f.md"]


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
    assert results[0].tier == 0
    assert target in [result.chunk for result in results[1:]]
    assert all(result.tier == 1 for result in results[1:])
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
        frontmatter={
            "confidence": "low",
            "supersedes": [_OTHER_NOTE_ID, ""],
            "valid_from": "2026-07-01",
            "invalid_at": "2026-07-17T08:30:00+00:00",
            "invalidated_by": _OTHER_NOTE_ID,
        },
    )
    current_chunk = chunk_factory(note=current, chunk_id=f"{current.id}::::0000")
    plain = note_factory(id=_OTHER_NOTE_ID, rel_path="plain.md", frontmatter={})
    plain_chunk = chunk_factory(note=plain, chunk_id=f"{plain.id}::::0000")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))

    await store.upsert_note(current, [current_chunk])
    await store.upsert_note(plain, [plain_chunk])

    assert await store.list_temporal_metadata() == {
        _NOTE_ID: TemporalMeta(
            confidence="low",
            supersedes=[_OTHER_NOTE_ID],
            valid_from="2026-07-01",
            invalid_at="2026-07-17T08:30:00+00:00",
            invalidated_by=_OTHER_NOTE_ID,
        ),
        _OTHER_NOTE_ID: TemporalMeta(confidence=None, supersedes=[]),
    }
    await store.close()


async def test_list_temporal_metadata_caches_same_generation(tmp_path: Path) -> None:
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    await store.set_generation(1)
    connection = store._conn
    assert connection is not None
    statements: list[str] = []
    await connection.set_trace_callback(statements.append)

    assert await store.list_temporal_metadata() == {}
    assert await store.list_temporal_metadata() == {}

    queries = [
        statement for statement in statements if "SELECT note_id, frontmatter_json" in statement
    ]
    assert len(queries) == 1
    await store.close()


async def test_list_temporal_metadata_recomputes_after_generation_bump(tmp_path: Path) -> None:
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    await store.set_generation(1)
    connection = store._conn
    assert connection is not None
    statements: list[str] = []
    await connection.set_trace_callback(statements.append)

    assert await store.list_temporal_metadata() == {}
    assert await store.increment_generation() == 2
    assert await store.list_temporal_metadata() == {}

    queries = [
        statement for statement in statements if "SELECT note_id, frontmatter_json" in statement
    ]
    assert len(queries) == 2
    await store.close()


async def test_list_temporal_metadata_never_caches_generation_zero(tmp_path: Path) -> None:
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    connection = store._conn
    assert connection is not None
    statements: list[str] = []
    await connection.set_trace_callback(statements.append)

    assert await store.list_temporal_metadata() == {}
    assert await store.list_temporal_metadata() == {}

    queries = [
        statement for statement in statements if "SELECT note_id, frontmatter_json" in statement
    ]
    assert len(queries) == 2
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
    assert await store.list_note_paths(folder=None, tags=[], limit=10, offset=0) == (
        ["legacy.md"],
        1,
    )
    await store.close()


async def test_legacy_discovery_sort_keys_are_backfilled(
    tmp_path: Path,
    note_factory: NoteFactory,
) -> None:
    db_path = _db_path(tmp_path)
    notes = (
        note_factory(id=_NOTE_ID, rel_path="proj/a.md"),
        note_factory(id=_OTHER_NOTE_ID, rel_path="proj-x/b.md"),
    )
    store = SQLiteFTS5Store()
    await store.open(db_path)
    for note in notes:
        await store.upsert_note(note, [])
    await store.close()

    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "UPDATE notes SET sort_key = ? WHERE rel_path = ?;",
            (("1proj/0a.md", "proj/a.md"), ("1proj-x/0b.md", "proj-x/b.md")),
        )
        connection.commit()

    await store.open(db_path)
    paths, total = await store.list_note_paths(folder=None, tags=[], limit=10, offset=0)
    await store.close()

    with sqlite3.connect(db_path) as connection:
        migrated = dict(connection.execute("SELECT rel_path, sort_key FROM notes;"))
    assert total == 2
    assert paths == ["proj/a.md", "proj-x/b.md"]
    assert migrated == {
        "proj/a.md": "1proj\x000a.md",
        "proj-x/b.md": "1proj-x\x000b.md",
    }


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


_LEGACY_CHUNKS_FTS_SQL = """
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    note_id UNINDEXED,
    note_rel_path UNINDEXED,
    header_path UNINDEXED,
    section_title UNINDEXED,
    chunk_type UNINDEXED,
    content,
    ordinal UNINDEXED,
    content_hash UNINDEXED,
    token_count UNINDEXED,
    line_start UNINDEXED,
    line_end UNINDEXED,
    wikilinks_out_json UNINDEXED,
    lang UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def _paths(results: list[SearchResult]) -> list[str]:
    return sorted(result.chunk.note_rel_path for result in results)


async def test_search_scope_filters_folder_tags_and_frontmatter(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    project = note_factory(
        id=_NOTE_ID,
        rel_path="projects/alpha.md",
        tags=["memory/project"],
        frontmatter={"confidence": "high"},
    )
    person = note_factory(
        id=_OTHER_NOTE_ID,
        rel_path="people/bob.md",
        tags=["memory/person"],
        frontmatter={"confidence": "low"},
    )
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    try:
        await store.upsert_note(
            project, [chunk_factory(note=project, content="scopeanchor in project")]
        )
        await store.upsert_note(
            person, [chunk_factory(note=person, content="scopeanchor in person")]
        )

        assert _paths(await store.search("scopeanchor")) == ["people/bob.md", "projects/alpha.md"]
        assert _paths(await store.search("scopeanchor", folder="projects")) == ["projects/alpha.md"]
        assert _paths(await store.search("scopeanchor", folder="projects/")) == [
            "projects/alpha.md"
        ]
        assert _paths(await store.search("scopeanchor", folder="proj")) == []
        assert _paths(await store.search("scopeanchor", tags=["Memory/Person"])) == [
            "people/bob.md"
        ]
        assert _paths(await store.search("scopeanchor", tags=["memory/person", "missing"])) == []
        assert _paths(await store.search("scopeanchor", frontmatter={"confidence": "LOW"})) == [
            "people/bob.md"
        ]
        assert _paths(await store.search("scopeanchor", frontmatter={"confidence": "none"})) == []
        assert _paths(
            await store.search(
                "scopeanchor",
                folder="people",
                tags=["memory/person"],
                frontmatter={"confidence": "low"},
            )
        ) == ["people/bob.md"]
        assert (
            _paths(
                await store.search(
                    "scopeanchor", folder="people", frontmatter={"confidence": "high"}
                )
            )
            == []
        )
        # The OR fallback for multi-term queries honours the same scope.
        assert _paths(await store.search("scopeanchor zzzabsent", folder="people")) == [
            "people/bob.md"
        ]
    finally:
        await store.close()


async def test_search_weights_note_title_and_heading_trail(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    titled = note_factory(id=_NOTE_ID, rel_path="projects/datacron.md", title="Projet Datacron")
    body_only = note_factory(id=_OTHER_NOTE_ID, rel_path="projects/other.md", title="Other")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    try:
        await store.upsert_note(
            titled,
            [
                chunk_factory(
                    note=titled,
                    header_path="Statut",
                    content="release published and verified end to end",
                )
            ],
        )
        await store.upsert_note(
            body_only,
            [
                chunk_factory(
                    note=body_only,
                    content="datacron is mentioned once in the body of another note",
                )
            ],
        )

        ranked = [result.chunk.note_rel_path for result in await store.search("datacron")]
        assert ranked == ["projects/datacron.md", "projects/other.md"]
        heading_hits = await store.search("statut")
        assert [result.chunk.note_rel_path for result in heading_hits] == ["projects/datacron.md"]
        # The body never mentions the heading, so the excerpt comes from the context.
        assert heading_hits[0].snippet == "Projet Datacron / **Statut**"
        body_hits = await store.search("release")
        assert body_hits[0].snippet.startswith("**release** published")
        accented = [result.chunk.note_rel_path for result in await store.search("projét")]
        assert accented == ["projects/datacron.md"]
    finally:
        await store.close()


async def _seed_legacy_context_free_index(
    db_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> tuple[str, str]:
    note = note_factory(id=_NOTE_ID, rel_path="legacy/migrated.md", title="Migrated Title")
    chunk = chunk_factory(
        note=note,
        header_path="Section",
        content="legacycontextanchor body",
    )
    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.upsert_note(note, [chunk])
    await store.close()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE chunks_fts;")
        connection.execute(_LEGACY_CHUNKS_FTS_SQL)
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                chunk.chunk_id,
                chunk.note_id,
                chunk.note_rel_path,
                chunk.header_path,
                chunk.section_title,
                chunk.chunk_type.value,
                chunk.content,
                chunk.ordinal,
                chunk.content_hash,
                chunk.token_count,
                chunk.line_start,
                chunk.line_end,
                "[]",
                chunk.lang,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return chunk.chunk_id, chunk.content_hash


def _fts_columns(db_path: Path) -> list[str]:
    connection = sqlite3.connect(db_path)
    try:
        return [str(row[1]) for row in connection.execute("PRAGMA table_info(chunks_fts);")]
    finally:
        connection.close()


async def test_writable_open_migrates_legacy_index_to_context_column(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    chunk_id, content_hash = await _seed_legacy_context_free_index(
        db_path, note_factory, chunk_factory
    )
    assert "context" not in _fts_columns(db_path)

    store = SQLiteFTS5Store()
    await store.open(db_path)
    try:
        assert "context" in _fts_columns(db_path)
        body_hits = await store.search("legacycontextanchor")
        assert [(hit.chunk.chunk_id, hit.chunk.content_hash) for hit in body_hits] == [
            (chunk_id, content_hash)
        ]
        assert [hit.chunk.chunk_id for hit in await store.search("migrated")] == [chunk_id]
        assert [hit.chunk.chunk_id for hit in await store.search("section")] == [chunk_id]
    finally:
        await store.close()

    connection = sqlite3.connect(db_path)
    try:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
        }
        assert "chunks_fts_legacy" not in names
        assert connection.execute("SELECT COUNT(*) FROM chunks_fts;").fetchone()[0] == 1
    finally:
        connection.close()

    # A second open finds the column and leaves the rows untouched.
    again = SQLiteFTS5Store()
    await again.open(db_path)
    try:
        assert [hit.chunk.chunk_id for hit in await again.search("migrated")] == [chunk_id]
    finally:
        await again.close()


async def test_read_only_open_keeps_legacy_index_searchable_without_weights(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    chunk_id, _ = await _seed_legacy_context_free_index(db_path, note_factory, chunk_factory)

    reader = SQLiteFTS5Store()
    await reader.open(db_path, read_only=True)
    try:
        assert [hit.chunk.chunk_id for hit in await reader.search("legacycontextanchor")] == [
            chunk_id
        ]
        assert await reader.search("migrated") == []
        assert [
            hit.chunk.chunk_id
            for hit in await reader.search("legacycontextanchor", folder="legacy")
        ] == [chunk_id]
    finally:
        await reader.close()
    assert "context" not in _fts_columns(db_path)


async def test_search_frontmatter_filter_uses_the_pair_index(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    listed = note_factory(
        id=_NOTE_ID,
        rel_path="items/listed.md",
        frontmatter={"Status": ["open", "Blocked"], "priority": 2, "flag": True},
    )
    scalar = note_factory(
        id=_OTHER_NOTE_ID, rel_path="items/scalar.md", frontmatter={"status": "done"}
    )
    store = SQLiteFTS5Store()
    await store.open(db_path)
    try:
        await store.upsert_note(listed, [chunk_factory(note=listed, content="pairanchor listed")])
        await store.upsert_note(scalar, [chunk_factory(note=scalar, content="pairanchor scalar")])
        assert _paths(await store.search("pairanchor", frontmatter={"STATUS": "blocked"})) == [
            "items/listed.md"
        ]
        assert _paths(await store.search("pairanchor", frontmatter={"status": "DONE"})) == [
            "items/scalar.md"
        ]
        assert _paths(await store.search("pairanchor", frontmatter={"priority": "2"})) == [
            "items/listed.md"
        ]
        assert _paths(await store.search("pairanchor", frontmatter={"flag": "true"})) == [
            "items/listed.md"
        ]
        assert (
            _paths(
                await store.search("pairanchor", frontmatter={"status": "open", "priority": "3"})
            )
            == []
        )
        # Re-indexing a note replaces its pairs; deleting it removes them.
        changed = note_factory(
            id=_NOTE_ID, rel_path="items/listed.md", frontmatter={"status": "closed"}
        )
        await store.upsert_note(changed, [chunk_factory(note=changed, content="pairanchor listed")])
        assert _paths(await store.search("pairanchor", frontmatter={"status": "open"})) == []
        assert _paths(await store.search("pairanchor", frontmatter={"status": "closed"})) == [
            "items/listed.md"
        ]
        await store.delete_note(_OTHER_NOTE_ID)
        assert _paths(await store.search("pairanchor", frontmatter={"status": "done"})) == []
    finally:
        await store.close()

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT note_id, key, value FROM note_frontmatter ORDER BY key, value;"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(_NOTE_ID, "status", "closed")]


async def test_writable_open_rebuilds_frontmatter_pairs_and_read_only_falls_back(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    note = note_factory(id=_NOTE_ID, rel_path="items/one.md", frontmatter={"confidence": "High"})
    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.upsert_note(note, [chunk_factory(note=note, content="backfillanchor")])
    await store.close()

    # Simulate an index created before the pair table existed.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE note_frontmatter;")
        connection.commit()
    finally:
        connection.close()

    reader = SQLiteFTS5Store()
    await reader.open(db_path, read_only=True)
    try:
        # Read-only never migrates; the filter falls back to scanning note metadata.
        assert _paths(
            await reader.search("backfillanchor", frontmatter={"confidence": "high"})
        ) == ["items/one.md"]
        assert await reader.search("backfillanchor", frontmatter={"confidence": "low"}) == []
    finally:
        await reader.close()

    writer = SQLiteFTS5Store()
    await writer.open(db_path)
    try:
        assert _paths(
            await writer.search("backfillanchor", frontmatter={"confidence": "high"})
        ) == ["items/one.md"]
    finally:
        await writer.close()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM note_frontmatter;").fetchone()[0] == 1
    finally:
        connection.close()


async def test_context_never_repeats_the_note_title(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    """The heading trail already starts at the H1 the title was resolved from."""
    note = note_factory(id=_NOTE_ID, rel_path="projects/alpha.md", title="Alpha Note")
    store = SQLiteFTS5Store()
    db_path = _db_path(tmp_path)
    await store.open(db_path)
    try:
        await store.upsert_note(
            note,
            [
                chunk_factory(note=note, header_path="Alpha Note", content="root prose"),
                chunk_factory(
                    note=note,
                    chunk_id=f"{note.id}::alpha-note/deep::0000",
                    header_path="Alpha Note / Deep Heading",
                    content="nested prose",
                    ordinal=1,
                ),
                chunk_factory(
                    note=note,
                    chunk_id=f"{note.id}::other::0000",
                    header_path="Other Trail",
                    content="unrelated prose",
                    ordinal=2,
                ),
            ],
        )
    finally:
        await store.close()

    connection = sqlite3.connect(db_path)
    try:
        stored = sorted(
            str(row[0]) for row in connection.execute("SELECT context FROM chunks_fts;")
        )
    finally:
        connection.close()
    assert stored == [
        "Alpha Note",
        "Alpha Note / Deep Heading",
        "Alpha Note / Other Trail",
    ]


async def test_legacy_migration_also_deduplicates_the_title(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    db_path = _db_path(tmp_path)
    note = note_factory(id=_NOTE_ID, rel_path="projects/alpha.md", title="Alpha Note")
    chunk = chunk_factory(note=note, header_path="Alpha Note / Deep Heading", content="prose here")
    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.upsert_note(note, [chunk])
    await store.close()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE chunks_fts;")
        connection.execute(_LEGACY_CHUNKS_FTS_SQL)
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                chunk.chunk_id,
                chunk.note_id,
                chunk.note_rel_path,
                chunk.header_path,
                chunk.section_title,
                chunk.chunk_type.value,
                chunk.content,
                chunk.ordinal,
                chunk.content_hash,
                chunk.token_count,
                chunk.line_start,
                chunk.line_end,
                "[]",
                chunk.lang,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    migrated = SQLiteFTS5Store()
    await migrated.open(db_path)
    await migrated.close()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT context FROM chunks_fts;").fetchone() == (
            "Alpha Note / Deep Heading",
        )
    finally:
        connection.close()


async def test_markdown_bold_in_the_body_does_not_hide_a_context_match(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    """The public ``**`` marker is also emphasis, so it cannot prove that the body matched."""
    bold = note_factory(id=_NOTE_ID, rel_path="a.md", title="Kerberos ticket renewal")
    plain = note_factory(id=_OTHER_NOTE_ID, rel_path="b.md", title="Kerberos ticket rotation")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    try:
        await store.upsert_note(
            bold,
            [
                chunk_factory(
                    note=bold,
                    header_path="Steps",
                    content="**Rotate** the service account entry every quarter.",
                )
            ],
        )
        await store.upsert_note(
            plain,
            [
                chunk_factory(
                    note=plain,
                    header_path="Steps",
                    content="Rotate the service account entry every quarter.",
                )
            ],
        )
        snippets = {
            result.chunk.note_rel_path: result.snippet for result in await store.search("kerberos")
        }
        assert snippets["a.md"] == "**Kerberos** ticket renewal / Steps"
        assert snippets["b.md"] == "**Kerberos** ticket rotation / Steps"
        # A genuine body match still wins, and it is still decorated for the client.
        body = await store.search("quarter")
        assert body[0].snippet.endswith("every **quarter**.")
        assert body[0].redaction_source is None
    finally:
        await store.close()


async def test_frontmatter_pairs_agree_with_the_json_round_trip(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    """An unquoted YAML timestamp reaches the index as a datetime, not as a string."""
    stamp = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    note = note_factory(id=_NOTE_ID, rel_path="items/dated.md", frontmatter={"created": stamp})
    store = SQLiteFTS5Store()
    db_path = _db_path(tmp_path)
    await store.open(db_path)
    try:
        await store.upsert_note(note, [chunk_factory(note=note, content="stampanchor")])
        iso = stamp.isoformat()
        assert _paths(await store.search("stampanchor", frontmatter={"created": iso})) == [
            "items/dated.md"
        ]
        assert await store.search("stampanchor", frontmatter={"created": str(stamp)}) == []
        # list_notes answers the same filter from the JSON round trip; both must agree.
        listed, total = await store.list_note_paths(
            folder=None, tags=[], frontmatter={"created": iso}, limit=10, offset=0
        )
        assert (listed, total) == (["items/dated.md"], 1)
    finally:
        await store.close()

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT key, value FROM note_frontmatter;").fetchall()
    finally:
        connection.close()
    assert rows == [("created", "2026-05-01t10:00:00+00:00")]


async def test_writable_open_repairs_an_index_a_downgrade_wrote_behind_us(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    """A release predating these tables leaves rows behind; no one-shot marker may hide them."""
    db_path = _db_path(tmp_path)
    kept = note_factory(
        id=_NOTE_ID, rel_path="projects/kept.md", frontmatter={"confidence": "high"}
    )
    store = SQLiteFTS5Store()
    await store.open(db_path)
    await store.upsert_note(kept, [chunk_factory(note=kept, content="downgradeanchor kept")])
    await store.close()

    # Simulate the older release: it knows neither table nor column, so it writes a note
    # row and its chunks without pairs and without context, leaving ours untouched.
    stale = note_factory(
        id=_OTHER_NOTE_ID, rel_path="projects/added.md", frontmatter={"confidence": "low"}
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO notes (note_id, rel_path, title, frontmatter_json, content_hash,"
            " created, updated, indexed_at, fs_mtime, tags_json, sort_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', ?);",
            (
                stale.id,
                stale.rel_path,
                "Added",
                json.dumps({"confidence": "low"}),
                "0" * 64,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                stale.rel_path,
            ),
        )
        connection.execute(
            "INSERT INTO chunks_fts (chunk_id, note_id, note_rel_path, header_path,"
            " section_title, chunk_type, content, ordinal, content_hash, token_count,"
            " line_start, line_end, wikilinks_out_json, lang, context)"
            " VALUES (?, ?, ?, 'Added', NULL, 'narrative', ?, 0, ?, 4, 1, 1, '[]', NULL, NULL);",
            (
                f"{stale.id}::added::0000",
                stale.id,
                stale.rel_path,
                "downgradeanchor added",
                "0" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = SQLiteFTS5Store()
    await reopened.open(db_path)
    try:
        assert _paths(
            await reopened.search("downgradeanchor", frontmatter={"confidence": "low"})
        ) == ["projects/added.md"]
        assert _paths(
            await reopened.search("downgradeanchor", frontmatter={"confidence": "high"})
        ) == ["projects/kept.md"]
        # The repaired chunk is reachable by its title, which needs the context column.
        assert _paths(await reopened.search("added")) == ["projects/added.md"]
    finally:
        await reopened.close()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE context IS NULL OR context = '';"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM index_meta WHERE key = 'frontmatter_pairs_backfilled';"
        ).fetchone() == (0,)
    finally:
        connection.close()


async def test_upsert_clears_the_pairs_of_a_replaced_identity(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    store = SQLiteFTS5Store()
    db_path = _db_path(tmp_path)
    await store.open(db_path)
    try:
        first = note_factory(
            id=_NOTE_ID, rel_path="items/one.md", frontmatter={"confidence": "high"}
        )
        await store.upsert_note(first, [chunk_factory(note=first, content="identityanchor")])
        reidentified = note_factory(
            id=_OTHER_NOTE_ID, rel_path="items/one.md", frontmatter={"confidence": "low"}
        )
        await store.upsert_note(
            reidentified, [chunk_factory(note=reidentified, content="identityanchor")]
        )
        assert (
            _paths(await store.search("identityanchor", frontmatter={"confidence": "high"})) == []
        )
    finally:
        await store.close()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT note_id, value FROM note_frontmatter;").fetchall() == [
            (_OTHER_NOTE_ID, "low")
        ]
    finally:
        connection.close()


async def test_count_matches_by_note_ignores_the_result_limit(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> None:
    many = note_factory(id=_NOTE_ID, rel_path="projects/many.md", title="Many")
    one = note_factory(id=_OTHER_NOTE_ID, rel_path="projects/one.md", title="One")
    store = SQLiteFTS5Store()
    await store.open(_db_path(tmp_path))
    try:
        await store.upsert_note(
            many,
            [
                chunk_factory(
                    note=many,
                    chunk_id=f"{many.id}::s::{index:04d}",
                    header_path=f"Section {index}",
                    content=f"countanchor section {index}",
                    ordinal=index,
                )
                for index in range(12)
            ],
        )
        await store.upsert_note(one, [chunk_factory(note=one, content="countanchor once")])

        assert len(await store.search("countanchor", limit=3)) == 3
        counts = await store.count_matches_by_note("countanchor", [many.id, one.id])
        assert counts == {many.id: 12, one.id: 1}
        # The scope of the search applies to the count too.
        assert await store.count_matches_by_note(
            "countanchor", [many.id, one.id], folder="projects"
        ) == {many.id: 12, one.id: 1}
        assert (
            await store.count_matches_by_note("countanchor", [many.id, one.id], folder="elsewhere")
            == {}
        )
        assert await store.count_matches_by_note("countanchor", []) == {}
        assert await store.count_matches_by_note("", [many.id]) == {}
    finally:
        await store.close()
