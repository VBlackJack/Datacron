# Reliable writes and retrieval quality

**English** | [Français](../fr/improvements.md)

## Replaying an ordinary write

All eight ordinary note-writing tools accept an optional `request_id` (1-128 ASCII
letters, digits, dots, underscores or hyphens; the first character must be alphanumeric).
Generate a unique identifier per logical operation and preserve **every argument**, including
`expected_hash`, when retrying. The key is scoped to the vault, across tools and clients.
Changing the payload or target while reusing the key returns `WriteConflictError`.

```json
{
  "rel_path": "_memory/example.md",
  "heading": "Journal",
  "entry": "Verified milestone.",
  "request_id": "milestone-20260905-001"
}
```

The existing journal stores hashes of the key and canonical arguments, not their raw content.
Recovery and key verification run under the cross-process mutation lock, before CAS and before
the mutation callback. A pending transaction that never wrote the note can be attempted again;
a recovered committed transaction returns its receipt without repeating the edit.

The initial keyed success includes `operation_id`, `committed=true`, `replayed=false`,
`rel_path`, `content_hash` and `indexed=true`, plus the ordinary tool-specific result.
A replay returns the common receipt with `replayed=true` and `indexed=false`; it does not
reconstruct tool-specific results or claim the index is current. Its hash identifies the
**historical commit**, even if later operations changed or removed the note. Read the note again
before using a hash for a new CAS operation. Current confinement and writable-policy checks
still apply, so a receipt does not grant access to a path that is now forbidden.

Use `get_note_history(note="_memory/example.md", request_id="milestone-20260905-001")`
to query a committed receipt without writing. No match means no committed receipt was found,
not proof that an interrupted pending transaction wrote nothing: recover first. Keeping the
operation journal is required for replay protection; do not delete it to retry an operation.
Calls without a key keep their existing behavior. Organization batches retain their separate
manifest/token protocol. Existing clients need a server restart to discover the new schemas.

## Targeted indexing

Ordinary writes refresh only their target, verify the committed hash, and invalidate aliases.
`indexed=true` describes that note, not the health of the entire vault. The refresh deliberately
does not advance the last global-sweep timestamp. Read repair, `datacron index`, `datacron reindex`
and health checks retain their vault-wide responsibilities. An unrelated malformed note no
longer causes an ordinary write to return an index error; a failure indexing the actual target
still returns `committed_index_incomplete`. A concurrent edit before refresh also refuses that
acknowledgement rather than claiming to have indexed the committed bytes.

Measure synthetic warm writes and global reconciliation independently:

```text
uv run --frozen python scripts/benchmark_writes.py --sizes 100 1000 5000 --repeats 5
```

The command creates temporary vaults and prints JSON with raw samples, medians, package and
host versions. These are separate operations, not a claim of end-to-end speedup on your vault.

## One Markdown selector

Maps, chunks and writers share the parser's heading text identity: inline formatting is
removed, closing ATX hashes are normalized, and Setext headings include their underline.
Headings inside code, blockquotes and lists are not addressable top-level sections.
Multiline Setext titles use the same concatenated text as existing chunk identities.
`heading_occurrence` counts matching headings after level filtering in document order; its
existing exact-hash requirement remains. An ambiguous `append_journal` refuses to choose.
Rename and delete still refuse H1. Untouched suffix bytes preserve uniform LF/CRLF and BOM;
mixed endings retain the existing dominant-EOL normalization policy.

## Reproducible evaluation and delivery

`tests/fixtures/retrieval_quality/` contains a synthetic corpus and 44 questions: English,
French, ambiguous and disambiguated queries, freshness, excluded paths and missing answers,
plus hard cases: a note titled after the subject against passing mentions, a section heading
never repeated in its body, the same heading in two project notes, bilingual queries without
configured expansion, a backlog index and an archive note repeating the subject, and an
`invalid_at` note behind its replacement. One distractor case is expected to stay imperfect
until archive demotion exists; it documents that gap rather than hiding it.

Two thresholds are deliberately not raised further. A freshness question asks a bare term that
five notes carry identically, so BM25 scores them equal to the digit and their order falls to
insertion; pinning MRR above 0.94 would only pin that tie-break. The filler notes that push a
demoted note out of the top five are sized to the window for the same reason: a filler set
larger than the window makes the positive expectation depend on the tie-break too.

`expected_empty: true` cannot coexist with expected paths/chunks. Empty cases have a separate
`empty_accuracy`, and are excluded from aggregate positive recall, MRR, nDCG and precision.
Per-question results retain categories, latency and payload tokens. Baseline comparisons reject
regressions or disappearance of previously measured empty-answer and forbidden-path metrics.

```text
uv run --frozen --extra dev pytest tests/integration/test_retrieval_quality.py
```

This exercises public MCP tool serialization, not a model's choice to call the tool or a
production-vault benchmark. The small-model campaign remains separate. The historical
19-question measurements in the README remain dated observations, not results for this corpus.

Both publication workflows call the same CI workflow: Python 3.11-3.13 on Windows and Ubuntu,
coverage, invariants, dependency audit and ShellCheck. The aggregate `Quality gate` succeeds
only when every dependency succeeds. Repository required-check rules must require that context;
workflow files alone cannot enforce branch protection. No release is triggered by these changes.

