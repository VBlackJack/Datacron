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
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast, final

import aiosqlite

from datacron.core.logger import get_logger
from datacron.core.models import Chunk, ChunkType, IndexStats, Note, SearchResult
from datacron.core.query_expansion import expand_terms, normalize_term_map
from datacron.core.temporal import TemporalMeta

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
    indexed_at TEXT NOT NULL,
    fs_mtime INTEGER
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

_CREATE_INDEX_META_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    indexed_at,
    fs_mtime
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(note_id) DO UPDATE SET
    rel_path = excluded.rel_path,
    title = excluded.title,
    frontmatter_json = excluded.frontmatter_json,
    content_hash = excluded.content_hash,
    created = excluded.created,
    updated = excluded.updated,
    indexed_at = excluded.indexed_at,
    fs_mtime = excluded.fs_mtime;
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

_LIST_INDEXED_NOTES_WITH_MTIME_SQL: Final[str] = """
SELECT rel_path, note_id, content_hash, fs_mtime
FROM notes
ORDER BY indexed_at ASC;
"""

_LIST_TEMPORAL_METADATA_SQL: Final[str] = """
SELECT note_id, frontmatter_json
FROM notes
ORDER BY indexed_at ASC;
"""

_RECORD_MTIME_SQL: Final[str] = "UPDATE notes SET fs_mtime = ? WHERE note_id = ?;"

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

    def __init__(self, term_map: Mapping[str, Sequence[str]] | None = None) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._read_only = False
        self._term_map = normalize_term_map(term_map or {})

    async def open(
        self,
        db_path: Path,
        *,
        read_only: bool = False,
        sidecar_writeback: bool = True,
    ) -> None:
        """Open SQLite normally or as an immutable certified read-only index."""
        if self._conn is not None:
            return

        resolved_path = db_path.expanduser().resolve()
        if read_only:
            if not resolved_path.is_file():
                raise FileNotFoundError(
                    f"certified read-only mode requires a prebuilt index: {resolved_path}"
                )
            uri = f"{resolved_path.as_uri()}?mode=ro&immutable=1"
            connection = await aiosqlite.connect(uri, uri=True)
        else:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(resolved_path)

        connection.row_factory = sqlite3.Row
        self._conn = connection
        self._db_path = resolved_path
        self._read_only = read_only

        try:
            if read_only:
                await connection.execute("PRAGMA query_only = ON;")
            else:
                await self._ensure_schema(connection)
                await self._migrate_ulid_sidecar(
                    connection,
                    resolved_path,
                    writeback=sidecar_writeback,
                )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the database connection if one is open."""
        connection = self._conn
        self._conn = None
        self._read_only = False
        if connection is not None:
            await connection.close()

    async def upsert_note(
        self, note: Note, chunks: list[Chunk], fs_mtime_ns: int | None = None
    ) -> None:
        """Insert or replace a note and all of its chunks in one transaction.

        ``fs_mtime_ns`` is the note file's ``st_mtime_ns`` at index time. It is
        stored so the read-repair can skip the read+hash of unchanged notes by
        comparing the filesystem mtime first (the ``content_hash`` stays the
        authority on any mtime change).
        """
        connection = self._require_connection()
        self._require_writable()
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
            await connection.execute(_INSERT_NOTE_SQL, _note_row(note, indexed_at, fs_mtime_ns))
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

    async def record_mtime(self, note_id: str, fs_mtime_ns: int) -> None:
        """Update the stored filesystem mtime for ``note_id`` without re-chunking.

        Used by the read-repair when a note's mtime moved but its
        ``content_hash`` is unchanged: refreshing the stored mtime lets the next
        repair skip the read+hash. Without it, a touched-but-unchanged note
        would be re-read on every search forever.
        """
        connection = self._require_connection()
        self._require_writable()
        await connection.execute(_RECORD_MTIME_SQL, (fs_mtime_ns, note_id))
        await connection.commit()

    async def delete_note(self, note_id: str) -> None:
        """Remove a note, its chunks, and its path mapping if present."""
        connection = self._require_connection()
        self._require_writable()

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
        if self._term_map:
            groups = expand_terms(terms, self._term_map)
            and_query = _join_fts5_groups(groups)
            fallback_terms = _flatten_fts5_groups(groups)
        else:
            and_query = _join_fts5_terms(terms, operator=" ")
            fallback_terms = terms
        rows = await _fetch_search_rows(connection, and_query, limit)
        if len(rows) < limit and len(terms) > 1:
            or_query = _join_fts5_terms(fallback_terms, operator=" OR ")
            seen_chunk_ids = {str(row["chunk_id"]) for row in rows}
            for row in await _fetch_search_rows(connection, or_query, limit):
                chunk_id = str(row["chunk_id"])
                if chunk_id in seen_chunk_ids:
                    continue
                rows.append(row)
                seen_chunk_ids.add(chunk_id)
                if len(rows) >= limit:
                    break

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

    async def list_indexed_notes_with_mtime(self) -> dict[str, tuple[str, str, int | None]]:
        """Return ``rel_path -> (note_id, content_hash, fs_mtime_ns)`` for the index.

        ``fs_mtime_ns`` is ``None`` for rows indexed before the mtime column
        existed; callers must treat ``None`` as "always re-read" (never skip).
        """
        connection = self._require_connection()
        async with connection.execute(_LIST_INDEXED_NOTES_WITH_MTIME_SQL) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        return {
            str(row["rel_path"]): (
                str(row["note_id"]),
                str(row["content_hash"]),
                None if row["fs_mtime"] is None else int(row["fs_mtime"]),
            )
            for row in rows
        }

    async def list_temporal_metadata(self) -> dict[str, TemporalMeta]:
        """Return explicit retrieval lifecycle metadata keyed by note_id."""
        connection = self._require_connection()
        async with connection.execute(_LIST_TEMPORAL_METADATA_SQL) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        return {
            str(row["note_id"]): _temporal_meta_from_frontmatter(row["frontmatter_json"])
            for row in rows
        }

    async def get_generation(self) -> int:
        """Return zero for legacy indexes, otherwise the completed generation."""
        connection = self._require_connection()
        try:
            async with connection.execute(
                "SELECT value FROM index_meta WHERE key = 'generation';"
            ) as cursor:
                row = await cursor.fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row is not None else 0

    async def set_generation(self, generation: int) -> None:
        """Set the generation seed on a writable offline index."""
        if generation < 0:
            raise ValueError("generation must be non-negative")
        connection = self._require_connection()
        self._require_writable()
        await connection.execute(
            "INSERT INTO index_meta(key, value) VALUES ('generation', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (str(generation),),
        )
        await connection.commit()

    async def increment_generation(self) -> int:
        """Advance generation after one complete reconcile operation."""
        generation = await self.get_generation() + 1
        await self.set_generation(generation)
        return generation

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
        generation = await self.get_generation()

        return IndexStats(
            note_count=note_count,
            chunk_count=chunk_count,
            generation=generation,
            last_indexed_at=last_indexed_at,
            db_size_bytes=db_size_bytes,
            db_path=db_path,
        )

    async def _ensure_schema(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(_CREATE_NOTES_SQL)
        await connection.execute(_CREATE_CHUNKS_FTS_SQL)
        await connection.execute(_CREATE_ULID_PATHS_SQL)
        await connection.execute(_CREATE_INDEX_META_SQL)
        await connection.execute(
            "INSERT OR IGNORE INTO index_meta(key, value) VALUES ('generation', '0');"
        )
        await self._migrate_notes_columns(connection)
        await connection.commit()

    def _require_writable(self) -> None:
        if self._read_only:
            raise PermissionError("immutable read-only index refuses mutation")

    async def _migrate_notes_columns(self, connection: aiosqlite.Connection) -> None:
        """Add columns introduced after the initial ``notes`` schema (idempotent).

        Databases created before ``fs_mtime`` existed keep working: the column
        is added with NULL values, which callers treat as "always re-read".
        """
        async with connection.execute("PRAGMA table_info(notes);") as cursor:
            rows = await cursor.fetchall()
        columns = {str(row[1]) for row in rows}
        if "fs_mtime" not in columns:
            await connection.execute("ALTER TABLE notes ADD COLUMN fs_mtime INTEGER;")

    async def _migrate_ulid_sidecar(
        self,
        connection: aiosqlite.Connection,
        db_path: Path,
        *,
        writeback: bool,
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
        if writeback and not ulids_path.exists():
            await asyncio.to_thread(_write_ulid_mappings, ulids_path, mappings)
        if writeback and not migrated_path.exists():
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


def _join_fts5_groups(groups: list[list[str]]) -> str:
    query_parts: list[str] = []
    for group in groups:
        if len(group) == 1:
            query_parts.append(_quote_fts5_term(group[0]))
            continue
        query_parts.append(f"({_join_fts5_terms(group, operator=' OR ')})")
    return " AND ".join(query_parts)


def _flatten_fts5_groups(groups: list[list[str]]) -> list[str]:
    flattened: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for term in group:
            key = term.lower()
            if key in seen:
                continue
            flattened.append(term)
            seen.add(key)
    return flattened


def _quote_fts5_term(term: str) -> str:
    return f'"{term.replace(chr(34), chr(34) * 2)}"'


def _validate_chunks_belong_to_note(note: Note, chunks: list[Chunk]) -> None:
    for chunk in chunks:
        if chunk.note_id != note.id:
            raise ValueError(
                f"Chunk {chunk.chunk_id!r} belongs to note {chunk.note_id!r}, not {note.id!r}."
            )


def _note_row(
    note: Note, indexed_at: str, fs_mtime_ns: int | None
) -> tuple[str, str, str, str, str, str, str, str, int | None]:
    return (
        note.id,
        note.rel_path,
        note.title,
        json.dumps(note.frontmatter, sort_keys=True, ensure_ascii=False, default=_json_default),
        note.content_hash,
        note.created.isoformat(),
        note.updated.isoformat(),
        indexed_at,
        fs_mtime_ns,
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


def _temporal_meta_from_frontmatter(value: Any) -> TemporalMeta:
    if value is None:
        return TemporalMeta(confidence=None, supersedes=[])
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("Stored frontmatter_json is not a JSON object.")
    return TemporalMeta(
        confidence=_optional_str(parsed.get("confidence")),
        supersedes=_string_list(parsed.get("supersedes")),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


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
