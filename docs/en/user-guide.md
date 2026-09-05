---
title: User guide
verified: 2026-08-30
tested_on: "Datacron MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# User guide

**English** | [Français](../fr/user-guide.md)

This guide explains how to use Datacron day to day from Claude, once installation is done
(see the [Installation guide](setup.md)). Datacron is not an app with a UI: it is an MCP
server that your client (Claude Desktop or Claude Code) queries. You work in natural
language, and Claude calls the Datacron tools for you.

## The mental model in one minute

Datacron indexes your notes folder and, instead of sending the whole vault into the context,
returns only the relevant notes or fragments (chunks) to Claude. Concretely:

- You ask a question or request an action on your notes.
- Claude picks the right Datacron tool (search, read, write...).
- Datacron returns a bounded result (limited count and token budget), wrapped in
  `<vault_content>...</vault_content>`.
- Your notes stay ordinary Markdown files, editable by hand at any time.

## Tools, by use

### Read and search

| Tool | What it does |
|---|---|
| `session_context` | Bounded session context and versioned common protocol. |
| `prepare_follow_up` | Prepare sourced follow-up plans without writing. |
| `get_follow_up` | Latest structured follow-up revisions. |
| `list_notes` | Paginated list of notes, filterable by folder, tags, and top-level frontmatter; returns ULID, title, tags, aliases, and dates. |
| `get_note` | Reads a specific note by ULID, chunk id, or relative path; paginated content, single chunk, or heading outline. |
| `search_text` | BM25 search over the FTS5 index: ranked snippets, stale notes demoted by default. |
| `search_regex` | Literal regular-expression search via ripgrep, resolved to indexed chunks. |
| `get_backlinks` | Returns chunks whose wikilinks point to a given ULID or alias. |

For example, `list_notes(frontmatter={"confidence": "needs_verification", "origin": "ai"})`
returns notes matching both pairs. Frontmatter keys and values are matched case-insensitively;
a list value matches when any element matches. A request can contain at most eight pairs.

### Write (if enabled)

