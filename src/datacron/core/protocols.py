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
"""Materialized :class:`typing.Protocol` types for the Datacron contracts.

This module is the executable mirror of the service contracts. It exists so
that consumers can type-annotate their Protocol parameters and so that concrete
implementations can declare conformance via a one-line structural check (see
``core/vault.py`` for the pattern).

Importing a Protocol does not pull in the concrete implementation — that
is the whole point. ``mcp/tools.py`` can depend on
:class:`ASTChunker` for typing while the runtime wiring lives in
``cli.py`` (the only module allowed to import across all layers).

If the contract evolves, update the canonical contract first, then update this
module to match, then bump the amendment row in the frozen-status table.
Silent drift between this module and the contract breaks consumers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from datacron.core.models import (
    Chunk,
    EvalQuestion,
    EvalResult,
    IndexStats,
    Note,
    SearchResult,
    Wikilink,
)
from datacron.core.temporal import TemporalMeta

__all__ = [
    "ASTChunker",
    "EvalHarness",
    "FTS5Store",
    "RipgrepWrapper",
    "VaultReader",
    "VaultWriter",
    "WikilinksExtractor",
]


@runtime_checkable
class ASTChunker(Protocol):
    """Parses a Markdown :class:`Note` into :class:`Chunk` instances.

    Implementation: ``src/datacron/indexing/chunker.py``
    (``MarkdownChunker``). See contracts §2.1 for required behavior:
    heading-bounded splits, atomic code/table/list/quote blocks,
    deterministic ordering, ``chunk_id`` format per §1.3.
    """

    def chunk(self, note: Note) -> list[Chunk]:
        """Return all chunks for the given Note, in document order."""
        ...


@runtime_checkable
class WikilinksExtractor(Protocol):
    """Extracts :class:`Wikilink` references from a :class:`Chunk`.

    Implementation: ``src/datacron/indexing/wikilinks.py``. Recognizes
    ``[[target]]``, ``[[target|display]]``, ``[[target#header]]``,
    ``[[target#^block]]``. Returns one Wikilink per occurrence (duplicates
    allowed within a chunk). Does NOT resolve targets to note IDs —
    callers use :meth:`VaultReader.resolve_alias` for that.
    """

    def extract(self, chunk: Chunk) -> list[Wikilink]:
        """Return all wikilinks found in the chunk's content."""
        ...


@runtime_checkable
class FTS5Store(Protocol):
    """SQLite FTS5 storage for chunks + BM25 search.

    Implementation: ``src/datacron/indexing/fts5_store.py``. The store
    lives at ``{vault_root}/.datacron/index/datacron.db``. See
    contracts §2.3 for transaction semantics and snippet format.
    """

    async def open(self, db_path: Path) -> None:
        """Open or create the database. Idempotent."""
        ...

    async def close(self) -> None:
        """Close the database. Idempotent."""
        ...

    async def upsert_note(
        self, note: Note, chunks: list[Chunk], fs_mtime_ns: int | None = None
    ) -> None:
        """Insert or update a Note and its Chunks atomically.

        Replaces all chunks for ``note.id``. ``fs_mtime_ns`` is the note file's
        ``st_mtime_ns`` at index time, stored for the read-repair mtime gate.
        """
        ...

    async def delete_note(self, note_id: str) -> None:
        """Remove a Note and all its Chunks. No error if absent."""
        ...

    async def record_mtime(self, note_id: str, fs_mtime_ns: int) -> None:
        """Update only the stored filesystem mtime for ``note_id``.

        Lets the read-repair refresh the mtime of a note whose content is
        unchanged but whose mtime moved, so the next repair can skip it.
        """
        ...

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """BM25 search over chunk content.

        Empty results = empty list, never ``None``. Snippets must
        highlight matched terms with ``**term**`` markers.
        """
        ...

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Lookup a single chunk by id, or ``None`` if absent."""
        ...

    async def list_chunks_for_note(self, note_id: str) -> list[Chunk]:
        """Return all chunks for a note in ordinal order."""
        ...

    async def list_chunks_with_wikilinks(self) -> list[Chunk]:
        """Return all chunks whose indexed wikilink list is non-empty."""
        ...

    async def list_indexed_notes(self) -> dict[str, tuple[str, str]]:
        """Return ``rel_path -> (note_id, content_hash)`` for index freshness checks."""
        ...

    async def list_indexed_notes_with_mtime(self) -> dict[str, tuple[str, str, int | None]]:
        """Return ``rel_path -> (note_id, content_hash, fs_mtime_ns)`` for the index.

        ``fs_mtime_ns`` is ``None`` for rows indexed before the column existed;
        callers MUST treat ``None`` as "always re-read" (never skip).
        """
        ...

    async def list_temporal_metadata(self) -> dict[str, TemporalMeta]:
        """Return explicit retrieval lifecycle metadata keyed by note_id."""
        ...

    def iter_all_chunks(self) -> AsyncIterator[Chunk]:
        """Stream all indexed chunks in insertion/document order."""
        ...

    async def stats(self) -> IndexStats:
        """Aggregate stats."""
        ...


@runtime_checkable
class RipgrepWrapper(Protocol):
    """Subprocess wrapper around ``ripgrep --json``.

    Implementation: ``src/datacron/indexing/ripgrep.py``. Streams JSON
    output line-by-line, resolves matches to :class:`Chunk` instances
    via the supplied :class:`FTS5Store`. See contracts §2.4 for the
    score formula and snippet highlighting rules.
    """

    async def search(
        self,
        pattern: str,
        vault_root: Path,
        glob: str | None = None,
        limit: int = 20,
        store: FTS5Store | None = None,
        rg_path: str | None = None,
    ) -> list[SearchResult]:
        """Run ripgrep against ``vault_root`` and resolve matches."""
        ...


@runtime_checkable
class EvalHarness(Protocol):
    """Runs an evaluation suite against the live indexes.

    Implementation: ``src/datacron/eval/harness.py``. Reads
    :class:`EvalQuestion` records, hits FTS5 + ripgrep directly (not
    via MCP), and produces :class:`EvalResult` metrics for
    ``recall@k``, ``citation_precision``, ``latency_ms``,
    ``tokens_returned``. See contracts §2.5.
    """

    async def run(
        self,
        eval_questions: list[EvalQuestion],
        store: FTS5Store,
        ripgrep: RipgrepWrapper,
        k_values: list[int] = [5, 10, 20],  # noqa: B006 — Protocol default is documentation, not runtime state
    ) -> list[EvalResult]:
        """Execute the eval; return one :class:`EvalResult` per question."""
        ...


@runtime_checkable
class VaultReader(Protocol):
    """Reads notes from the filesystem and resolves aliases.

    Implementation: ``src/datacron/core/vault.py``
    (``FilesystemVaultReader``). The reader is **construction-bound to
    a single ``vault_root``**: implementations MUST accept ``vault_root``
    as a constructor parameter and MUST NOT take it as a method argument.
    The bound vault_root governs every method call.

    See contracts §2.6 for the full resolution rules — in particular,
    :meth:`resolve_alias` uses strict global priority
    (title → filename stem → aliases) across all notes, not per-note.
    """

    async def read_note(self, path: Path) -> Note:
        """Parse the markdown file at ``path`` into a populated Note.

        ``path`` must be inside the bound ``vault_root``; otherwise
        :class:`ValueError` is raised.
        """
        ...

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        """Return notes from the bound vault, optionally scoped/limited."""
        ...

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        """Return ``rel_path -> (absolute_path, st_mtime_ns)`` for every live note.

        Enumerates the vault with the same folder/file exclusions as
        :meth:`list_notes` but only ``stat()``s each file — no content read or
        parse. Used by the index read-repair to skip the read+hash of notes
        whose filesystem mtime is unchanged.
        """
        ...

    async def resolve_alias(self, alias: str) -> str | None:
        """Return the matching note's ULID, or ``None``."""
        ...


@runtime_checkable
class VaultWriter(Protocol):
    """Writes notes through confined, reversible filesystem primitives."""

    async def write_note_atomic(self, rel_path: str, content: str, *, overwrite: bool) -> None:
        """Write Markdown content under the bound vault root atomically."""
        ...
