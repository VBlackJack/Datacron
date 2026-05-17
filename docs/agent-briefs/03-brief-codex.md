# Brief — Codex (Indexing & Retrieval Track)

> **Agent**: Codex (OpenAI)
> **Branch**: `codex/phase0`
> **Working directory**: `G:\_Projects\Datacron`
> **Duration**: 4 weeks (Phase 0 of Datacron)
> **Co-agent**: Claude Code (parallel work on `core/`, `mcp/`, `cli`, `installers/`)

---

## Your role

You are the **implementation specialist** for the indexing and retrieval layers of
Datacron Phase 0. You own:

- The **AST chunker** that parses Markdown into structured chunks
- The **SQLite FTS5 store** for BM25 search
- The **ripgrep wrapper** for regex/exact search
- The **wikilinks extractor** for backlinks
- The **eval harness** that measures retrieval quality

You do NOT touch `src/datacron/core/`, `src/datacron/mcp/`, `src/datacron/cli.py`,
`src/datacron/installers/`, or the build configuration. Those are Claude Code's.

You consume Pydantic models from `src/datacron/core/models.py` (Claude Code provides
them in Sem 1 per `01-contracts.md` §1).

---

## Mandatory pre-flight

Before writing any code, complete this checklist:

- [ ] Read [`00-shared-context.md`](./00-shared-context.md) (standards, layout, success criteria)
- [ ] Read [`01-contracts.md`](./01-contracts.md) (Pydantic models + Protocols — **frozen**)
- [ ] Read [`../../README.md`](../../README.md)
- [ ] Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) §5 (catalog MCP) and §7 (layout)
- [ ] Read [`../decisions-tranchees-v2.1.md`](../decisions-tranchees-v2.1.md) §§4.4, 4.5, 4.6, 4.12 (the parts about retrieval, chunking, eval gate)
- [ ] Read [`04-integration-plan.md`](./04-integration-plan.md) (weekly schedule + cross-review)

---

## Scope & deliverables

### Week 1 — AST chunker

**Goal**: A pure-function chunker that turns a `Note` into a list of `Chunk` per §1.3
of `01-contracts.md`. No I/O. No database. Just AST parsing + chunk construction.

**Prerequisite**: Claude Code merges the contracts file (`core/models.py`) on Monday Sem 1.
You import `Note`, `Chunk`, `ChunkType` from there.

| Deliverable | Path | Notes |
|---|---|---|
| `chunker.py` | `src/datacron/indexing/chunker.py` | Implements ASTChunker Protocol per §2.1 |
| `__init__.py` | `src/datacron/indexing/__init__.py` | Exports `MarkdownChunker` |
| Unit tests | `tests/unit/indexing/test_chunker.py` | ≥20 tests, ≥85% coverage |
| Test fixtures | `tests/fixtures/chunker/*.md` | Edge cases: nested headings, code blocks, tables, empty notes, no headings, frontmatter-only |

**Implementation guidance**:

