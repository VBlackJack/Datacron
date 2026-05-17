# Cross-review — Claude Code on codex/phase0 [Sem 2]

**Reviewed commits**: `f546e59..a48f9ff` (8 commits)
**Date**: 2026-05-17
**Verdict**: **Approve** — no blockers; two merge-time notes for Julien.

---

## Verification (run on `codex/phase0`)

```
$ git checkout codex/phase0
$ git log --oneline c38698d..HEAD
a48f9ff fix(core): align EvalHarness Protocol default with frozen contract
d419183 docs(review): cross-review of claude-code/phase0 Sem 2
a738b96 chore(pytest): disable cache provider to avoid Windows ACL issues
8424daf feat(indexing): wikilinks extractor with full Obsidian-compatible syntax + tests
d74ada9 feat(indexing): populate Chunk.line_start/line_end from mistletoe AST + tests
a9cfff6 docs(contract): amend §1.3 Chunk to add line_start / line_end for ripgrep resolution
f940dbc refactor(indexing): add Protocol conformance checks on chunker and fts5_store
f546e59 feat(indexing): SQLite FTS5 store with BM25 search and JsonIdStore migration

$ python -m ruff check src tests            # All checks passed!
$ python -m ruff format --check src tests   # 31 files already formatted
$ python -m mypy --strict src tests         # Success: no issues found in 31 source files
$ python -m pytest --no-cov -q              # 159 passed in 2.23s
```

All four gates green; test count matches Codex's report (159).

---

## Boundary crosses (process note, not blocker)

Two commits touched my territory. Per Julien's process arbitration in my
author response (`docs/reviews/sem2/codex-on-claude-code-response.md`),
the convention going forward is to flag cross-territory bugs in review
rather than commit them; both fixes are correct so no rework is required.

| Commit | File | Status |
|---|---|---|
| `a48f9ff` | `src/datacron/core/protocols.py` | Identical functional fix to my `6d7c88a` on `claude-code/phase0`. Different comment text (`# noqa: B006 - mirrors frozen contract §2.5` vs my `Protocol default is documentation, not runtime state`). Two-line conflict at merge; pick either text. |
| `a738b96` | `pyproject.toml` `addopts` | Same disable as my `25cb0a2`, comment text identical, but Codex placed the entry just before `--strict-config` while mine sits at the end of the list. Merge will need to keep one ordering. Either works. |

