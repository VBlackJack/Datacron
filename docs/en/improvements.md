# Reliable writes and retrieval quality

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

`tests/fixtures/retrieval_quality/` contains a synthetic corpus and 32 questions: English,
French, ambiguous and disambiguated queries, freshness, excluded paths and missing answers.
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