## Scoped search and heading context

`search_text` accepts three optional scope filters with exactly the `list_notes` semantics:
`folder` (prefix on a folder boundary, confined to the vault), `tags` (every listed tag must be
present, compared case-insensitively) and `frontmatter` (top-level key/value pairs, at most
eight, case-insensitive, list values match on any element). The response echoes the applied
filters under `filters`, and the MCP structured payload materializes that key as `null` when
no filter narrowed the search, exactly as it does for every other optional key. The OR fallback
for multi-term queries runs inside the same scope, so a narrowed search never leaks results
from outside it.

```json
{
  "query": "backlog",
  "folder": "_memory/projects",
  "tags": ["memory/project"],
  "frontmatter": {"confidence": "high"}
}
```

Each indexed chunk also carries a `context` column: the note title, then the heading trail
above the chunk, joined by ` / `. BM25 weighs that column three times the chunk body
(`SEARCH_CONTEXT_WEIGHT` and `SEARCH_CONTENT_WEIGHT`). A note titled after a subject therefore
outranks a note that only mentions it, and a section heading is searchable even when its body
never repeats the words. Diacritics are folded on both sides, as for the body.

The heading trail starts at the note's H1 and a title is usually resolved from that same H1,
so the title is written once, not twice. Writing it twice would apply an undeclared weight
multiplier to exactly the notes whose H1 restates their frontmatter title, and leave the notes
whose H1 differs without it. The emphasis on titles belongs in `SEARCH_CONTEXT_WEIGHT`, where
it applies to every note equally and can be read off the configuration.

A writable open of an index created before this column renames the legacy table, recreates it
with the column and refills it from its own rows joined with the indexed titles, inside one
transaction. Chunk identities, content hashes, ordinals and line ranges are copied verbatim, so
existing `chunk_id` references, CAS hashes and follow-up projections stay valid. A certified
read-only open never migrates: it detects the missing column and keeps serving unweighted BM25
until `datacron reindex` rebuilds the index.

On the versioned retrieval corpus (44 questions over 24 notes, tool pipeline), the branch moves
MRR from 0.922 to 0.950 and nDCG@10 from 0.944 to 0.964, with note recall@5, empty-answer
accuracy and forbidden-path violations unchanged at 1.0, 1.0 and 0. The integration test fails
below recall@5 0.99, MRR 0.94 or nDCG@10 0.95, so a ranking regression cannot pass silently.

`session_context` applies the same scope to its subject search: when a domain maps to a memory
tag, the bounded candidate list is built only from notes carrying that tag, instead of being
filled by notes the domain filter would discard afterwards.

## Grouped results, context excerpts and indexed frontmatter filters

`search_text(group_by_note=true)` keeps the best-ranked chunk of every note after temporal
re-ranking and before the result limit, and adds `note_matches` to each surviving hit. Notes
keep their relative order; the response carries `grouped_by_note: true`. On the versioned
corpus the same 44 questions return 15929 tokens grouped against 26382 flat, for 111 hits
instead of 199, measured on the tool implementation the eval harness itself calls. Grouping is opt-in: clients that iterate chunks keep the flat shape.

`note_matches` counts the matching chunks of that note, not the matching chunks the response
happened to carry. The ranked list is a bounded overfetch window, so counting its rows would
report a different number for the same note at every `limit`; the count is asked of the index
directly, under the same scope and the same AND/OR tiers as the search itself.

Excerpts prefer the chunk body. When the body carries no highlighted term and the context
column does, the excerpt is taken from the note title and heading trail, so a note found by
its title shows `Projet Datacron / **Statut**` instead of an unrelated first sentence. A
legacy index without the context column keeps the body excerpt.

Whether a column matched is decided on private markers, not on the public `**` decoration.
FTS5 returns a column's leading tokens when nothing matched in it, and `**` is also ordinary
Markdown emphasis, so a bolded body would otherwise look like a match and suppress the very
excerpt this feature exists to produce. A context excerpt also carries its own undecorated
source for redaction: the secret guard compares against the chunk body, and a body comparison
can never see a secret carried by a title or a heading.

The `frontmatter` scope filter is answered by a `note_frontmatter` table of casefolded
key/value pairs, one row per scalar or list element, produced by the same flattening rule as
the in-memory comparison and derived from the same serialized metadata. That last point is not
a detail: `list_notes` matches against the JSON round trip, where an unquoted YAML timestamp
has already become its ISO string, so pairs built from the live Python objects would index a
space where the other tool expects a `T`, and the two tools would disagree on one vault.

A writable open rebuilds the table from the indexed metadata, every time and without a marker.
A release that predates the table writes notes without pairs, and a one-shot marker would make
the next upgrade skip the repair forever, which is a wrong answer rather than a slow one. The
same open refills the context of any chunk still missing it, for the same reason. A certified
read-only open changes nothing and falls back to scanning note metadata, so the filter stays
exact in both modes.

The context-column migration now logs its row count and duration, and `pytest-xdist` is a
development dependency: `uv run --frozen --extra dev pytest -n auto` runs the suite in
parallel with one temporary directory per worker. Two pytest sessions started by hand in the
same checkout still share the default temporary root and must not overlap.