- Use `mistletoe` or `markdown-it-py` for AST parsing. Pick one and document the choice.
- Split chunks at heading boundaries (h1-h6).
- A code fence (```language) is an atomic chunk of `ChunkType.CODE` with `lang` set.
- A GFM table is an atomic chunk of `ChunkType.TABLE`.
- A bullet/ordered list with N items is **one** chunk of `ChunkType.LIST` (don't split items).
- A blockquote is a `ChunkType.QUOTE` chunk.
- Frontmatter parsed by Claude Code is NOT re-chunked here; chunks come from `Note.content`
  (which excludes frontmatter).
- A note with no headings produces one chunk per "paragraph unit" (separated by blank lines)
  with empty `header_path` and `section_title=None`.
- `header_path` is the **human-readable** breadcrumb of parent headings, joined with ` / `
  (space-slash-space), per the frozen contract in `01-contracts.md` §1.3.
  E.g. headings `["Architecture", "Chunking strategy"]` → `header_path="Architecture / Chunking strategy"`.
- For **`chunk_id` construction only**, you also compute a slugged form (kebab-case,
  lowercase, ASCII) of the same headings, joined with `/`. Keep this in a private helper
  function `_slug_header_path(headings: list[str]) -> str` returning e.g. `"architecture/chunking-strategy"`.
  The slugged form is NEVER stored on the Chunk — it lives only inside the `chunk_id` string.
- Slugging rules for `_slug_header_path`: lowercase, ASCII-fold via `unicodedata.normalize`,
  replace non-alphanumeric with `-`, collapse repeats, strip leading/trailing `-`. Empty
  headings list → empty string (top-level content). Unit-test the helper independently.
- `ordinal` is zero-indexed within the same `(note_id, header_path, chunk_type)` group.
  Format with `f"{ordinal:04d}"` in the chunk_id.
- `token_count`: deterministic heuristic `len(content) // 4`. Don't import tiktoken.
- `wikilinks_out`: simple regex `\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]` capturing the raw target.
  (The WikilinksExtractor does the full parsing; the chunker just extracts the raw targets
  for the chunk's metadata.)

**End of Week 1 deliverable**:

```python
from datacron.core.models import Note
from datacron.indexing.chunker import MarkdownChunker

chunker = MarkdownChunker()
note = Note(...)  # constructed by test
chunks = chunker.chunk(note)
assert all(c.note_id == note.id for c in chunks)
assert all(c.chunk_id.startswith(f"{note.id}::") for c in chunks)
```

**PR for Friday Sem 1**: `[Sem 1] AST chunker + tests`. Request Claude Code review.

---

### Week 2 — SQLite FTS5 store + wikilinks parser

**Goal**: A persistent SQLite store with FTS5-backed BM25 search, plus a wikilinks
parser that produces `Wikilink` records.

**Prerequisite**: Your Sem 1 PR is merged. Claude Code's `core/models.py` is stable.

| Deliverable | Path | Notes |
|---|---|---|
| `fts5_store.py` | `src/datacron/indexing/fts5_store.py` | Implements FTS5Store Protocol per §2.3 |
| `wikilinks.py` | `src/datacron/indexing/wikilinks.py` | Implements WikilinksExtractor Protocol per §2.2 |
| Unit tests fts5 | `tests/unit/indexing/test_fts5_store.py` | Insert/update/delete/search, edge cases |
| Unit tests wikilinks | `tests/unit/indexing/test_wikilinks.py` | All wikilink syntax variants |

**`fts5_store.py` implementation guidance**:

- Use `aiosqlite` for async access.
- Schema (managed inside `open()`):
  ```sql
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

  CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
      chunk_id UNINDEXED,
      note_id UNINDEXED,
      note_rel_path UNINDEXED,
      header_path UNINDEXED,
      section_title UNINDEXED,
      chunk_type UNINDEXED,
      content,                  -- the indexed text
      ordinal UNINDEXED,
      content_hash UNINDEXED,
      token_count UNINDEXED,
      wikilinks_out_json UNINDEXED,
      lang UNINDEXED,
      tokenize = 'unicode61 remove_diacritics 2'
  );

  CREATE TABLE IF NOT EXISTS ulid_paths (
      rel_path TEXT PRIMARY KEY,
      note_id TEXT NOT NULL UNIQUE
  );
  ```
- `upsert_note(note, chunks)`: transaction. Delete old chunks for note_id, insert note row,
  insert all new chunks. Update `indexed_at`.
- `search(query, limit)`: `SELECT chunk_id, content, bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?`. Note: bm25 returns lower=better; **invert it** for the SearchResult so higher=better.
- `search` must produce a `snippet` highlighting query terms with `**term**`. Use FTS5's
  `snippet()` function with `'**'` markers, max 32 tokens.
- Build the `Chunk` for each result by reading all UNINDEXED columns. Don't make extra DB calls.

**`wikilinks.py` implementation guidance**:

- Regex: `\[\[(?P<target>[^\]|#^]+)(?:#\^(?P<block>[^\]|]+))?(?:#(?P<header>[^\]|]+))?(?:\|(?P<display>[^\]]+))?\]\]`
  (and a few variants — write tests covering each).
- For each match in the chunk, produce a `Wikilink`. Don't resolve `target` to a note_id
  (that's the caller's job using VaultReader).
- Handle escaped brackets `\[\[` (should NOT match).
- Handle multi-line wikilinks (they exist; allow `\s+` inside).

**End of Week 2 deliverable**:

```python
import aiosqlite
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.wikilinks import RegexWikilinksExtractor

store = SQLiteFTS5Store()
await store.open(Path("/tmp/test.db"))
await store.upsert_note(note, chunks)
results = await store.search("kafka adoption", limit=10)
assert all(isinstance(r.chunk, Chunk) for r in results)

extractor = RegexWikilinksExtractor()
links = extractor.extract(chunks[0])
```

**PR for Friday Sem 2**: `[Sem 2] FTS5 store + wikilinks extractor + tests`. Request Claude Code review.

---

### Week 3 — ripgrep wrapper

**Goal**: An async subprocess wrapper around `ripgrep --json` that returns `SearchResult`
objects.

**Prerequisite**: Your Sem 2 PR is merged. `FTS5Store` is available for chunk lookup.

| Deliverable | Path | Notes |
|---|---|---|
| `ripgrep.py` | `src/datacron/indexing/ripgrep.py` | Implements RipgrepWrapper Protocol per §2.4 |
| Unit tests | `tests/unit/indexing/test_ripgrep.py` | Mock subprocess, parse JSON, edge cases |

**Implementation guidance**:

- Use `asyncio.create_subprocess_exec(...)` with `stdout=PIPE`, `stderr=PIPE`.
- Command: `[rg_path, "--json", "--max-count", str(limit), pattern, str(vault_root)]`
  Add `--glob` if provided.
- `rg_path` resolved from `DATACRON_RIPGREP_PATH` env var (default `"rg"`).
- Parse the JSON output **line by line** (each line is a JSON object). Don't slurp.
- Match events of `type == "match"` give you `path`, `lines`, `line_number`, `submatches`.
- To resolve each match to a `Chunk`:
  1. Call `store.list_chunks_for_note(note_id)` (you need to map path → note_id; use
     `ulid_paths` table or expose a helper on the store).
  2. Find the chunk whose source line range contains the matched line number. **Note**:
     Chunks don't currently store line ranges; you'll need to *amend the contract* to add
     `line_start: int` and `line_end: int` to `Chunk` (open a contract amendment PR in
     Sem 2 so it's frozen by Sem 3).
  3. If no chunk matches, log INFO and drop the result.
- `score = 1.0 / (1.0 + rank_index)` where rank_index is the 0-based result position.
- `snippet` = the matched line with `**<match>**` around the matched substrings.
- Honor `limit`: stop reading subprocess output after `limit` matches.
- Properly terminate the subprocess on early exit (use try/finally with `proc.kill()`).

**Note about the contract amendment**: You will likely need to add `line_start` and
`line_end` to `Chunk` to make ripgrep → chunk resolution work. **Open the amendment PR
in Sem 2** so it's debated and frozen by the time you start ripgrep in Sem 3.

**End of Week 3 deliverable**:

```python
from datacron.indexing.ripgrep import RipgrepWrapper

rg = RipgrepWrapper()
results = await rg.search(
    pattern=r"kafka",
    vault_root=Path("/test/vault"),
    glob="*.md",
    limit=20,
    store=fts5_store,
)
assert all(isinstance(r, SearchResult) for r in results)
```

**PR for Friday Sem 3**: `[Sem 3] Ripgrep wrapper + tests + (if needed) contract amendment for Chunk line ranges`. Request Claude Code review.

---

### Week 4 — Eval harness + metrics

**Goal**: An eval harness that runs a list of `EvalQuestion` against the live index and
produces `EvalResult` metrics. Plus the metrics computation primitives.

**Prerequisite**: Your Sem 3 PR is merged. Claude Code is in polish mode and will wire
your harness to the CLI (`datacron eval`).

| Deliverable | Path | Notes |
|---|---|---|
| `harness.py` | `src/datacron/eval/harness.py` | Implements EvalHarness Protocol per §2.5 |
| `metrics.py` | `src/datacron/eval/metrics.py` | Pure functions: recall@k, citation_precision |
| `__init__.py` | `src/datacron/eval/__init__.py` | Exports public API |
| Unit tests | `tests/unit/eval/test_harness.py`, `test_metrics.py` | Cover happy + edge cases |
| Eval set template | `examples/eval-questions.example.yaml` | Empty template for Julien to fill |

**`metrics.py` implementation guidance**:

```python
def recall_at_k(expected: list[str], retrieved: list[str], k: int) -> float:
    """Fraction of expected chunk_ids found in the top-k retrieved."""
    if not expected:
        return 1.0
    top_k = set(retrieved[:k])
    hits = sum(1 for e in expected if e in top_k)
    return hits / len(expected)


def citation_precision(expected: list[str], retrieved: list[str]) -> float:
    """Fraction of retrieved that are in expected. Returns 1.0 if retrieved is empty."""
    if not retrieved:
        return 1.0
    expected_set = set(expected)
    hits = sum(1 for r in retrieved if r in expected_set)
    return hits / len(retrieved)
```

Cover with property-based tests (using `hypothesis` if available) — these are easy to get wrong.

**`harness.py` implementation guidance**:

- Load `EvalQuestion`s from a YAML/JSON file. Validate via Pydantic.
- For each question:
  1. Start a perf counter.
  2. Call `store.search(question.question, limit=max(k_values))` to get top-N from BM25.
  3. Optionally also call `ripgrep.search(question.question, ...)` if the question has
     a `use_regex: true` flag.
  4. Build the retrieved chunk_id list.
  5. Compute metrics via `metrics.py`.
  6. Stop perf counter.
  7. Sum `chunk.token_count` for tokens_returned.
  8. Build and append `EvalResult`.
- After all questions: print a summary table (use `rich` if available) and return the list.
- Support an optional `--label` CLI flag that, after each question, prompts Julien for
  a trust_label (`high`/`medium`/`low`/`skip`). Read from stdin synchronously.

**End of Week 4 deliverable**:

```bash
datacron eval --questions examples/eval-questions.example.yaml
# Output:
# Question 1/30: "What did I write about Kafka?"
#   retrieved: 5 chunks · recall@5: 0.80 · recall@10: 1.00 · precision: 0.60
#   latency: 45ms · tokens: 412
# ...
# === Summary ===
# Avg recall@10: 0.92  Avg precision: 0.71  Avg latency: 52ms  Avg tokens: 380
```

**PR for Friday Sem 4**: `[Sem 4] Eval harness + metrics + example questions`. After merge, Julien
runs the success test.

---

## Specific implementation notes

### Standards reminders (from `00-shared-context.md`)

- Apache 2.0 header on every `.py`
- All English (code, comments, docstrings, identifiers)
- No hardcoded values — read from `core/config.py`
- Use `core/logger.py.get_logger(__name__)`, never `print`
- Type hints everywhere; `mypy --strict` must pass
- Async/await for all I/O
- Tests: ≥85% coverage on your modules
- No imports from `mcp/`, `cli.py`, or `installers/` (per `01-contracts.md` §3)

### Dependencies you may add to `pyproject.toml`

Coordinate with Claude Code via PR comment. Likely additions:

- `mistletoe` or `markdown-it-py` (chunker)
- `aiosqlite` (FTS5 store)
- `pyyaml` (eval questions)
- `rich` (eval summary, optional)
- `hypothesis` (property tests, optional)

`pytest`, `pytest-asyncio`, `pydantic` will already be in pyproject from Claude Code.

### Performance targets (informational, not blocking)

- AST chunker: ≤10ms per typical note (≈2KB Markdown)
- FTS5 upsert: ≤5ms per note (write); search ≤50ms on 1k notes (BM25)
- ripgrep wrapper: ≤200ms for typical regex on 1k notes
- Eval harness: ≤5s for 30 questions end-to-end

If you blow these by >2×, flag it in your PR description.

### Testing approach

- Use `tests/fixtures/demo-vault/` (Claude Code provides this in Sem 1).
- For chunker: write 1 markdown file per edge case in `tests/fixtures/chunker/`, parse it,
  assert the chunk structure. Snapshot tests are fine for stable cases.
- For FTS5: use `tmp_path` fixture to create a fresh DB per test.
- For ripgrep: mock the subprocess via `pytest-mock` for unit tests; integration test
  with real `rg` in a `tests/integration/test_ripgrep_real.py` (marked `@pytest.mark.integration`).
- For eval: feed fake stores; assert metric correctness without needing a real vault.

---

## Out of scope (do NOT build)

- ❌ Anything in `src/datacron/core/`, `mcp/`, `cli.py`, `installers/` (Claude Code's territory)
- ❌ Embeddings, LanceDB, vector search (Phase v0.4 if ever)
- ❌ Contextual Retrieval (Phase v0.5 if ever)
- ❌ LangGraph, Ollama
- ❌ Write tools
- ❌ HTTP transport
- ❌ Watcher daemons

If you find yourself implementing one of these, **stop and re-read decisions-tranchees-v2.1.md**.

---

## Cross-review expectations

Every Friday, you open a PR with the week's work. PR description includes a section
`## Cross-review requests for Claude Code` listing what you want them to verify
(e.g., "Please verify my snippet highlighting format matches what `mcp/tools.py` expects").

Claude Code will leave inline comments. You address them by Monday morning.

When **you** review Claude Code's PR (every Friday), focus on:
- Whether their consumption of your Protocols matches the contract
- Whether the MCP tool layer is bounding results correctly (`DATACRON_MAX_RESULT_COUNT`,
  `DATACRON_MAX_RESULT_TOKENS`)
- Whether they're properly awaiting async methods (no `asyncio.run` inside other async)
- Whether the sandbox wrapping is applied consistently to all content responses

---

## Definition of done (Phase 0)

- [ ] All deliverables in §Scope produced and merged to `main`
- [ ] All your modules pass `ruff check`, `ruff format --check`, `mypy --strict`, `pytest`
- [ ] Test coverage ≥85% on `src/datacron/indexing/` and `src/datacron/eval/`
- [ ] Performance targets met (or deviations flagged)
- [ ] Eval harness produces metrics on Julien's real vault
- [ ] No imports cross the forbidden boundaries (per `01-contracts.md` §3)

When all boxes are ticked, you write a short retrospective at `docs/retro-phase0-codex.md`
(in English): what went well, what hurt, what to change for v0.2.

Good luck. Ship clean code.
