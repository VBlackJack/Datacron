# Internal Contracts — Datacron Phase 0

> **Status**: **FROZEN** as of 2026-05-17. Amendments require a separate PR titled
> `contract: amend X` approved by both Claude Code and Codex.
>
> **Purpose**: This file is the **frontier** between Claude Code and Codex. Every
> Pydantic model and Protocol below is the public API that one agent provides and the
> other consumes. Implementing exactly these signatures means both agents can work in
> parallel from Sem 1 onward without collisions.

---

## 1. Pydantic models (shared types)

These types are owned by **Claude Code** (defined in `src/datacron/core/models.py`)
and imported by Codex. They are frozen for Phase 0.

### 1.1 `Note`

```python
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Note(BaseModel):
    """A Markdown note read from the vault.

    Attributes:
        id: ULID stable identifier. Generated and stored in `.datacron/` side-table
            if the note has no `id` frontmatter key.
        path: Absolute path on disk.
        rel_path: Path relative to vault root (POSIX separators).
        title: Human-readable title. Order of resolution: frontmatter `title`,
            first H1, filename without extension.
        frontmatter: Parsed YAML frontmatter as a dict. Empty dict if absent.
        content: Raw Markdown body, WITHOUT the frontmatter block.
        raw_content: Full file content, INCLUDING the frontmatter block.
        created: Creation timestamp. Order of resolution: frontmatter `created`,
            filesystem ctime.
        updated: Last-modified timestamp. Order of resolution: frontmatter `updated`,
            filesystem mtime.
        content_hash: SHA-256 of `raw_content` encoded as UTF-8 with LF line endings,
            no BOM. Hex string, lowercase, no prefix.
        tags: List of tags. Includes frontmatter `tags` (list or comma-separated string)
            and inline `#tag` occurrences. Deduplicated, lowercase.
        aliases: List of aliases from frontmatter `aliases`.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=26, max_length=26)  # ULID
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
```

### 1.2 `ChunkType`

```python
from enum import StrEnum


class ChunkType(StrEnum):
    """Classification of a chunk's content type."""
    FRONTMATTER = "frontmatter"
    NARRATIVE = "narrative"
    HEADING = "heading"
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    QUOTE = "quote"
```

### 1.3 `Chunk`

```python
class Chunk(BaseModel):
    """A semantic unit extracted from a Note.

    Chunks are produced by ASTChunker. One Note yields one or more Chunks.

    Attributes:
        chunk_id: Deterministic ID. Format: `{note_id}::{header_slug_path}::{ordinal}`.
            `header_slug_path` is the slash-joined kebab-case slug of parent headings
            (empty string for top-level content). `ordinal` is zero-padded to 4 digits.
            Example: `01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003`.
        note_id: The ULID of the parent Note.
        note_rel_path: Vault-relative path of the parent Note (POSIX).
        header_path: Slash-joined human-readable header trail (NOT slugged).
            Example: "Architecture / Chunking strategy".
        section_title: The immediate parent heading text. `None` for top-level chunks
            outside any heading.
        chunk_type: See ChunkType.
        content: The chunk's raw text content.
        ordinal: Zero-indexed position within the parent Note, ordered by document
            position, scoped to (note_id, header_path) only — NOT scoped to chunk_type.
            Two chunks of different types under the same heading share the
            (header_path) sequence and receive consecutive ordinals based on which
            appears first in the document. This guarantees chunk_id uniqueness given
            that the chunk_id format does NOT include chunk_type.

            Example: under heading "Architecture / Chunking", a narrative chunk
            followed by a code chunk → narrative.ordinal=0, code.ordinal=1 (NOT both 0).
        content_hash: SHA-256 hex of `content` (UTF-8, LF).
        token_count: Approximate token count. Use a deterministic heuristic
            (e.g., `len(content) // 4`); precision is not required.
        line_start: 1-indexed starting line number in the parent note's
            raw_content. Used by RipgrepWrapper to resolve `(file_path,
            line_number)` search matches back to the owning Chunk without
            re-parsing the note or maintaining a side-table.
        line_end: 1-indexed ending line number in the parent note's raw_content,
            inclusive. Used together with line_start for ripgrep result → chunk
            resolution in Sem 3.
        wikilinks_out: List of raw wikilink targets found inside the chunk.
            Resolution to note IDs is the WikilinksExtractor's job, not the chunker's.
        lang: Programming language identifier for `ChunkType.CODE`, else None.
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
```

### 1.4 `Wikilink`

```python
class Wikilink(BaseModel):
    """A resolved or unresolved wikilink reference.

    Attributes:
        source_chunk_id: The Chunk where the wikilink was found.
        target_alias: The raw target string as written, e.g. "Some Note" from `[[Some Note]]`.
        resolved_note_id: ULID of the resolved target Note, or None if unresolved.
        display_text: Optional display alias from `[[target|display]]` syntax.
        header_anchor: Optional anchor from `[[target#header]]`.
        block_ref: Optional block reference from `[[target#^id]]`.
    """

    model_config = ConfigDict(frozen=True)

    source_chunk_id: str
    target_alias: str
    resolved_note_id: str | None = None
    display_text: str | None = None
    header_anchor: str | None = None
    block_ref: str | None = None
```

### 1.5 `SearchResult`

```python
class SearchResult(BaseModel):
    """A single result returned by a search tool.

    Attributes:
        chunk: The matched Chunk.
        score: Method-dependent ranking score. For BM25 (FTS5): higher = better.
            For ripgrep: rank-based (1 / (1 + rank_index)).
        snippet: A short highlighted excerpt suitable for inclusion in the MCP tool
            response. Plain text, no HTML; query terms surrounded by `**term**` markers
            for client display.
    """

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    score: float
    snippet: str
```

### 1.6 `IndexStats`

```python
class IndexStats(BaseModel):
    """Aggregate statistics about the index."""

    model_config = ConfigDict(frozen=True)

    note_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    last_indexed_at: datetime | None = None
    db_size_bytes: int = Field(ge=0)
    db_path: Path
```

### 1.7 `EvalQuestion` and `EvalResult`

```python
class EvalQuestion(BaseModel):
    """A single eval input."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)  # alternative ground truth


