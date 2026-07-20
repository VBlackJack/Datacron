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

Importing a Protocol does not pull in the concrete implementation -- that
is the whole point. ``mcp/tools.py`` can depend on
:class:`ASTChunker` for typing while the runtime wiring lives in
``cli.py`` (the only module allowed to import across all layers).

If the contract evolves, update the canonical contract first, then update this
module to match, then bump the amendment row in the frozen-status table.
Silent drift between this module and the contract breaks consumers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from datacron.core.models import (
    Chunk,
    EvalPipeline,
    EvalQuestion,
    EvalReport,
    EvalTransport,
    IndexStats,
    Note,
    SearchResult,
    Wikilink,
)
from datacron.core.operation_log import OperationContext, OperationRecord
from datacron.core.temporal import TemporalMeta

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

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
    (``MarkdownChunker``). See contracts section 2.1 for required behavior:
    heading-bounded splits, atomic code/table/list/quote blocks,
    deterministic ordering, ``chunk_id`` format per section 1.3.
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
    allowed within a chunk). Does NOT resolve targets to note IDs --
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
    contracts section 2.3 for transaction semantics and snippet format.
    """

    async def open(self, db_path: Path, *, read_only: bool = False) -> None:
        """Open the database, optionally without any writable SQLite sidecar."""
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

    async def get_note_rel_path(self, note_id: str) -> str | None:
        """Return the indexed vault-relative path for ``note_id``, if present."""
        ...

    async def get_note_id(self, rel_path: str) -> str | None:
        """Return the indexed note ID for ``rel_path``, if present."""
        ...

    async def list_note_paths(
        self,
        *,
        folder: str | None,
        tags: list[str],
        frontmatter: dict[str, str] | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[str], int]:
        """Return a paginated discovery-order path page and total count."""
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

    async def get_generation(self) -> int:
        """Return the completed index generation counter."""
        ...

    async def set_generation(self, generation: int) -> None:
        """Set generation while preparing an offline replacement index."""
        ...

    async def increment_generation(self) -> int:
        """Commit and return the next completed index generation."""
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
    via the supplied :class:`FTS5Store`. See contracts section 2.4 for the
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
        fallback_max_pattern_length: int | None = None,
        fallback_timeout_seconds: float | None = None,
    ) -> list[SearchResult]:
        """Run ripgrep against ``vault_root`` and resolve matches."""
        ...


@runtime_checkable
class EvalHarness(Protocol):
    """Runs an evaluation suite against the live retrieval pipeline."""

    async def run(
        self,
        eval_questions: list[EvalQuestion],
        app: DatacronApp,
        k_values: Sequence[int] = (5, 10, 20),
        *,
        pipeline: EvalPipeline = EvalPipeline.TOOL,
        transport: EvalTransport = EvalTransport.IMPL,
        render: bool = True,
    ) -> EvalReport:
        """Execute the eval and return its complete report."""
        ...


@runtime_checkable
class VaultReader(Protocol):
    """Reads notes from the filesystem and resolves aliases.

    Implementation: ``src/datacron/core/vault.py``
    (``FilesystemVaultReader``). The reader is **construction-bound to
    a single ``vault_root``**: implementations MUST accept ``vault_root``
    as a constructor parameter and MUST NOT take it as a method argument.
    The bound vault_root governs every method call.

    See contracts section 2.6 for the full resolution rules -- in particular,
    :meth:`resolve_alias` uses strict global priority
    (title -> filename stem -> aliases) across all notes, not per-note.
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
        :meth:`list_notes` but only ``stat()``s each file -- no content read or
        parse. Used by the index read-repair to skip the read+hash of notes
        whose filesystem mtime is unchanged.
        """
        ...

    async def resolve_alias(self, alias: str) -> str | None:
        """Return the matching note's ULID, or ``None``."""
        ...

    async def invalidate_alias_cache(self) -> None:
        """Drop cached alias state after vault/index changes.

        Implementations without an alias cache may use a no-op.
        """
        ...


@runtime_checkable
class VaultWriter(Protocol):
    """Writes notes through confined, reversible filesystem primitives."""

    async def write_note_atomic(
        self,
        rel_path: str,
        content: str,
        *,
        overwrite: bool,
        expected_hash: str | None = None,
        note_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> str:
        """Durably write complete Markdown content and return its exact-byte hash."""
        ...

    async def mutate_note_atomic(
        self,
        rel_path: str,
        mutation: Callable[[str], str],
        *,
        expected_hash: str | None = None,
        operation: OperationContext | None = None,
    ) -> str:
        """Run a locked read-CAS-mutate-durable-write transaction."""
        ...

    async def revert_note_atomic(
        self,
        rel_path: str,
        to_hash: str,
        *,
        expected_hash: str | None,
        operation: OperationContext,
    ) -> str:
        """Restore exact content-addressed history under CAS and audit it."""
        ...

    async def recover_operations(self) -> int:
        """Recover durable pending operation manifests."""
        ...

    async def list_operations(self) -> list[OperationRecord]:
        """Return committed operation records without mutating journal state."""
        ...

    async def purge_history(self) -> list[str]:
        """Apply configured exact-content retention and return removed hashes."""
        ...
