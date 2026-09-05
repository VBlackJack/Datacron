# Memory discipline and daily follow-up

Datacron distributes one versioned memory contract through server instructions and supported
client files. The contract covers session orientation, continuous capture, people, projects,
meetings, professional objectives, waiting-for replies, handovers and closure.

## Start a session

Call `session_context(subject="project keywords", domain="project")`. Domains are `all`,
`project`, `people`, `meeting`, `objective`, and `review`. `note_paths` adds up to eight known
canonical paths. `DATACRON_SESSION_CONTEXT_PATHS` is a JSON list of startup paths, default
`["_memory/INIT.md"]`. `DATACRON_SESSION_NOTE_CHARS` defaults to 2400 per note.

The result contains the complete contract ID/version/hash, effective global write capability,
live source hashes, excerpts and continuation offsets. Optional subject search reads the
existing index without repairing it; candidates are not exhaustive. No subject means only
configured and explicit paths are read. Objective/review have no automatic taxonomy filter;
project/people/meeting candidates use their existing memory tags. Supply known paths when
labels differ. Missing or denied notes increase `unavailable` without exposing their content.

`max_tokens` is capped by `DATACRON_MAX_RESULT_TOKENS`. The whole JSON payload is bounded
using four characters per token, including escaping and metadata. Optional sources are omitted
before the contract is cut; an insufficient core budget returns `context_budget_too_small`.
This is an estimate, not a tokenizer-specific guarantee. Follow `next_read` and inspect sources
before concluding from a partial excerpt. Two people candidates trigger clarification; even
one candidate is not an identity confirmation. Vault text remains sandboxed data.

## Enrich people and preserve commitments

Read the existing person record before attributing an interaction. Match professional context,
not just a name. Ask a short clarification if identity is ambiguous. Preserve role changes,
dated interactions, original evidence, reciprocal commitments and next discussion points.
Reuse the existing history heading and link the original meeting; do not copy entire transcripts.

Use `prepare_follow_up(records=[...])` for structured revisions in existing canonical notes.
Each record supplies `record_id`, `revision`, `kind`, `target_path`, `target_id`, `expected_hash`,
`heading`, `source_path`, `source_hash`, `source_excerpt`, and `summary`. Kinds are `action`,
`interaction`, `decision`, `objective`, and `project_state`. Unknown `event_date`, `owner` and
`due_date` remain null. Status is `unknown`, `proposed`, `open`, `in_progress`, `waiting`,
`completed`, or `cancelled`; a proposal is not an agreed commitment.

Targets tagged `memory/contact`, paths under `people/`, and interactions require `identity_confirmed=true` and a
nonempty `identity_basis`. That is caller-supplied confirmation, not identity inference by the
server. Sources must already exist: capture a conversation/meeting through existing writers
before preparing its follow-up. Target history headings must exist exactly once at H2-H6.
Use existing creation conventions for new subjects; this tool does not create files or folders.

The preparer verifies live IDs, hashes, exact excerpts, history headings and revision chains.
It refuses detected secrets, including secret-bearing source notes, rather than silently
changing evidence. It does not establish that a summary follows logically from its excerpt,
that a caller-supplied deadline was agreed, or that identity confirmation is truthful.

Prepared plans group records by target note and provide `append_journal` arguments, a fresh
CAS hash and stable `request_id`. Apply sequentially, require `indexed:true`, then reread.
An uncertain write must retrieve/replay its original request. A CAS conflict requires rereading
and preparing remaining work again. Multi-note completion is not atomic. The preparer remains
available in read-only mode and reports `writes_enabled=false`; it never claims persistence.

Keep record IDs stable and canonical within one note. An identical revision is detected as
already recorded; different content under that revision is refused. A new revision references
the latest `previous_revision`. Semantic duplicates under different IDs or in different notes
are not automatically detected. Do not manually edit the integrity-protected revision blocks;
append a correction revision. Normal narrative sections remain editable.

## Read current state

`get_follow_up(note_paths=[...], include_closed=false)` validates stored envelopes and returns
the latest structured revision per record identity, hiding completed/cancelled records by
default. Older revisions remain in the note and history. It reports legacy notes explicitly:
unstructured prose is not parsed, and an empty result does not mean no commitments exist.
Source freshness is labelled `not_revalidated`; reread original evidence when needed.
The note's `updated` and operation history provide capture evidence; they do not replace the
record's event date. Output truncation is explicit. A malformed/tampered envelope causes refusal.

For weekly reviews, interviews, handovers and recurring meetings, collect relevant canonical
notes, read current records, then consult the dated original sources and legacy prose. Keep
observations separate from evaluations. A stored deadline is not a scheduled reminder.
No scheduler, external message sending or automatic inbox/calendar collection is added.

## Check distribution

```text
datacron protocol status --client all --scope user
datacron protocol status --client cursor --scope project --project PATH
datacron protocol install --client codex-cli --scope user
```

Status is read-only JSON: `current`, `outdated`, `missing`, `invalid`, `manual` or `unverified`.
Expected version/hash and detected block evidence are separate from `activation` and `behavior`,
which remain `unverified`. Cursor global rules need manual installation; server-only clients
cannot be certified by file inspection. Refresh owned blocks with the existing installer and
restart the server/client so cached tool catalogs and instructions are reloaded. The previous
server can still fall back to `get_note` until it is upgraded.

## Validation boundaries

`tests/fixtures/memory_discipline/scenarios.json` contains eight synthetic workflows. Integration
tests exercise preparation, existing writers, receipts and current projections. Additional tests
cover ambiguity, stale sources, long notes, hostile content, budget refusal, duplicate revisions,
partial completion, tampered history and read-only operation. These are deterministic product
tests, not live-model evaluations.

For each actual client/model version, run fresh sessions on these fixtures and after context
loss. Inspect tool traces for bootstrap, source reads, complete commitments, correct identities,
unknown dates, write receipts and rereads. Record client/model/contract versions and failures;
do not infer universal compliance from one successful run. No live client is certified by the
installation tests alone.