class EvalResult(BaseModel):
    """Metrics for a single EvalQuestion run.

    `recall_at_k` keys are the k values (5, 10, 20); values are recall in [0, 1].
    `citation_precision`: fraction of retrieved chunks that are in expected set.
    `trust_label`: optional human-assigned label.
    """

    model_config = ConfigDict(frozen=True)

    question_id: str
    retrieved_chunk_ids: list[str]
    recall_at_k: dict[int, float] = Field(default_factory=dict)
    citation_precision: float = Field(ge=0.0, le=1.0)
    latency_ms: float = Field(ge=0.0)
    tokens_returned: int = Field(ge=0)
    trust_label: str | None = None  # "high" | "medium" | "low"
```

---

## 2. Protocols (interfaces between agents)

A Protocol is a structural type — anything that has the same methods satisfies it.
This lets Claude Code consume Codex's implementations without importing concrete classes.

### 2.1 `ASTChunker` (implemented by **Codex**)

```python
from typing import Protocol


class ASTChunker(Protocol):
    """Parses a Markdown Note into Chunks.

    Implementation lives in `src/datacron/indexing/chunker.py`.

    Required behavior:
    - Chunks split at heading boundaries (h1-h6). Each heading starts a new chunk.
    - Code blocks, tables, and frontmatter blocks are atomic — never split internally.
    - Sequence of chunks per Note is stable across runs (deterministic ordinal).
    - Empty notes produce a single chunk of ChunkType.NARRATIVE with empty content.
    - chunk_id format must match the spec in §1.3.
    """

    def chunk(self, note: Note) -> list[Chunk]:
        """Return all chunks for the given Note, in document order."""
        ...
```

### 2.2 `WikilinksExtractor` (implemented by **Codex**)

```python
class WikilinksExtractor(Protocol):
    """Extracts wikilinks from a Chunk.

    Implementation lives in `src/datacron/indexing/wikilinks.py`.

    Required behavior:
    - Recognizes `[[target]]`, `[[target|display]]`, `[[target#header]]`, `[[target#^block]]`.
    - Returns one Wikilink per occurrence (duplicates allowed within a chunk).
    - Does NOT resolve target → note_id; resolution is the caller's responsibility
      using VaultReader.resolve_alias().
    """

    def extract(self, chunk: Chunk) -> list[Wikilink]:
        """Return all wikilinks found in the chunk's content."""
        ...
