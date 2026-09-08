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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, cast, final

import aiosqlite

from datacron.core.config import (
    CHUNK_CONTEXT_SEPARATOR,
    SEARCH_CONTENT_WEIGHT,
    SEARCH_CONTEXT_WEIGHT,
)
from datacron.core.frontmatter import (
    coerce_string_list,
    extract_tags,
    matches_frontmatter_filter,
)
from datacron.core.logger import get_logger
from datacron.core.models import Chunk, ChunkType, IndexStats, Note, SearchResult
from datacron.core.paths import read_ulid_mappings
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
    fs_mtime INTEGER,
    tags_json TEXT,
    sort_key TEXT
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
    context,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Column positions inside ``chunks_fts``; bm25() weights are positional.
_FTS_CONTENT_COLUMN: Final[int] = 6
_FTS_CONTEXT_COLUMN: Final[int] = 14
_FTS_COLUMN_COUNT: Final[int] = 15
_LEGACY_CHUNKS_TABLE: Final[str] = "chunks_fts_legacy"
_FTS_CONTEXT_COLUMN_NAME: Final[str] = "context"

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
    fs_mtime,
    tags_json,
    sort_key
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(note_id) DO UPDATE SET
    rel_path = excluded.rel_path,
    title = excluded.title,
    frontmatter_json = excluded.frontmatter_json,
    content_hash = excluded.content_hash,
    created = excluded.created,
    updated = excluded.updated,
    indexed_at = excluded.indexed_at,
    fs_mtime = excluded.fs_mtime,
    tags_json = excluded.tags_json,
    sort_key = excluded.sort_key;
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
    lang,
    context
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

_SEARCH_COLUMNS_SQL: Final[str] = """
    chunks_fts.chunk_id,
    chunks_fts.note_id,
    chunks_fts.note_rel_path,
    chunks_fts.header_path,
    chunks_fts.section_title,
    chunks_fts.chunk_type,
    chunks_fts.content,
    chunks_fts.ordinal,
    chunks_fts.content_hash,
    chunks_fts.token_count,
    chunks_fts.line_start,
    chunks_fts.line_end,
    chunks_fts.wikilinks_out_json,
    chunks_fts.lang,
"""

_SEARCH_SNIPPET_SQL: Final[str] = f"""
    snippet(
        chunks_fts,
        {_FTS_CONTENT_COLUMN},
        '**',
        '**',
        '...',
        32
    ) AS snippet
"""

# Scope filters mirror ``_NOTE_PATH_FILTER_SQL``: same folder prefix rule, same
# required-tags rule, plus an optional explicit note identity allowlist.
_SEARCH_SCOPE_SQL: Final[str] = """
JOIN notes ON notes.note_id = chunks_fts.note_id
WHERE chunks_fts MATCH ?
  AND (? IS NULL OR substr(notes.rel_path, 1, length(?) + 1) = ? || '/')
  AND NOT EXISTS (
      SELECT 1
      FROM json_each(?) AS required
      WHERE NOT EXISTS (
          SELECT 1
          FROM json_each(notes.tags_json) AS actual
          WHERE actual.value = required.value
      )
  )
  AND (? IS NULL OR notes.note_id IN (SELECT value FROM json_each(?)))
"""

_LEGACY_SCORE_SQL: Final[str] = "bm25(chunks_fts)"


def _weighted_score_sql() -> str:
    weights = ["0"] * _FTS_COLUMN_COUNT
    weights[_FTS_CONTENT_COLUMN] = repr(float(SEARCH_CONTENT_WEIGHT))
    weights[_FTS_CONTEXT_COLUMN] = repr(float(SEARCH_CONTEXT_WEIGHT))
    return f"bm25(chunks_fts, {', '.join(weights)})"


def _search_sql(*, weighted: bool, scoped: bool) -> str:
    score = _weighted_score_sql() if weighted else _LEGACY_SCORE_SQL
    scope = _SEARCH_SCOPE_SQL if scoped else "WHERE chunks_fts MATCH ?"
    return (
        f"SELECT {_SEARCH_COLUMNS_SQL} {score} AS raw_score, {_SEARCH_SNIPPET_SQL}"
        f"FROM chunks_fts\n{scope}\nORDER BY raw_score\nLIMIT ?;"
    )


