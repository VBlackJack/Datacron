---
title: Operational health, certified read-only mode, and durability policy
verified: 2026-08-30
tested_on: "Datacron 2026.0828.01 / MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# Operational health, certified read-only mode, and durability policy

**English** | [Français](../fr/operational-health.md)

## `get_health`

`get_health` is a read-only MCP tool intended for operator and buyer evidence. It
does not repair the index, recover pending operations, purge history, or write a
cached result.

The response contains:

- `status`, `server_version`, and the active `read_only` flag;
- `index`: completed generation counter, deterministic generation hash, latest stored per-note
  indexing timestamp exposed as `last_reindex`, indexed/live note counts, chunk count,
  path-and-content-hash consistency, stale entry count, byte-hash divergence count, and
  staleness seconds;
- `integrity`: live read-only counts for ID mismatches, broken wikilinks
  (`broken_wikilinks`) and their blocking subset (`broken_wikilinks_misdirected`),
  mixed-EOL Markdown notes, supersedes cycles, and note read, decode, or frontmatter parse errors;
- `vault_checksum`: SHA-256 rollup of sorted relative paths and byte-exact note
  content hashes, over every readable Markdown note outside hidden and build directories --
  including folders that `excluded_folders` omits from admitted reads, indexing, and the integrity
  counters, and carrying its own `notes_count` because that scope is wider than the one
  `integrity` reports on;
- `durability`: filesystem backend, directory-flush support, selected mode, the
  policy/durability-only `writes_allowed` gate, whether at least one write path is configured
  (`write_paths_configured`), and their conjunction (`effective_writes_enabled`). The conjunction
  is a policy/configuration precondition, not proof that ACLs, free space, recovery state, or a
  concrete I/O operation will allow a write;
- `recovery`: whether blocked operations require explicit repair, their count, and bounded
  content-free evidence in `detail=full` mode;
- `scrubber`: last completed scrub, current pass and index generation, coverage,
  checked bytes, canary state, and path/type anomaly evidence;
- `invariants`: I1 through I15 from packaged `reliability_evidence.json`.

The scan is intentionally uncached and O(Markdown paths + total readable Markdown bytes + indexed
rows). Do not poll it as a high-frequency metrics endpoint.

### Blocked organization batches

Before `apply_organization_manifest`, stop every Datacron client and server and make a verified,
byte-exact backup outside the vault of the affected notes and the complete `.datacron` directory.
Keep it until the apply response, index reconcile, planner oracle, and health checks are all green.

`datacron ops inspect --vault PATH` reports ordinary and organization-batch recovery blockers.
A reason beginning with `pending_batch_` belongs to a whole transaction: both actions are reported
as unavailable because the single-note `ops repair` command cannot safely resolve one member while
the rest of its batch remains pending. Stop all writers and do not delete or edit the pending
receipt, stage, operation log, or content-addressed history. Preserve a forensic copy, then restore
the complete verified pre-apply backup as one offline maintenance rollback. If no such backup is
available, stop and preserve the evidence for manual recovery; never force or quarantine only one
member. Restart Datacron, run `datacron ops inspect` again, then reconcile or reindex and verify
`get_health` before resuming writes.

### Index staleness definition

An exact indexed-to-live path and content-hash match reports `0.0`, even when the index contains
no timestamp. IDs are not part of this consistency boolean; inspect `integrity.id_mismatches` for
identity disagreements. When rows differ, staleness is the positive difference between the newest
live file mtime and the latest stored note-indexing timestamp. If that timestamp is unavailable or
there are no live-note mtimes, it reports `null`. Always inspect `consistent_with_vault` and
`stale_entries`; a deleted row can be stale even when the timestamp difference is zero.

`stale_entries` includes path additions, path deletions, and content-hash changes.
`hash_divergences` counts only paths present in both views whose stored hash differs
from the current byte-exact disk SHA-256. The numeric `generation` advances after an incremental
reconcile changes the complete index state, and after every successfully published full rebuild,
including an empty one. `generation_hash` remains the deterministic rollup of indexed path, ID, and
content-hash rows. Despite its public name,
`last_reindex` is `MAX(notes.indexed_at)`, not a reconcile-completion clock: a deletion-only pass
can advance `generation` without changing it.

Health remains `degraded` when the index is current but the live scan finds ID
mismatches, misdirected wikilinks, mixed-EOL notes, supersedes cycles, or note read, decode, or
frontmatter parse errors. This separates index freshness from known content-cleanup backlog.