```

### 2.3 `FTS5Store` (implemented by **Codex**)

```python
class FTS5Store(Protocol):
    """SQLite FTS5 storage for chunks + BM25 search.

    Implementation lives in `src/datacron/indexing/fts5_store.py`.

    The store lives at `{vault_root}/.datacron/index/datacron.db`. Schema is managed
    by the store; expose migrations via init/upgrade methods if needed (not required
    for Phase 0).
    """

    async def open(self, db_path: Path) -> None:
        """Open or create the database. Idempotent."""
        ...

    async def close(self) -> None:
        """Close the database. Idempotent."""
        ...

    async def upsert_note(self, note: Note, chunks: list[Chunk]) -> None:
        """Insert or update a Note and its Chunks atomically. Replaces all chunks
        for the note_id."""
        ...

    async def delete_note(self, note_id: str) -> None:
        """Remove a Note and all its Chunks. No error if absent."""
        ...

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """BM25 search over chunk content. Empty results = empty list, never None.
        Snippets must highlight matched terms with **term** markers."""
        ...

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Lookup a single chunk by id, or None if absent."""
        ...

    async def list_chunks_for_note(self, note_id: str) -> list[Chunk]:
        """Return all chunks for a note in ordinal order."""
        ...

    async def list_chunks_with_wikilinks(self) -> list[Chunk]:
        """Return all chunks whose indexed wikilink list is non-empty."""
        ...

    async def list_indexed_notes(self) -> dict[str, tuple[str, str]]:
        """Return rel_path -> (note_id, content_hash) for index freshness checks."""
        ...

    def iter_all_chunks(self) -> AsyncIterator[Chunk]:
        """Stream all indexed chunks in insertion/document order."""
        ...

    async def stats(self) -> IndexStats:
        """Aggregate stats."""
        ...
```

### 2.4 `RipgrepWrapper` (implemented by **Codex**)

```python
class RipgrepWrapper(Protocol):
    """Subprocess wrapper around ripgrep with JSON output parsing.

    Implementation lives in `src/datacron/indexing/ripgrep.py`.

    Required behavior:
    - Invokes `rg --json <pattern> <vault_root>` (with --glob if provided).
    - Parses streaming JSON output, never loads the full output into memory at once
      (use line-by-line iteration).
    - Returns SearchResult.chunk fields filled by looking up the chunk via FTS5Store
      based on the file path + line number. If the chunk cannot be resolved, the
      result is dropped (logged at INFO level).
    - Score = 1.0 / (1.0 + rank_index), where rank_index is the result index in the
      output order.
    - Snippet = the matched line with **<match>** highlighting.
    - If the ripgrep binary is missing, falls back to scanning indexed chunk bodies
      from FTS5Store.iter_all_chunks(); this fallback excludes frontmatter and depends
      on index freshness.
    """

    async def search(
        self,
        pattern: str,
        vault_root: Path,
        glob: str | None = None,
        limit: int = 20,
        store: FTS5Store | None = None,  # used for chunk resolution
        rg_path: str | None = None,
    ) -> list[SearchResult]:
        ...
```

Note: `RipgrepWrapper.search` depends on `FTS5Store` for chunk resolution. **Codex
must implement both, and `RipgrepWrapper` may import `FTS5Store` concrete class.**

### 2.5 `EvalHarness` (implemented by **Codex**)

```python
class EvalHarness(Protocol):
    """Runs an evaluation suite against the live MCP server.

    Implementation lives in `src/datacron/eval/harness.py`.

    Phase 0 scope:
    - Iterates over a list of EvalQuestion read from a YAML or JSON file.
    - For each question, calls the local FTS5Store + RipgrepWrapper directly
      (NOT via MCP) to time the retrieval-only path.
    - Computes recall@5, recall@10, recall@20, citation_precision, latency_ms,
      tokens_returned (approximated as sum of chunk.token_count).
    - Returns a list of EvalResult.
    - Optionally accepts a human-labeling callback for trust_label.
    """

    async def run(
        self,
        eval_questions: list[EvalQuestion],
        store: FTS5Store,
        ripgrep: RipgrepWrapper,
        k_values: list[int] = [5, 10, 20],
    ) -> list[EvalResult]:
        ...
```

### 2.6 `VaultReader` (implemented by **Claude Code**)

```python
class VaultReader(Protocol):
    """Reads notes from the filesystem and resolves aliases.

    Implementation lives in `src/datacron/core/vault.py`.

    VaultReader is **construction-bound to a single vault_root**. Implementations
    MUST accept `vault_root` as a constructor parameter and MUST NOT take it as a
    method argument. The bound vault_root governs every method call.

    Required behavior:
    - read_note: parses frontmatter (using python-frontmatter), generates or reads
      ULID, computes content_hash, returns a fully-populated Note. The `path`
      argument must be inside the bound vault_root; otherwise raise ValueError.
    - list_notes: yields Notes from the bound vault. May be lazy/iterable.
    - resolve_alias: matches a raw wikilink target to a Note by **strict global
      priority order**:
      (1) exact frontmatter `title` match across ALL notes — if found, return.
      (2) else filename without `.md` match across ALL notes — if found, return.
      (3) else frontmatter `aliases` entries across ALL notes — if found, return.
      Priority is global, NOT per-note. A title match on Note A wins over an
      alias match on Note B even if B is iterated first. Returns None if
      unresolved or ambiguous (multiple notes match at the same priority level).
    """

    async def read_note(self, path: Path) -> Note:
        ...

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        ...

    async def resolve_alias(self, alias: str) -> str | None:
        """Returns note_id or None."""
        ...
```

---

## 3. Module ownership matrix

| Module | Owner | Imports allowed from |
|---|---|---|
| `core/models.py` | Claude Code | stdlib, pydantic |
| `core/config.py` | Claude Code | stdlib, pydantic-settings |
| `core/logger.py` | Claude Code | stdlib |
| `core/paths.py` | Claude Code | stdlib, `core.config` |
| `core/hashing.py` | Claude Code | stdlib |
| `core/frontmatter.py` | Claude Code | stdlib, python-frontmatter |
| `core/vault.py` | Claude Code | core.*, ulid-py |
| `cli.py` | Claude Code | core.*, mcp.*, installers.*, indexing.*, eval.*, typer |
| `mcp/server.py` | Claude Code | core.*, indexing.*, mcp-python-sdk (FastMCP) |
| `mcp/tools.py` | Claude Code | core.*, indexing.*, mcp.sandbox |
| `mcp/resources.py` | Claude Code | core.*, indexing.* |
| `mcp/sandbox.py` | Claude Code | stdlib |
| `installers/claude_desktop.py` | Claude Code | stdlib, core.paths |
| `indexing/chunker.py` | **Codex** | core.models, mistletoe (or markdown-it-py) |
| `indexing/fts5_store.py` | **Codex** | core.models, aiosqlite |
| `indexing/ripgrep.py` | **Codex** | core.models, indexing.fts5_store, asyncio.subprocess |
| `indexing/wikilinks.py` | **Codex** | core.models, re |
| `eval/harness.py` | **Codex** | core.models, indexing.* |
| `eval/metrics.py` | **Codex** | core.models |

### Forbidden dependencies (enforced by review)

- `indexing/*` MUST NOT import from `mcp/`, `cli.py`, `installers/`, or `eval/`.
- `eval/*` MUST NOT import from `mcp/`, `cli.py`, or `installers/`.
- `core/*` MUST NOT import from `mcp/`, `indexing/`, `eval/`, or `installers/`.
- `mcp/*` may import from `core/` and `indexing/`. NOT from `eval/` or `cli.py`.
- `cli.py` is the only top-level orchestrator and may import from anywhere.

---

## 4. Reserved configuration keys

Both agents must use these env var / config key names exactly (zero hardcoding rule):

| Key | Default | Owner |
|---|---|---|
| `DATACRON_LOG_LEVEL` | `INFO` | core.config |
| `DATACRON_LOG_DIR` | `~/.datacron/logs` | core.config |
| `DATACRON_READ_PATHS` | (none — required) | core.config |
| `DATACRON_VAULT_ROOT` | (resolved from `.datacron/VAULT.yaml`) | core.config |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | mcp.tools, indexing.* |
| `DATACRON_MAX_RESULT_COUNT` | `20` | mcp.tools, indexing.* |
| `DATACRON_RIPGREP_PATH` | `rg` (PATH lookup) | indexing.ripgrep |
| `DATACRON_CHUNK_MAX_TOKENS` | `1024` | indexing.chunker |

---

## 5. Test fixtures

A small demo vault MUST be available at `tests/fixtures/demo-vault/` containing:

- 5 notes with diverse content (code blocks, tables, wikilinks, frontmatter variants)
- A note with no frontmatter
- A note in a subfolder
- A note with `important: true`
- An empty note
- A note that links to a non-existent note

Claude Code produces this fixture in Sem 1 as part of `conftest.py` setup.
Both agents may use it freely.

---

## 6. Amendment process

If you discover during implementation that a contract here is wrong, ambiguous, or
insufficient:

1. Open a separate PR titled `contract: amend <model or protocol>` with the proposed change.
2. PR description must explain: what's wrong, what changes, why, impact on the other agent.
3. The other agent must approve the PR.
4. Julien merges.
5. The amendment supersedes the original — update this file and bump the
   `Frozen as of` date at the top.

**Do not** silently diverge from contracts. That breaks parallel work.

---

## 7. Frozen status

| Section | Frozen since | Last amendment |
|---|---|---|
| §1 Pydantic models | 2026-05-17 | 2026-05-24 — §1.3 Chunk.line_start/line_end added for ripgrep result → chunk resolution; prior 2026-05-22 amendment clarified Chunk.ordinal scope |
| §2 Protocols | 2026-05-17 | 2026-05-23 — §2.6 VaultReader: bound at construction, removed `vault_root` from method signatures, made `resolve_alias` priority explicitly global (strict order title→filename→aliases across all notes) |
| §3 Ownership matrix | 2026-05-17 | — |
| §4 Reserved config keys | 2026-05-17 | — |
| §5 Test fixtures | 2026-05-17 | — |