_MIGRATE_CHUNK_CONTEXT_SQL: Final[str] = """
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
    lang,
    context
)
SELECT
    legacy.chunk_id,
    legacy.note_id,
    legacy.note_rel_path,
    legacy.header_path,
    legacy.section_title,
    legacy.chunk_type,
    legacy.content,
    legacy.ordinal,
    legacy.content_hash,
    legacy.token_count,
    legacy.line_start,
    legacy.line_end,
    legacy.wikilinks_out_json,
    legacy.lang,
    CASE
        WHEN legacy.header_path = '' THEN COALESCE(notes.title, '')
        ELSE COALESCE(notes.title, '') || ? || legacy.header_path
    END
FROM chunks_fts_legacy AS legacy
LEFT JOIN notes ON notes.note_id = legacy.note_id
ORDER BY legacy.rowid;
"""

_LIST_NOTE_FRONTMATTER_SQL: Final[str] = """
SELECT note_id, frontmatter_json
FROM notes
ORDER BY sort_key COLLATE BINARY;
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

_GET_NOTE_REL_PATH_SQL: Final[str] = """
SELECT rel_path
FROM ulid_paths
WHERE note_id = ?
LIMIT 1;
"""

_GET_NOTE_ID_SQL: Final[str] = """
SELECT note_id
FROM ulid_paths
WHERE rel_path = ?
LIMIT 1;
"""

_NOTE_PATH_FILTER_SQL: Final[str] = """
FROM notes
WHERE tags_json IS NOT NULL
  AND sort_key IS NOT NULL
  AND (? IS NULL OR substr(rel_path, 1, length(?) + 1) = ? || '/')
  AND NOT EXISTS (
      SELECT 1
      FROM json_each(?) AS required
      WHERE NOT EXISTS (
          SELECT 1
          FROM json_each(notes.tags_json) AS actual
          WHERE actual.value = required.value
      )
  )
"""

_COUNT_NOTE_PATHS_SQL: Final[str] = f"SELECT COUNT(*) {_NOTE_PATH_FILTER_SQL};"

_LIST_NOTE_PATHS_SQL: Final[str] = f"""
SELECT rel_path
{_NOTE_PATH_FILTER_SQL}
ORDER BY sort_key COLLATE BINARY
LIMIT ? OFFSET ?;
"""

_LIST_FILTERABLE_NOTE_PATHS_SQL: Final[str] = f"""
SELECT rel_path, frontmatter_json
{_NOTE_PATH_FILTER_SQL}
ORDER BY sort_key COLLATE BINARY;
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


_EMPTY_JSON_LIST: Final[str] = "[]"


@dataclass(frozen=True)
class _SearchScope:
    """Bound parameters narrowing one search to a subset of indexed notes."""

    folder: str | None
    tags_json: str
    note_ids_json: str | None


async def _has_context_column(connection: aiosqlite.Connection) -> bool:
    async with connection.execute("PRAGMA table_info(chunks_fts);") as cursor:
        rows = await cursor.fetchall()
    return any(str(row[1]) == _FTS_CONTEXT_COLUMN_NAME for row in rows)