Broken wikilinks are judged by classification, not by count. A link whose target
exists nowhere (`nonexistent`) is an intent link: some vaults use one to mark a note
that still has to be written, so it counts in `broken_wikilinks` without blocking
`healthy`. A link whose target exists under another title or alias
(`existing_under_other_title_or_alias`) is always a mistake: it counts in
`broken_wikilinks_misdirected` and keeps `degraded`. Without that split, a legitimate
writing convention pins `status` to `degraded` forever, and the only field meant to
alert becomes the field readers learn to ignore.

A scrubber anomaly is different: top-level health becomes `critical`. A readable checkpoint can
carry anomalies from a direct primary-filesystem byte comparison or a configured canary check. If
the checkpoint cannot be read or validated, health instead synthesizes a transient
`checkpoint_unreadable` anomaly in memory. `get_health` never starts a scrub or repairs an anomaly;
it reads the durable checkpoint when possible and reports that read failure otherwise. See
[Integrity scrubber](integrity-scrubber.md) for the execution, budget, resume, and canary contract.

### What the scan looks at

The counts under `integrity` cover the notes admitted by the current `VAULT.yaml`:
`excluded_folders` and `excluded_files` are reloaded for each scan. The long-lived reader and
index use the policy captured at server startup, so restart the server and reconcile the index
after changing those settings before expecting all three views to agree. A defect inside an
excluded folder is not reported, and `get_note` refuses such a path with `note_not_admitted`.

Exclusion is a read/admission policy, not a write ACL. Ordinary single-note tools authorize
paths independently through `DATACRON_WRITE_PATHS`; an excluded path that is also write-authorized
can still be reached by a direct ordinary mutator call. `apply_organization_manifest` is stricter:
every note source and target must also pass the live admission policy and stay inside the unchanged
live `organization.scope`. Keep write paths disjoint from excluded content when exclusion must also
mean non-writable for ordinary mutators.

`vault_checksum` is the deliberate exception. It stays exhaustive and carries its own
`notes_count`, so the two numbers differ when at least one readable Markdown note is actually
excluded. Narrowing the checksum would silently change what a comparison against an earlier
trusted value means, and a byte integrity claim that quietly changes scope is worse than no claim.

### Checksum boundary

The rollup is a point-in-time signal for Markdown note bytes and paths. The filesystem walk is not
an atomic snapshot, so use a quiescent vault for a reproducible checkpoint; concurrent edits can
mix observations from different instants. Comparing a stable result with a trusted earlier value
detects alteration. It is not proof of future durability, hardware cache behavior, attachment
integrity, or protection against an attacker who can replace both data and reference evidence.

## Repairing a note identity divergence

A note carries its identity in three places: the frontmatter `id` field, the `.datacron/ulids.json`
sidecar, and the SQLite index. `get_health` reports every disagreement in `integrity.id_mismatches`,
and any mismatch keeps `status` at `degraded`.

`set_frontmatter` writes only lifecycle fields, `patch_note_preamble` edits the body placed before
the first heading, and `datacron ops repair` resolves blocked operations rather than identities.
`revert_note` can restore exact history bytes, including an earlier `id`, but only by reversing a
recorded operation; it cannot choose or canonicalize an identity. A divergent note therefore had
no sanctioned targeted repair. Two `ops` commands close that gap.

### `datacron ops inspect-id`

```text
datacron ops inspect-id --vault PATH
```

Read-only. It lists every divergence with the value recorded by each of the three sources, the
`classification`, the exact `content_hash` to copy into the repair, and the preferred action that
passes its collision and migrated-sidecar preflight, or why no action can be suggested. Run it
first: `ops repair-id` refuses to guess the hash for you and repeats its preconditions against the
current state, so it can still refuse after an inspection when that state changes.

A `mismatch` is one note whose sources disagree. A `duplicate` is several notes claiming the same
ID; it is reported and never repaired automatically, because choosing which note keeps the ID is
an editorial decision.

### `datacron ops repair-id`

```text
datacron ops repair-id --vault PATH --rel-path NOTE.md --action adopt-index --expected-hash HASH --confirm NOTE.md
```

`--rel-path`, `--action`, `--expected-hash`, and `--confirm` are mandatory. `--vault` is optional
and falls back to `DATACRON_VAULT_ROOT`, or to the current directory only when it contains
`.datacron/VAULT.yaml`. `--confirm` repeats `--rel-path` exactly, and `--expected-hash` is a strict
compare-and-swap against the note bytes: a note that changed since the inspection is refused, not
overwritten.

`--action adopt-index` is the nominal case. When the frontmatter differs, the canonical ID --
SQLite, or the sidecar when the index holds none -- is written through the ordinary atomic,
journaled note path. When the frontmatter already carries that ID, the note is not rewritten and
only the sidecar/index sources that differ are realigned. For a rewritten note, the BOM and body
bytes are preserved when line endings are uniform. A note that mixes CRLF and LF is instead
normalized to its dominant EOL, exactly as any other structured note write normalizes it. The
frontmatter itself is re-serialized in canonical key order, so a hand-written frontmatter can come
back with more changed lines than `id` alone: a flow-style list is re-emitted in block style, and a
`T`-separated timestamp comes back with a space.

