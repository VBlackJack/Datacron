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
"""Frozen Pydantic models shared across Datacron.

This module is the canonical implementation of the shared data contracts. The
contract is frozen: any change here requires a separate
``contract: amend <X>`` PR. Silent divergence breaks cross-module consumers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Chunk",
    "ChunkType",
    "EvalQuestion",
    "EvalResult",
    "IndexStats",
    "Note",
    "SearchResult",
    "Wikilink",
]


class Note(BaseModel):
    """A Markdown note read from the vault.

    Attributes:
        id: ULID stable identifier. Generated and stored in ``.datacron/``
            side-table if the note has no ``id`` frontmatter key.
        path: Absolute path on disk.
        rel_path: Path relative to vault root (POSIX separators).
        title: Human-readable title. Order of resolution: frontmatter ``title``,
            first H1, filename without extension.
        frontmatter: Parsed YAML frontmatter as a dict. Empty dict if absent.
        content: Raw Markdown body, WITHOUT the frontmatter block.
        raw_content: Full file content, INCLUDING the frontmatter block.
        created: Creation timestamp. Order of resolution: frontmatter ``created``,
            filesystem ctime.
        updated: Last-modified timestamp. Order of resolution: frontmatter
            ``updated``, filesystem mtime.
        content_hash: SHA-256 of ``raw_content`` encoded as UTF-8 with LF line
            endings, no BOM. Hex string, lowercase, no prefix.
        tags: List of tags. Includes frontmatter ``tags`` (list or
            comma-separated string) and inline ``#tag`` occurrences.
            Deduplicated, lowercase.
        aliases: List of aliases from frontmatter ``aliases``.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=26, max_length=26)
    path: Path
    rel_path: str
    title: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    content: str
    raw_content: str
    created: datetime
    updated: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class ChunkType(StrEnum):
    """Classification of a chunk's content type."""

    FRONTMATTER = "frontmatter"
    NARRATIVE = "narrative"
    HEADING = "heading"
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    QUOTE = "quote"


class Chunk(BaseModel):
    """A semantic unit extracted from a Note.

    Chunks are produced by the ``ASTChunker`` protocol. One Note yields one or
    more Chunks.

    Attributes:
        chunk_id: Deterministic ID with format
            ``{note_id}::{header_slug_path}::{ordinal}``. ``header_slug_path``
            is the slash-joined kebab-case slug of parent headings (empty
            string for top-level content). ``ordinal`` is zero-padded to four
            digits. Example:
            ``01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003``.
        note_id: The ULID of the parent Note.
        note_rel_path: Vault-relative path of the parent Note (POSIX).
        header_path: Slash-joined human-readable header trail (NOT slugged).
            Example: ``"Architecture / Chunking strategy"``.
        section_title: The immediate parent heading text. ``None`` for
            top-level chunks outside any heading.
        chunk_type: See :class:`ChunkType`.
        content: The chunk's raw text content.
        ordinal: Zero-indexed position within the
            ``(header_path, chunk_type)`` sequence for the parent Note.
            Used in ``chunk_id``.
        content_hash: SHA-256 hex of ``content`` (UTF-8, LF).
        token_count: Approximate token count. Deterministic heuristic
            (e.g., ``len(content) // 4``); precision is not required.
        line_start: 1-indexed starting line number in the parent note's
            ``raw_content``. Used by RipgrepWrapper to resolve a
            ``(file_path, line_number)`` match back to a Chunk without
            re-parsing or side tables.
        line_end: 1-indexed ending line number in the parent note's
            ``raw_content``, inclusive.
        wikilinks_out: List of raw wikilink targets found inside the chunk.
            Resolution to note IDs is the WikilinksExtractor's job, not the
            chunker's.
        lang: Programming language identifier for ``ChunkType.CODE``, else
            ``None``.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    note_id: str = Field(min_length=26, max_length=26)
    note_rel_path: str
    header_path: str
    section_title: str | None = None
    chunk_type: ChunkType
    content: str
    ordinal: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_count: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    wikilinks_out: list[str] = Field(default_factory=list)
    lang: str | None = None


class Wikilink(BaseModel):
    """A resolved or unresolved wikilink reference.

    Attributes:
        source_chunk_id: The Chunk where the wikilink was found.
        target_alias: The raw target string as written, e.g. ``"Some Note"``
            from ``[[Some Note]]``.
        resolved_note_id: ULID of the resolved target Note, or ``None`` if
            unresolved.
        display_text: Optional display alias from ``[[target|display]]``
            syntax.
        header_anchor: Optional anchor from ``[[target#header]]``.
        block_ref: Optional block reference from ``[[target#^id]]``.
    """

    model_config = ConfigDict(frozen=True)

    source_chunk_id: str
    target_alias: str
    resolved_note_id: str | None = None
    display_text: str | None = None
    header_anchor: str | None = None
    block_ref: str | None = None


class SearchResult(BaseModel):
    """A single result returned by a search tool.

    Attributes:
        chunk: The matched Chunk.
        score: Method-dependent ranking score. For BM25 (FTS5): higher is
            better. For ripgrep: rank-based (``1 / (1 + rank_index)``).
        snippet: A short highlighted excerpt suitable for inclusion in the
            MCP tool response. Plain text, no HTML; query terms surrounded by
            ``**term**`` markers for client display.
    """

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    score: float
    snippet: str


class IndexStats(BaseModel):
    """Aggregate statistics about the index."""

    model_config = ConfigDict(frozen=True)

    note_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    last_indexed_at: datetime | None = None
    db_size_bytes: int = Field(ge=0)
    db_path: Path


class EvalQuestion(BaseModel):
    """A single eval input."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    """Metrics for a single :class:`EvalQuestion` run.

    Attributes:
        question_id: The question's stable ID.
        retrieved_chunk_ids: Chunk IDs returned by the system under test, in
            ranked order.
        recall_at_k: Keys are k values (5, 10, 20); values are recall in
            ``[0, 1]``.
        citation_precision: Fraction of retrieved chunks that are in the
            expected set.
        latency_ms: End-to-end retrieval latency in milliseconds.
        tokens_returned: Approximate sum of ``chunk.token_count`` returned.
        trust_label: Optional human-assigned label (``"high"``, ``"medium"``,
            ``"low"``).
    """

    model_config = ConfigDict(frozen=True)

    question_id: str
    retrieved_chunk_ids: list[str]
    recall_at_k: dict[int, float] = Field(default_factory=dict)
    citation_precision: float = Field(ge=0.0, le=1.0)
    latency_ms: float = Field(ge=0.0)
    tokens_returned: int = Field(ge=0)
    trust_label: str | None = None