These tools only work if `DATACRON_WRITE_PATHS` is set (see
[Installation guide, §8](setup.md#8-enable-writing-optional)). Ordinary writes are confined,
atomic per file, versioned, and audited.

| Tool | What it does |
|---|---|
| `create_note_ai` | Creates a new typed note without overwriting any existing file. |
| `append_journal` | Adds an entry under an existing heading of a note. |
| `set_frontmatter` | Updates lifecycle fields without touching the Markdown body. |
| `patch_note_preamble` | Replaces or removes the preamble before the first Markdown heading, with version control. |
| `patch_note_section` | Replaces the content under an existing heading, with compare-and-set (CAS). |
| `delete_note_section` | Explicitly removes an H2-H6 section and its subtree. |
| `rename_note_section` | Renames an H2-H6 section heading without changing its content. |
| `revert_note` | Restores the exact bytes of a version kept in history. |
| `apply_organization_manifest` | Validates and then applies a local content-addressed bundle containing at least one exact note operation and/or an exact `organization` configuration replacement. |

`apply_organization_manifest` always uses two calls. `mode="validate"` authenticates the manifest
and payloads, checks every CAS, projects the organization report, and returns a
`confirmation_token` without writing. `mode="apply"` requires that exact token and revalidates the
same authenticated organization pre-state under the global lock before the first mutation. That
pre-state covers every admitted Markdown note inside `organization.scope`, the exact vault
configuration and identity sidecars, and the projected report, but not unrelated note bytes outside
the scope. Each file replacement is atomic and the transaction is crash-consistent; the different
paths do not become visible at one instant. `history_mode=full` is required at validation time.
Note sources and targets must remain inside the unchanged live `organization.scope`, pass the live
note-admission policy, and stay inside `DATACRON_WRITE_PATHS`. Only the exact
`.datacron/VAULT.yaml` target may sit outside that allowlist as a declared member, under CAS, and
only to replace the top-level `organization` mapping without changing `organization.scope`.
Datacron may also add one derived internal `.datacron/ulids.json` member, strictly limited to key
migrations required by validated moves and mechanically proven obsolete case collisions. Validate
mode exposes their count and a content-free SHA-256, both token-bound; the durable receipt retains
the exact evidence needed for recovery and index cleanup. An existing source must carry its `id`
in frontmatter;
sources whose identity exists only in the sidecar are unsupported in v1. Stop every other Datacron
client and server during this maintenance window.

The receipt distinguishes manifest operations from derived internal members. If bytes are already
durably committed but reconciliation or the planner oracle fails, status becomes
`committed_index_incomplete` or `committed_report_mismatch`: do not infer that no write occurred,
and retry the same call with the same token.

### Supervise

| Tool | What it does |
|---|---|
| `get_health` | Real state: index freshness, integrity, checksum, durability, invariants. |
| `get_note_history` | Metadata of a note's committed operations, without reading historical content. |
| `audit_query` | Queries the operation journal by period, tool, or note, read-only. |
| `contradiction_scan` | Scans for contradictions and proposes an explicit update without ever executing it. |

Three MCP resources round out these tools: `datacron://vault/map` (vault map),
`datacron://vault/info` (metadata), and `datacron://policy/active` (active policy).

When the vault declares an `organization` block, `datacron reorganize --dry-run` measures the
gap between its actual state and the declared organization. It is read-only: it measures and
applies nothing. `apply_organization_manifest` is what applies, and only after validation. See
[Vault organization](organization.md).

## How search works

`search_text` combines several signals, which is why results are not a plain "word match":

- **FTS5 / BM25** for the base lexical score.
- **FR↔EN query expansion** configured in `VAULT.yaml`: for example "sauvegarde" also
  surfaces notes that mention "backup".
- **Conservative temporal re-ranking**:
  - a note referenced in another note's `supersedes` field is strongly demoted;
  - a note carrying `invalid_at` is demoted identically;
  - `confidence: low` and `confidence: needs_verification` receive a light penalty;
  - `include_superseded=true` brings superseded or invalidated history back up.

`search_regex` stays **literal**: no query expansion, no temporal re-ranking. Use it when you
are after an exact string (an identifier, a path, a snippet of code).

Rule of thumb: `search_text` for "what was I saying about X", `search_regex` for "where did I
write exactly this string".

### Evaluating a knowledge update

By default, `datacron eval` measures the real `search_text` pipeline (re-ranking, confinement,
budget, and serialized snippets). To test that outdated information no longer pollutes answers,
create a pair: the active note declares the old note's ULID in `supersedes`, then add a question
to the private golden set:

```yaml
- id: rotation-certificats-active
  question: Quelle politique de rotation des certificats est active ?
  expected_paths:
    - securite/rotation-certificats-2026.md
  forbidden_paths:
    - securite/rotation-certificats-2025.md
```

Paths are relative to the vault and use `/`. The freshness check fails if the replaced note
appears among the first five distinct notes. Then run:

```bash
datacron eval --vault /chemin/vault --questions local/golden.yaml --save-baseline
datacron eval --vault /chemin/vault --questions local/golden.yaml --compare
```

The second command returns exit code 1 if recall@5 or nDCG@10 drops by more than 0.02. Configure
this threshold with `DATACRON_EVAL_REGRESSION_TOLERANCE`.

## Note trust states

The notes' frontmatter carries signals that Datacron honors at ranking time. The most useful
day to day:

- `confidence: low` / `confidence: needs_verification` - the note is still considered but
  slightly demoted; handy to flag a draft or an item to verify.
- `supersedes: <ULID>` - designates the replaced note, which will be strongly demoted in
  everyday searches.
- `invalid_at: <UTC datetime>` with `invalidated_by: <ULID>` - invalidates a targeted fact
  without deleting it or rewriting its history.

The result: you can keep history in the vault without polluting answers, while still being
able to recall it explicitly with `include_superseded=true`.

### Recording rejected options

Use the optional `rejected` frontmatter list to record discarded options so they are not
proposed again. Each entry uses the exact `option -- reason` format; a note can contain at most
16 entries of 300 characters each. `create_note_ai` can set the list, and `set_frontmatter`
replaces it completely; pass `rejected=[]` to remove the key.

This field is declarative in this release and does not alter retrieval or
`contradiction_scan`. `list_notes` can filter it only by matching one complete entry,
case-insensitively.

### Fact lifecycle

A fact is **active** until another note supersedes it or it receives an `invalid_at` field. For a
targeted correction, prefer `invalid_at + invalidated_by`: the note becomes **invalidated**, stays
queryable as history with `include_superseded=true`, and the replacement note may be written before
or after it. `valid_from` can state when validity began; otherwise `created` is the implicit value.

## Concrete requests

You phrase things in natural language; Claude translates them into tool calls. A few
examples:

- "What did I note about certificate rotation?"
  → `search_text` then `get_note` on the best results.
- "Show me the outline of the enterprise-deployment note."
  → `get_note(format="map")`.
- "Where did I write exactly `DATACRON_WRITE_PATHS`?"
  → `search_regex`.
- "Which notes link back to the security-boundary one?"
  → `get_backlinks`.
- "Add today's journal entry under 'Tracking' in the Datacron project note."
  → `append_journal` (requires writing enabled).
- "Set this note to confidence: low."
  → `set_frontmatter`.
- "Is the index fresh and intact?"
  → `get_health`.

## Best practices

- **Keep a single writer** on the vault: concurrent multi-machine writing is not supported.
- **Close every other client and server** before applying an organization manifest; never confirm
  a token produced for another hash or another vault state.
- **Leave writing disabled** until you need it; enable it on a targeted subfolder (`_memory`,
  for example), not on the whole vault.
- **Edit freely by hand**: Datacron repairs the index on read, so your manual changes are
  picked up automatically.
- **Check via `get_health`** after a large change rather than guessing.
- **Retire stale notes** with `supersedes` instead of deleting them: you keep traceability
  without degrading search.

## Privacy

Datacron does no telemetry and calls no cloud LLM. However, the MCP client (for example
Claude Desktop) may itself forward to its provider the chunks that Datacron returns -
Datacron never sends it the whole vault, only the relevant, bounded fragments. Details:
[Security boundary](security-boundary.md).

## Going further

- [Installation and configuration guide](setup.md)
- [Architecture and public surface](architecture.md)
- [Vault conventions (SPEC)](spec.md)
- [Documentation index](index.md)


## Retrieval and write failure guarantees

Search redaction examines undecorated source text. When it detects sensitive text, the
response uses the masked source excerpt without query highlighting; ordinary excerpts keep
highlighting. Physical chunk line coordinates account for frontmatter, LF/CRLF and UTF-8 BOM.
Reading by ULID validates the current file identity instead of trusting a stale path mapping.

Secrets spanning multiple chunks stay concealed: Datacron checks the complete parent note
and masks a fragment that contains only part of a secret. Public chunks elsewhere in the note
remain readable; the file and its hashes do not change.

Search budgets include the serialized results and their metadata/envelopes, using the project's
four-characters-per-token estimate; outer tool fields and the echoed query are separate.
`truncated_for_tokens=true` signals cropped excerpts or omitted results. Regex excerpts keep the
matching region when it fits, and frontmatter matches no longer consume the result limit.

For unusually large lines, `regex_frame_too_large` means one ripgrep JSON frame exceeded
`DATACRON_REGEX_MAX_FRAME_BYTES` (8 MiB by default). Narrow the glob, or increase this setting
deliberately for trusted input. The search stops cleanly and subsequent calls remain available.

If an ordinary note write succeeds but index reconciliation fails, the MCP result is an error
with `error.code="committed_index_incomplete"`, `error.committed=true`, `error.indexed=false`,
`error.content_hash` (the committed bytes), and a diagnostic `error.correlation_id`.
**Do not repeat the mutation.** Re-read the note, diagnose/repair the index, and verify health.
The hash records this write, not a guarantee that no later writer changed the file.
Without `request_id`, a retry with the original expected hash is rejected by CAS; omitting
the hash can duplicate an append. Failures before a confirmed commit retain their existing
error contracts. Without `request_id`, cancellation or a lost transport response requires
checking the note/history before deciding what to do. With a stable `request_id`, retry the
exact same arguments to recover the historical receipt without repeating a committed edit.


See [Reliability improvements](improvements.md) for request replay, targeted indexing, shared Markdown selection and quality gates.