`--action adopt-frontmatter` promotes the note's own ID to canonical and realigns the sidecar and
the index instead. It leaves the note untouched, and it is refused when the frontmatter ID is not a
canonical 26-character Crockford ULID. That refusal is the point: adopting a malformed ID would
propagate the very defect the command exists to remove.

The command never generates a new ID and never accepts one typed by hand. It also fails closed
when the note has no frontmatter, when no divergence is recorded for the path, when the divergence
is a duplicate, and when `.datacron/ulids.json.migrated` still maps the path to another ID.
`JsonIdStore` gives the migrated file precedence, while the reliability scanner and index
migration prefer the primary sidecar. The refusal prevents those readers from silently disagreeing
again after the repair.

After the write, the repaired identity is realigned through the same incremental reconcile the
`index` command uses, so no offline `datacron reindex` is required for that repair. The reconcile
walks the whole vault and drops rows for notes that no longer exist, but its mtime gate trusts other
unchanged-mtime rows without rereading or hashing them. Unrelated index-only drift can therefore
persist and requires an offline reindex. The command then rescans the vault and prints the
divergence count it cleared, for example `id_mismatches: 1 -> 0`; it exits non-zero if the count did
not fall.

## Offline atomic reindex

`datacron reindex --vault PATH` builds a complete SQLite database under a unique
temporary name in the live index directory. It reads notes without writing them,
stores byte-exact content hashes, and uses the configured fence- and Bash-aware
wikilink parser. Before publication it validates exact path, ID, and content-hash
equality against the vault, checks note count and next generation, runs SQLite
`integrity_check`, and flushes the temporary database.

On Windows the command refuses before it starts when another process holds the live index open:
`os.replace` cannot replace an open file, and a running `datacron mcp serve` keeps the index open
for as long as it serves. POSIX permits replacing an open file, so no equivalent preflight exists
there and an already-open reader can remain attached to the previous file until it reopens. Stop
every MCP client and server on the vault first on every platform. The Windows check costs one file
handle; discovering the same condition at publication costs the whole rebuild.

Publication uses one same-filesystem atomic replacement and then attempts a directory flush. A
failed or unavailable flush is logged as degraded; the new generation can therefore be visible
without confirmed directory-metadata durability. A failure before replacement preserves the old
complete generation; a failure after replacement exposes the new complete generation. The command
fails closed if a live `-wal` or `-shm` sidecar exists. Run it as an offline maintenance operation
with note writers quiesced and a verified `.datacron` backup outside the vault.

## Certified read-only mode

Set:

```text
DATACRON_READ_ONLY=true
```

The live MCP registry then omits `create_note_ai`, `append_journal`, `set_frontmatter`,
`patch_note_preamble`, `patch_note_section`, `delete_note_section`, `rename_note_section`,
`revert_note`, and `apply_organization_manifest`. Direct calls also fail with `ReadOnlyModeError`.

The guarantee includes the `.datacron` sidecar: startup recovery is skipped, the
prebuilt SQLite index opens with `mode=ro`, and search read-repair is disabled. The
live reader keeps SQLite locking and change detection enabled so it can follow index
commits from another process safely. FileLogger remains active at `DATACRON_LOG_DIR`; its default
is `~/.datacron/logs`, but the setting is configurable and certified read-only mode does not ensure
that the log directory is outside the vault. A prebuilt index is required; certified mode never
creates one.

## Durability mode

Set one of:

```text
DATACRON_DURABILITY=best-effort
DATACRON_DURABILITY=strict
```

`best-effort` is the default. If the startup directory-flush probe is unsupported, mutations
governed by the write policy continue with a loud FileLogger warning and the existing per-write
fallback.

`strict` refuses those policy-governed mutations with `DurabilityUnavailableError` when the probe
is unsupported. Maintenance commands that bypass `WritePolicy.ensure_writable` are outside this
gate. Reads remain available from a prebuilt read-only index.

On Windows the probe opens the existing directory with
`FILE_FLAG_BACKUP_SEMANTICS` and calls `FlushFileBuffers`. On POSIX it opens the
directory and calls `fsync`. The probe creates no file. Success proves only that
the primitive is supported for the current filesystem, permissions, and startup
moment. VaultWriter note writes attempt their own directory flush; when that attempt is unavailable
or fails, they use a degraded target-file fsync fallback and log a warning. Maintenance paths that
bypass VaultWriter do not inherit even that behavior; `ops repair-id` currently realigns
`ulids.json` with a temporary-file replacement without an explicit file or directory flush.
