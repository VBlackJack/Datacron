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
"""SQLite FTS5 storage for Datacron chunks."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast, final

import aiosqlite

from datacron.core.logger import get_logger
from datacron.core.models import Chunk, ChunkType, IndexStats, Note, SearchResult

__all__ = ["SQLiteFTS5Store"]

_LOGGER = get_logger(__name__)

_FTS5_TERM_PATTERN: Final[re.Pattern[str]] = re.compile(r"\w+", flags=re.UNICODE)
_ULID_SIDECAR_FILENAME: Final[str] = "ulids.json"
_MIGRATED_ULID_SIDECAR_FILENAME: Final[str] = "ulids.json.migrated"

_CREATE_NOTES_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS notes (
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

_CREATE_CHUNKS_FTS_SQL: Final[str] = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
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

_CREATE_ULID_PATHS_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS ulid_paths (
    rel_path TEXT PRIMARY KEY,
    note_id TEXT NOT NULL UNIQUE
);
"""

_INSERT_NOTE_SQL: Final[str] = """
INSERT INTO notes (
    note_id,
    rel_path,
    title,
    frontmatter_json,
    content_hash,
    created,
    updated,
    indexed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(note_id) DO UPDATE SET
    rel_path = excluded.rel_path,
    title = excluded.title,
    frontmatter_json = excluded.frontmatter_json,
    content_hash = excluded.content_hash,
    created = excluded.created,
    updated = excluded.updated,
    indexed_at = excluded.indexed_at;
"""

_INSERT_CHUNK_SQL: Final[str] = """
INSERT INTO chunks_fts (
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

_SELECT_CHUNK_COLUMNS: Final[str] = """
chunk_id,
note_id,
note_rel_path,
header_path,
section_title,
chunk_type,
content,
ordinal,
content_hash,
token_count,
line_start,
line_end,
wikilinks_out_json,
lang
"""

_SEARCH_SQL: Final[str] = """
SELECT
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang,
    bm25(chunks_fts) AS raw_score,
    snippet(
        chunks_fts,
        6,
        '**',
        '**',
        '...',
        32
    ) AS snippet
FROM chunks_fts
WHERE chunks_fts MATCH ?
ORDER BY raw_score
LIMIT ?;
"""

_GET_CHUNK_SQL: Final[str] = """
SELECT
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang
FROM chunks_fts
WHERE chunk_id = ?
LIMIT 1;
"""

_LIST_CHUNKS_FOR_NOTE_SQL: Final[str] = """
SELECT
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang
FROM chunks_fts
WHERE note_id = ?
ORDER BY rowid;
"""

_LIST_CHUNKS_WITH_WIKILINKS_SQL: Final[str] = """
SELECT
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang
FROM chunks_fts
WHERE wikilinks_out_json IS NOT NULL
  AND wikilinks_out_json != '[]'
ORDER BY rowid;
"""

_LIST_INDEXED_NOTES_SQL: Final[str] = """
SELECT rel_path, note_id, content_hash
FROM notes
ORDER BY indexed_at ASC;
"""

_ITER_ALL_CHUNKS_SQL: Final[str] = """
SELECT
    chunk_id,
    note_id,
    note_rel_path,
    header_path,
    section_title,
    chunk_type,
    content,
    ordinal,
    content_hash,
    token_count,
    line_start,
    line_end,
    wikilinks_out_json,
    lang