@final
class SQLiteFTS5Store:
    """Persistent SQLite-backed implementation of the FTS5Store contract."""

    def __init__(self, term_map: Mapping[str, Sequence[str]] | None = None) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._read_only = False
        self._term_map = normalize_term_map(term_map or {})
        self._temporal_metadata_cache: tuple[int, dict[str, TemporalMeta]] | None = None
        # ``None`` until the open index has been inspected for the ``context`` column.
        self._context_indexed: bool | None = None

    async def open(
        self,
        db_path: Path,
        *,
        read_only: bool = False,
        sidecar_writeback: bool = True,
    ) -> None:
        """Open SQLite normally or in certified read-only mode."""
        if self._conn is not None:
            return

        resolved_path = db_path.expanduser().resolve()
        if read_only:
            if not resolved_path.is_file():
                raise FileNotFoundError(
                    f"certified read-only mode requires a prebuilt index: {resolved_path}"
                )
            uri = f"{resolved_path.as_uri()}?mode=ro"
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
        self._temporal_metadata_cache = None
        self._context_indexed = None
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
            await connection.executemany(
                _INSERT_CHUNK_SQL,
                [
                    _chunk_row(chunk, _chunk_context(note.title, chunk.header_path))
                    for chunk in chunks
                ],
            )
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

    async def search(
        self,
        query: str,
        limit: int = 20,
        *,
        folder: str | None = None,
        tags: Sequence[str] | None = None,
        frontmatter: Mapping[str, str] | None = None,
    ) -> list[SearchResult]:
        """Run BM25 search over chunk content and heading context.

        ``folder``, ``tags`` and ``frontmatter`` narrow the searched notes with
        the same semantics as :meth:`list_note_paths`: folder prefix, every
        required tag present, and case-insensitive top-level frontmatter
        matches. Chunk text receives the content weight and the note title plus
        heading trail receive the context weight; a legacy index that predates
        the context column falls back to unweighted scoring until it is rebuilt.
        """
        if limit <= 0 or not query.strip():
            return []

        connection = self._require_connection()
        terms = _fts5_terms(query)
        if not terms:
            return []
        scope = await self._search_scope(
            connection, folder=folder, tags=tags, frontmatter=frontmatter
        )
        if scope is not None and scope.note_ids_json == _EMPTY_JSON_LIST:
            return []
        weighted = await self._is_context_indexed(connection)
        if self._term_map:
            groups = expand_terms(terms, self._term_map)
            and_query = _join_fts5_groups(groups)
            fallback_terms = _flatten_fts5_groups(groups)
        else:
            and_query = _join_fts5_terms(terms, operator=" ")
            fallback_terms = terms
        and_rows = await _fetch_search_rows(
            connection, and_query, limit, weighted=weighted, scope=scope
        )
        ranked_rows = [(row, 0) for row in and_rows]
        if len(ranked_rows) < limit and len(terms) > 1:
            or_query = _join_fts5_terms(fallback_terms, operator=" OR ")
            seen_chunk_ids = {str(row["chunk_id"]) for row in and_rows}
            for row in await _fetch_search_rows(
                connection, or_query, limit, weighted=weighted, scope=scope
            ):
                chunk_id = str(row["chunk_id"])
                if chunk_id in seen_chunk_ids:
                    continue
                ranked_rows.append((row, 1))
                seen_chunk_ids.add(chunk_id)
                if len(ranked_rows) >= limit:
                    break

        return [
            SearchResult(
                chunk=_chunk_from_row(row),
                score=-float(row["raw_score"]),
                snippet=str(row["snippet"]),
                tier=tier,
            )
            for row, tier in ranked_rows
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

    async def get_note_rel_path(self, note_id: str) -> str | None:
        """Return the indexed vault-relative path for ``note_id``, if present."""
        connection = self._require_connection()
        async with connection.execute(_GET_NOTE_REL_PATH_SQL, (note_id,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def get_note_id(self, rel_path: str) -> str | None:
        """Return the indexed note ID for ``rel_path``, if present."""
        connection = self._require_connection()
        async with connection.execute(_GET_NOTE_ID_SQL, (rel_path,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def list_note_paths(
        self,
        *,
        folder: str | None,
        tags: list[str],
        frontmatter: dict[str, str] | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[str], int]:
        """Return one filtered path page in filesystem discovery order."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        connection = self._require_connection()
        async with connection.execute("PRAGMA table_info(notes);") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        if not {"tags_json", "sort_key"} <= columns:
            raise RuntimeError("Indexed note discovery columns are unavailable.")
        normalized_folder = folder.rstrip("/") if folder else None
        required_tags = sorted({tag.strip().lower() for tag in tags if tag.strip()})
        parameters = (
            normalized_folder,
            normalized_folder,
            normalized_folder,
            json.dumps(required_tags, ensure_ascii=False),
        )
        if frontmatter:
            async with connection.execute(
                _LIST_FILTERABLE_NOTE_PATHS_SQL,
                parameters,
            ) as cursor:
                rows = await cursor.fetchall()
            matching_paths: list[str] = []
            for row in rows:
                metadata_raw = json.loads(str(row["frontmatter_json"]))
                metadata = (
                    cast("dict[str, object]", metadata_raw)
                    if isinstance(metadata_raw, dict)
                    else {}
                )
                if matches_frontmatter_filter(metadata, frontmatter):
                    matching_paths.append(str(row["rel_path"]))
            total = len(matching_paths)
            start = min(offset, total)
            return matching_paths[start : start + limit], total

        async with connection.execute(_COUNT_NOTE_PATHS_SQL, parameters) as cursor:
            count_row = await cursor.fetchone()
        total = 0 if count_row is None else int(count_row[0])
        start = min(offset, total)
        async with connection.execute(
            _LIST_NOTE_PATHS_SQL,
            (*parameters, limit, start),
        ) as cursor:
            rows = await cursor.fetchall()
        return [str(row[0]) for row in rows], total

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
        generation = await self.get_generation()
        cached = self._temporal_metadata_cache
        if generation != 0 and cached is not None and cached[0] == generation:
            return cached[1]

        connection = self._require_connection()
        async with connection.execute(_LIST_TEMPORAL_METADATA_SQL) as cursor:
            rows = cast("list[sqlite3.Row]", await cursor.fetchall())
        metadata = {
            str(row["note_id"]): _temporal_meta_from_frontmatter(row["frontmatter_json"])
            for row in rows
        }
        if generation != 0:
            self._temporal_metadata_cache = (generation, metadata)
        return metadata

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
        await self._migrate_chunk_context(connection)
        await connection.commit()
        self._context_indexed = True

    async def _migrate_chunk_context(self, connection: aiosqlite.Connection) -> None:
        """Add the indexed ``context`` column to a legacy ``chunks_fts`` table.

        FTS5 virtual tables cannot be altered in place, so the legacy table is
        renamed, recreated with the new column, and refilled from its own rows
        joined with the indexed note titles. Chunk identities, hashes and
        ordinals are copied verbatim; only the searchable context is new. The
        copy runs inside the caller's transaction and is idempotent.
        """
        if await _has_context_column(connection):
            return
        _LOGGER.info("Migrating chunks_fts to the context-indexed schema")
        await connection.execute(f"ALTER TABLE chunks_fts RENAME TO {_LEGACY_CHUNKS_TABLE};")
        await connection.execute(_CREATE_CHUNKS_FTS_SQL)
        await connection.execute(_MIGRATE_CHUNK_CONTEXT_SQL, (CHUNK_CONTEXT_SEPARATOR,))
        await connection.execute(f"DROP TABLE {_LEGACY_CHUNKS_TABLE};")

    async def _is_context_indexed(self, connection: aiosqlite.Connection) -> bool:
        if self._context_indexed is None:
            self._context_indexed = await _has_context_column(connection)
            if not self._context_indexed:
                _LOGGER.warning(
                    "Index predates the context column; scoring stays unweighted until rebuilt"
                )
        return self._context_indexed

    async def _search_scope(
        self,
        connection: aiosqlite.Connection,
        *,
        folder: str | None,
        tags: Sequence[str] | None,
        frontmatter: Mapping[str, str] | None,
    ) -> _SearchScope | None:
        """Translate list-style filters into bound search parameters, or ``None``."""
        normalized_folder = folder.rstrip("/") if folder else None
        required_tags = sorted({tag.strip().lower() for tag in (tags or []) if tag.strip()})
        if not normalized_folder and not required_tags and not frontmatter:
            return None
        note_ids_json: str | None = None
        if frontmatter:
            async with connection.execute(_LIST_NOTE_FRONTMATTER_SQL) as cursor:
                rows = await cursor.fetchall()
            allowed: list[str] = []
            for row in rows:
                metadata_raw = json.loads(str(row["frontmatter_json"]))
                metadata = (
                    cast("dict[str, object]", metadata_raw)
                    if isinstance(metadata_raw, dict)
                    else {}
                )
                if matches_frontmatter_filter(metadata, frontmatter):
                    allowed.append(str(row["note_id"]))
            note_ids_json = json.dumps(allowed)
        return _SearchScope(
            folder=normalized_folder or None,
            tags_json=json.dumps(required_tags, ensure_ascii=False),
            note_ids_json=note_ids_json,
        )

    def _require_writable(self) -> None:
        if self._read_only:
            raise PermissionError("read-only index refuses mutation")

    async def _migrate_notes_columns(self, connection: aiosqlite.Connection) -> None:
        """Add columns introduced after the initial ``notes`` schema (idempotent).

        Databases created before ``fs_mtime`` existed keep working: the column
        is added with NULL values, which callers treat as "always re-read".
        Discovery metadata is reconstructed from the indexed frontmatter and
        chunks, so upgrading does not require a filesystem-wide note read.
        Nested keys using the legacy slash separator are recalculated once;
        their existing tag JSON is preserved.
        """
        async with connection.execute("PRAGMA table_info(notes);") as cursor:
            rows = await cursor.fetchall()
        columns = {str(row[1]) for row in rows}
        if "fs_mtime" not in columns:
            await connection.execute("ALTER TABLE notes ADD COLUMN fs_mtime INTEGER;")
        if "tags_json" not in columns:
            await connection.execute("ALTER TABLE notes ADD COLUMN tags_json TEXT;")
        if "sort_key" not in columns:
            await connection.execute("ALTER TABLE notes ADD COLUMN sort_key TEXT;")

        async with connection.execute(
            "SELECT note_id, rel_path, frontmatter_json, tags_json FROM notes "
            "WHERE tags_json IS NULL OR sort_key IS NULL "
            "OR (instr(rel_path, '/') > 0 AND instr(sort_key, char(0)) = 0);"
        ) as cursor:
            unprepared = await cursor.fetchall()
        for row in unprepared:
            tags_json = row["tags_json"]
            if tags_json is None:
                metadata_raw = json.loads(str(row["frontmatter_json"]))
                metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
                async with connection.execute(
                    "SELECT content FROM chunks_fts WHERE note_id = ? ORDER BY rowid;",
                    (str(row["note_id"]),),
                ) as cursor:
                    chunk_rows = await cursor.fetchall()
                indexed_body = "\n".join(str(chunk[0]) for chunk in chunk_rows)
                tags_json = json.dumps(extract_tags(metadata, indexed_body), ensure_ascii=False)
            await connection.execute(
                "UPDATE notes SET tags_json = ?, sort_key = ? WHERE note_id = ?;",
                (
                    str(tags_json),
                    _vault_order_key(str(row["rel_path"])),
                    str(row["note_id"]),
                ),
            )

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

        mappings = await asyncio.to_thread(read_ulid_mappings, source_path)
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
    *,
    weighted: bool,
    scope: _SearchScope | None,
) -> list[sqlite3.Row]:
    sql = _search_sql(weighted=weighted, scoped=scope is not None)
    parameters: tuple[object, ...]
    if scope is None:
        parameters = (fts_query, limit)
    else:
        parameters = (
            fts_query,
            scope.folder,
            scope.folder,
            scope.folder,
            scope.tags_json,
            scope.note_ids_json,
            scope.note_ids_json,
            limit,
        )
    async with connection.execute(sql, parameters) as cursor:
        return cast("list[sqlite3.Row]", await cursor.fetchall())


def _chunk_context(note_title: str, header_path: str) -> str:
    """Return the searchable context of one chunk: note title plus heading trail."""
    if not header_path:
        return note_title
    return f"{note_title}{CHUNK_CONTEXT_SEPARATOR}{header_path}"


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
) -> tuple[str, str, str, str, str, str, str, str, int | None, str, str]:
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
        json.dumps(note.tags, ensure_ascii=False),
        _vault_order_key(note.rel_path),
    )


def _vault_order_key(rel_path: str) -> str:
    """Encode the reader's files-before-subdirectories traversal order.

    NUL separates path components so an exact directory name sorts before
    prefix siblings such as ``proj-x`` and ``proj.old``.
    """
    parts = rel_path.split("/")
    return "\x00".join(
        f"{'0' if index == len(parts) - 1 else '1'}{part}" for index, part in enumerate(parts)
    )


def _chunk_row(
    chunk: Chunk,
    context: str,
) -> tuple[str, str, str, str, str | None, str, str, int, str, int, int, int, str, str | None, str]:
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
        context,
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
        supersedes=coerce_string_list(parsed.get("supersedes"), split_delimited=False),
        valid_from=_optional_str(parsed.get("valid_from")),
        invalid_at=_optional_str(parsed.get("invalid_at")),
        invalidated_by=_optional_str(parsed.get("invalidated_by")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


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