Heads-up for Julien at merge: there's also a **third structural merge
point** that's worth flagging — `core/protocols.py` was created by both
agents independently (my `17a55c5`, Codex's `f940dbc`). The two files
differ in:

- Comment style on the EvalHarness `noqa` line (already mentioned).
- Free-form docstring wording (paragraph order and a few word choices).
- `f940dbc` was committed at 15:54:38 (Sem 2 mid-week), `17a55c5` at
  approximately the same time on a different branch.

The functional surface (six Protocols, six `@runtime_checkable`
decorators, identical method signatures after the EvalHarness fix) is
byte-equivalent. The straightforward merge resolution is "keep one
version" — mine reads slightly more verbose in the module docstring,
Codex's is leaner. Julien's call.

No process violation — both agents needed the file to land in Sem 2 and
neither had reason to wait. Worth noting because the file is now in two
"created" states in git history, so a naive `--theirs`/`--ours` won't
choose; a manual diff-and-pick is needed.

---

## Findings on FTS5 store (`f546e59 src/datacron/indexing/fts5_store.py`)

**Status**: Approve. Strong implementation, all tests pass.

Schema matches the spec in `03-brief-codex.md` §Week 2 verbatim, plus the
two `line_start`/`line_end` UNINDEXED columns from the Sem-2 contract
amendment (a9cfff6). Snippet format uses `**`/`**` delimiters and a
`32`-token budget, matching what `mcp/tools.py::search_text` will expect
in Sem 3.

Specific things I verified:

- **Transaction safety on `upsert_note`** (lines 243-265): `BEGIN` →
  insert/update note row → delete old chunks → delete old `ulid_paths`
  row → re-insert `ulid_paths` → `executemany` for chunks → `commit`,
  with `rollback` on any exception. Replace-all semantics for chunks
  matches the protocol's "Replaces all chunks for the note_id".
- **`delete_note`** (267-279): same `BEGIN`/`commit` envelope, deletes
  all three tables, idempotent (no error on absent note). ✓
- **BM25 score inversion** (line 293): `score=-float(row["raw_score"])`
  inverts SQLite FTS5's lower-is-better into the contract's higher-is-
  better convention. ✓ Test `test_upsert_get_list_search_and_stats`
  asserts `results[0].score > 0` after inversion. ✓
- **Snippet highlighting** (lines 150-158): `snippet(chunks_fts, 6,
  '**', '**', '...', 32)` — the column-6 argument is the `content`
  column index in the virtual table; correct given the FTS5 schema in
  `_CREATE_CHUNKS_FTS_SQL` (chunk_id is 0, …, content is 6).
- **`row_factory = sqlite3.Row`** (line 225) — lets `_chunk_from_row`
  use column-name access for clarity. Nice.
- **`_chunk_from_row` reads every UNINDEXED column** without making
  extra DB calls — matches the brief's "Don't make extra DB calls".

### JsonIdStore → SQLite migration (lines 343-363)

This is the bridge from my Sem-1 `core/vault.py::JsonIdStore` to the
canonical FTS5 `ulid_paths` table. Worth a careful look:

- **Idempotency** (line 352): `if migrated_path.exists() or not
  ulids_path.exists(): return`. Two short-circuits: don't re-migrate
  when `.migrated` already exists; don't crash if no sidecar to import
  from. Tests `test_migration_no_ulids_sidecar_is_noop` and
  `test_migration_skips_when_migrated_sidecar_exists` cover both.
- **Non-destructive on the source** (line 362): the source `ulids.json`
  is **renamed** to `ulids.json.migrated`, not deleted. Good — recoverable
  if migration corrupts something.
- **Conflict policy** (line 358): `INSERT OR IGNORE` — pre-existing
  rows in `ulid_paths` (e.g., from a partial earlier run) win over the
  JSON file. Test `test_migration_conflict_uses_insert_or_ignore`
  confirms with a deliberate id conflict. The right call IMO: the SQLite
  rows are the new source of truth, the JSON is the bootstrap import.
- **`sidecar_dir = db_path.parent.parent`** (line 348) — assumes the
  layout `…/.datacron/index/datacron.db`. Matches `core.paths.sidecar_index_db`.
  Fragile if the layout ever changes (e.g., index moves elsewhere), but
  fine for Phase 0; one line to update if needed.

### Tiny nit (non-blocker)

- `_optional_str` (line 446) returns `None` when the value is an empty
  string. That matches the storage convention (we serialize `None` as
  `NULL`, never as `""`). The chunker emits `section_title=None` for
  top-level chunks, so no real ambiguity exists today. Worth a one-line
  comment so a future reader doesn't accidentally introduce empty-string
  section titles and lose them on round-trip.

---

## Findings on contract amendment (`a9cfff6 docs/agent-briefs/01-contracts.md`)

**Status**: Approve.

Adds `line_start: int = Field(ge=1)` and `line_end: int = Field(ge=1)`
to `Chunk`. Docstring clearly states 1-indexed, inclusive on both ends,
and explains the ripgrep `(file_path, line_number) → Chunk` resolution
need that motivates the addition.

The §7 Frozen-status table is correctly updated with the new amendment
date (2026-05-24 — Codex used the right week), and the prior
2026-05-22 ordinal-scope amendment is preserved as historical context.

Pre-approved in principle per `04-integration-plan.md` §5 ("Pre-known
amendment in Sem 2: Codex will likely propose adding line_start and
line_end to Chunk…"). Specification ships as advertised.

---

## Findings on chunker line-range population (`d74ada9 src/datacron/indexing/chunker.py`)

**Status**: Approve.

`_block_line_range` (lines 217-224) is the right primitive:
- Pulls `line_number` from mistletoe's block tokens (1-indexed, guaranteed by mistletoe ≥ 1.x).
- For the last block, extends `line_end = max(len(source_lines), line_start)` — covers to EOF.
- For intermediate blocks, `line_end = max(next_line_start - 1, line_start)` — non-overlapping
  by construction, with the `max(…, line_start)` guard handling pathological cases where
  two blocks share a line number (mistletoe occasionally does this for setext headings).

Tests verify the non-overlap invariant explicitly via
`test_nested_heading_line_ranges_do_not_overlap` with `pairwise`. Good
choice — a more brittle approach would have asserted exact line numbers
that depend on mistletoe internals.

### `_content_line_offset` for frontmatter-aware line numbers

This is the clever bit. `note.content` is the body-only string;
`note.raw_content` is the full file. The chunker parses `content` (so
mistletoe's line numbers are relative to the body), then shifts them by
`raw_content[:content_start].count("\n")` so the published
`line_start`/`line_end` are file-relative. `test_line_ranges_are_relative_to_raw_content`
covers this with a frontmatter prefix; passes.

One subtle case worth a comment (not a blocker): `raw_content.find(content)`
returns the **first** occurrence. If `content` were a prefix-substring
that happened to appear inside frontmatter (e.g., a YAML value that
literally contains the body's first sentence), the offset would be
wrong. Vanishingly unlikely in real notes; impossible if frontmatter is
strictly YAML mapping syntax. Worth a one-line `# assumes content
appears exactly once in raw_content` annotation for future readers.

Code blocks correctly span their full fence:
`test_code_block_line_range_spans_entire_fence` asserts
`line_end - line_start + 1 == 5` for a 5-line code block (open fence +
3 body lines + close fence). ✓

---

## Findings on wikilinks (`8424daf src/datacron/indexing/wikilinks.py`)

**Status**: Approve.

Regex (lines 25-33) handles all four documented syntax variants:

| Variant | Test | Result |
|---|---|---|
| `[[Roadmap]]` | `test_extracts_supported_syntax_variants[Roadmap]` | ✓ |
| `[[Roadmap\|Display]]` | …`[Display]` | ✓ |
| `[[Roadmap#Header]]` | …`[Header]` | ✓ |
| `[[Roadmap#^block]]` | …`[block]` | ✓ |
| `\[[Roadmap]]` (escaped) | `test_escaped_opening_brackets_are_ignored` | excluded ✓ |
| Multi-line `[[A\nB#H\nE\|D\nT]]` | `test_multiline_wikilink_normalizes_internal_whitespace` | `Project Charter` ✓ |
| Multiple occurrences | `test_multiple_occurrences_are_returned_in_order` | order preserved ✓ |

The `re.MULTILINE` flag plus the non-greedy quantifiers on every group
make multi-line wikilinks work, with `_normalize_part` collapsing
internal whitespace to single spaces and trimming. That's a sensible
canonicalization — `[[A\nB]]` and `[[A B]]` resolve to the same alias.

Pattern grouping order (`block` before `header` before `display`) is
correct: the `#^` block-ref prefix is matched first so it doesn't get
absorbed by the `#header` branch.

One micro-nit (not blocking): the regex is anchored to occurrences in
`chunk.content` only. If a chunk's content contains both an opening
`[[` and the closing `]]` on **separate paragraphs** (separated by a
blank line) — possible with the LIST chunk type — the multiline regex
will still match across paragraphs. Probably the intended behavior;
worth a comment to document the choice.

---

## Findings on Protocol conformance checks (`f940dbc src/datacron/indexing/{chunker,fts5_store}.py`)

**Status**: Approve.

The pattern Codex used differs from mine in `core/vault.py` but is
equally valid:

| | claude-code/phase0 (vault.py) | codex/phase0 (chunker.py, fts5_store.py, wikilinks.py) |
|---|---|---|
| Import | `from datacron.core.protocols import VaultReader as _VaultReaderProtocol` | same |
| Check fn | `_conformance_check(reader)` taking the Protocol param | `_conformance_check(_)` taking unused Protocol param |
| Invocation | `_assert_conformance()` (function never called) | `_conformance_check(MarkdownChunker())` at module import |

Both pass mypy strict — the structural check is in the *parameter
annotation*, not the call. Codex's invocation does instantiate the
concrete class at import time, which is harmless for parameter-less
constructors (`MarkdownChunker()`, `SQLiteFTS5Store()`,
`RegexWikilinksExtractor()`) and slightly more idiomatic in that the
check actually runs. My function-never-called variant avoids any
import-time side effect but relies on the reader trusting that mypy
will check the body without it ever running.

For consistency post-merge, Julien may want one or the other style
across the codebase. I have no strong preference; happy to refactor
vault.py's check to match Codex's pattern in a Sem 3 housekeeping
commit if that's the direction.

`RipgrepWrapper` (Sem 3) and `EvalHarness` (Sem 4) will get the same
treatment as those modules land.

---

## Approval

All four gates green on `codex/phase0`. No blocking issues across the
8 commits. The two boundary crosses are documented above and merge
cleanly with the parallel commits on `claude-code/phase0`
(`6d7c88a` and `25cb0a2`).

The three Sem-3-relevant deliverables are ready for me to consume:

1. **`FTS5Store`** — I'll wire `search_text` to `SQLiteFTS5Store.search`.
   Snippet format (`**term**`, 32-token budget) matches my sandbox
   assumption — no extra escaping needed in `wrap_vault_content`.
2. **`WikilinksExtractor`** — I'll wire `get_backlinks` to walk
   chunks via `FTS5Store.list_chunks_for_note`, run
   `RegexWikilinksExtractor().extract(chunk)` on each, then resolve
   targets via `FilesystemVaultReader.resolve_alias`.
3. **`Chunk.line_start`/`line_end`** — `RipgrepWrapper` (Codex Sem 3)
   uses these for `(file_path, line_number) → Chunk` resolution; not
   directly consumed by my MCP code, but I'll surface line ranges in
   `get_note(format=map)` if it turns out useful (only if a consumer
   asks — keeping the MVP minimal per my own Sem-2 arbitration #2).

Ready for Julien's Monday merge sequence. Cross-review on my own
`[Sem 2] MCP server + tools + resources + Protocols` PR was approved
by Codex after the EvalHarness blocker landed (`6d7c88a`).