FROM chunks_fts
ORDER BY rowid;
"""


@final
class SQLiteFTS5Store:
    """Persistent SQLite-backed implementation of the FTS5Store contract."""

    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._db_path: Path | None = None

    async def open(self, db_path: Path) -> None:
        """Open or create the SQLite database at ``db_path``."""
        if self._conn is not None:
            return

        resolved_path = db_path.expanduser().resolve()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        connection = await aiosqlite.connect(resolved_path)
        connection.row_factory = sqlite3.Row
        self._conn = connection
        self._db_path = resolved_path

        try:
            await self._ensure_schema(connection)
            await self._migrate_ulid_sidecar(connection, resolved_path)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the database connection if one is open."""
        connection = self._conn
        self._conn = None
        if connection is not None:
            await connection.close()

    async def upsert_note(self, note: Note, chunks: list[Chunk]) -> None:
        """Insert or replace a note and all of its chunks in one transaction."""
        connection = self._require_connection()
        _validate_chunks_belong_to_note(note, chunks)

        indexed_at = datetime.now(tz=UTC).isoformat()
        await connection.execute("BEGIN")
        try:
            await connection.execute(
                "DELETE FROM chunks_fts WHERE note_id = ? OR note_rel_path = ?",
                (note.id, note.rel_path),
            )
            await connection.execute(
                "DELETE FROM notes WHERE rel_path = ? AND note_id != ?",
                (note.rel_path, note.id),
            )
            await connection.execute(_INSERT_NOTE_SQL, _note_row(note, indexed_at))
            await connection.execute(
                "DELETE FROM ulid_paths WHERE rel_path = ? OR note_id = ?",
                (note.rel_path, note.id),
            )
            await connection.execute(
                "INSERT INTO ulid_paths(rel_path, note_id) VALUES (?, ?)",
                (note.rel_path, note.id),
            )
            await connection.executemany(_INSERT_CHUNK_SQL, [_chunk_row(chunk) for chunk in chunks])
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()

    async def delete_note(self, note_id: str) -> None:
        """Remove a note, its chunks, and its path mapping if present."""
        connection = self._require_connection()

        await connection.execute("BEGIN")
        try:
            await connection.execute("DELETE FROM chunks_fts WHERE note_id = ?", (note_id,))
            await connection.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
            await connection.execute("DELETE FROM ulid_paths WHERE note_id = ?", (note_id,))
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Run BM25 search over chunk content."""
        if limit <= 0 or not query.strip():
            return []

        connection = self._require_connection()
        terms = _fts5_terms(query)
        if not terms:
            return []
        and_query = _join_fts5_terms(terms, operator=" ")
        rows = await _fetch_search_rows(connection, and_query, limit)
        if not rows and len(terms) > 1:
            or_query = _join_fts5_terms(terms, operator=" OR ")
            rows = await _fetch_search_rows(connection, or_query, limit)

        return [
            SearchResult(
                chunk=_chunk_from_row(row),
                score=-float(row["raw_score"]),
                snippet=str(row["snippet"]),
            )
            for row in rows
        ]

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Return one chunk by ID, or ``None`` if absent."""
        connection = self._require_connection()
        async with connection.execute(_GET_CHUNK_SQL, (chunk_id,)) as cursor:
            row = await cursor.fetchone()
        return _chunk_from_row(row) if row is not None else None

    async def list_chunks_for_note(self, note_id: str) -> list[Chunk]:
        """Return chunks for ``note_id`` in insertion/document order."""
        connection = self._require_connection()
        async with connection.execute(_LIST_CHUNKS_FOR_NOTE_SQL, (note_id,)) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        return [_chunk_from_row(row) for row in rows]

    async def list_chunks_with_wikilinks(self) -> list[Chunk]:
        """Return all chunks whose indexed wikilink list is non-empty."""
        connection = self._require_connection()
        async with connection.execute(_LIST_CHUNKS_WITH_WIKILINKS_SQL) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        return [_chunk_from_row(row) for row in rows]

    async def list_indexed_notes(self) -> dict[str, tuple[str, str]]:
        """Return ``rel_path -> (note_id, content_hash)`` for the current index."""
        connection = self._require_connection()
        async with connection.execute(_LIST_INDEXED_NOTES_SQL) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        return {
            str(row["rel_path"]): (str(row["note_id"]), str(row["content_hash"])) for row in rows
        }

    async def iter_all_chunks(self) -> AsyncIterator[Chunk]:
        """Stream all indexed chunks in insertion/document order."""
        connection = self._require_connection()
        async with connection.execute(_ITER_ALL_CHUNKS_SQL) as cursor:
            async for row in cursor:
                yield _chunk_from_row(row)

    async def stats(self) -> IndexStats:
        """Return aggregate index statistics."""
        connection = self._require_connection()
        db_path = self._require_db_path()

        note_count = await _fetch_int(connection, "SELECT COUNT(*) FROM notes;")
        chunk_count = await _fetch_int(connection, "SELECT COUNT(*) FROM chunks_fts;")
        last_indexed_raw = await _fetch_optional_str(
            connection,
            "SELECT MAX(indexed_at) FROM notes;",
        )
        last_indexed_at = (
            datetime.fromisoformat(last_indexed_raw) if last_indexed_raw is not None else None
        )
        db_size_bytes = db_path.stat().st_size if db_path.exists() else 0

        return IndexStats(
            note_count=note_count,
            chunk_count=chunk_count,
            last_indexed_at=last_indexed_at,
            db_size_bytes=db_size_bytes,
            db_path=db_path,
        )

    async def _ensure_schema(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(_CREATE_NOTES_SQL)
        await connection.execute(_CREATE_CHUNKS_FTS_SQL)
        await connection.execute(_CREATE_ULID_PATHS_SQL)
        await connection.commit()

    async def _migrate_ulid_sidecar(
        self,
        connection: aiosqlite.Connection,
        db_path: Path,
    ) -> None:
        sidecar_dir = db_path.parent.parent
        ulids_path = sidecar_dir / _ULID_SIDECAR_FILENAME
        migrated_path = sidecar_dir / _MIGRATED_ULID_SIDECAR_FILENAME

        source_path = ulids_path if ulids_path.exists() else migrated_path
        if not source_path.exists():
            return

        mappings = await asyncio.to_thread(_read_ulid_mappings, source_path)
        rows = list(mappings.items())
        await connection.executemany(
            "INSERT OR IGNORE INTO ulid_paths(rel_path, note_id) VALUES (?, ?)",
            rows,
        )
        await connection.commit()
        if not ulids_path.exists():
            await asyncio.to_thread(_write_ulid_mappings, ulids_path, mappings)
        if not migrated_path.exists():
            await asyncio.to_thread(_write_ulid_mappings, migrated_path, mappings)
        _LOGGER.info("Imported %s ULID mappings from JsonIdStore", len(rows))

    def _require_connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteFTS5Store is not open.")
        return self._conn

    def _require_db_path(self) -> Path:
        if self._db_path is None:
            raise RuntimeError("SQLiteFTS5Store is not open.")
        return self._db_path


async def _fetch_search_rows(
    connection: aiosqlite.Connection,
    fts_query: str,
    limit: int,
) -> list[sqlite3.Row]:
    async with connection.execute(_SEARCH_SQL, (fts_query, limit)) as cursor:
        return cast("list[sqlite3.Row]", await cursor.fetchall())


def _fts5_terms(query: str) -> list[str]:
    return _FTS5_TERM_PATTERN.findall(query)


def _join_fts5_terms(terms: list[str], *, operator: str) -> str:
    return operator.join(_quote_fts5_term(term) for term in terms)


def _quote_fts5_term(term: str) -> str:
    return f'"{term.replace(chr(34), chr(34) * 2)}"'


def _validate_chunks_belong_to_note(note: Note, chunks: list[Chunk]) -> None:
    for chunk in chunks:
        if chunk.note_id != note.id:
            raise ValueError(
                f"Chunk {chunk.chunk_id!r} belongs to note {chunk.note_id!r}, not {note.id!r}."
            )


def _note_row(note: Note, indexed_at: str) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        note.id,
        note.rel_path,
        note.title,
        json.dumps(note.frontmatter, sort_keys=True, ensure_ascii=False, default=_json_default),
        note.content_hash,
        note.created.isoformat(),
        note.updated.isoformat(),
        indexed_at,
    )


def _chunk_row(
    chunk: Chunk,
) -> tuple[str, str, str, str, str | None, str, str, int, str, int, int, int, str, str | None]:
    return (
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
        json.dumps(chunk.wikilinks_out, ensure_ascii=False),
        chunk.lang,
    )


def _chunk_from_row(row: sqlite3.Row) -> Chunk:
    return Chunk(
        chunk_id=str(row["chunk_id"]),
        note_id=str(row["note_id"]),
        note_rel_path=str(row["note_rel_path"]),
        header_path=str(row["header_path"]),
        section_title=_optional_str(row["section_title"]),
        chunk_type=ChunkType(str(row["chunk_type"])),
        content=str(row["content"]),
        ordinal=int(row["ordinal"]),
        content_hash=str(row["content_hash"]),
        token_count=int(row["token_count"]),
        line_start=int(row["line_start"]),
        line_end=int(row["line_end"]),
        wikilinks_out=_wikilinks_from_json(row["wikilinks_out_json"]),
        lang=_optional_str(row["lang"]),
    )


def _wikilinks_from_json(value: Any) -> list[str]:
    if value is None:
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("Stored wikilinks_out_json is not a JSON array.")
    return [str(item) for item in parsed]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _read_ulid_mappings(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"ULID sidecar {path} is not a JSON object (found {type(data).__name__}).")
    return {str(rel_path): str(note_id) for rel_path, note_id in data.items()}


def _write_ulid_mappings(path: Path, mappings: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(mappings, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(serialized + "\n", encoding="utf-8")


async def _fetch_int(connection: aiosqlite.Connection, sql: str) -> int:
    async with connection.execute(sql) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


async def _fetch_optional_str(connection: aiosqlite.Connection, sql: str) -> str | None:
    async with connection.execute(sql) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


# Structural conformance check for mypy.
from datacron.core.protocols import FTS5Store as _FTS5StoreProtocol  # noqa: E402


def _conformance_check(_: _FTS5StoreProtocol) -> None:
    """Mypy structural conformance: SQLiteFTS5Store must satisfy FTS5Store Protocol."""


_conformance_check(SQLiteFTS5Store())
