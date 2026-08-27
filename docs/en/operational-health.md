---
title: Operational health, certified read-only mode, and durability policy
verified: 2026-08-11
tested_on: "Datacron MCP stdio / mcp 2.0.0 / Python 3.11.15"
---

# Operational health, certified read-only mode, and durability policy

**English** | [Français](../fr/operational-health.md)

## `get_health`

`get_health` is a read-only MCP tool intended for operator and buyer evidence. It
does not repair the index, recover pending operations, purge history, or write a
cached result.

The response contains:

- `status`, `server_version`, and the active `read_only` flag;
- `index`: completed generation counter, deterministic generation hash, latest
  stored reindex timestamp, indexed/live note counts, chunk count, exact
  consistency, stale entry count, byte-hash divergence count, and staleness
  seconds;
- `integrity`: live read-only counts for ID mismatches, broken wikilinks
  (`broken_wikilinks`) and their blocking subset (`broken_wikilinks_misdirected`),
  mixed-EOL Markdown notes, supersedes cycles, and parse errors;
- `vault_checksum`: SHA-256 rollup of sorted relative paths and byte-exact note
  content hashes;
- `durability`: filesystem backend, directory-flush support, selected mode, the
  policy/durability-only `writes_allowed` gate, whether at least one write path is configured
  (`write_paths_configured`), and whether a write can actually land
  (`effective_writes_enabled`, requiring both `writes_allowed` and a configured write path);
- `scrubber`: last completed scrub, current pass and index generation, coverage,
  checked bytes, canary state, and path/type anomaly evidence;
- `invariants`: I1 through I15 from packaged `reliability_evidence.json`.

The scan is intentionally uncached and O(number of Markdown notes). Do not poll it
as a high-frequency metrics endpoint.

### Index staleness definition

An exact indexed-to-live path, ID, and content-hash match reports `0.0`. When rows
differ, staleness is the positive difference between the newest live file mtime
and the latest stored index timestamp. A missing timestamp reports `null`. Always
inspect `consistent_with_vault` and `stale_entries`; a deleted row can be stale even
when the timestamp difference is zero.

`stale_entries` includes path additions, path deletions, and content-hash changes.
`hash_divergences` counts only paths present in both views whose stored hash differs
from the current byte-exact disk SHA-256. The numeric `generation` advances only
after a reconcile changes the complete index state; `generation_hash` remains the
deterministic rollup of indexed path, ID, and content-hash rows.

Health remains `degraded` when the index is current but the live scan finds ID
mismatches, misdirected wikilinks, mixed-EOL notes, supersedes cycles, or frontmatter
parse errors. This separates index freshness from known content-cleanup backlog.

Broken wikilinks are judged by classification, not by count. A link whose target
exists nowhere (`nonexistent`) is an intent link: some vaults use one to mark a note
that still has to be written, so it counts in `broken_wikilinks` without blocking
`healthy`. A link whose target exists under another title or alias
(`existing_under_other_title_or_alias`) is always a mistake: it counts in
`broken_wikilinks_misdirected` and keeps `degraded`. Without that split, a legitimate
writing convention pins `status` to `degraded` forever, and the only field meant to
alert becomes the field readers learn to ignore.

A scrubber anomaly is different: top-level health becomes `critical`. Scrubber
alerts come only from a direct primary-filesystem byte comparison or a configured
canary check. `get_health` never starts a scrub or repairs an anomaly; it only
reads the durable checkpoint. See [Integrity scrubber](integrity-scrubber.md) for the
execution, budget, resume, and canary contract.

### Checksum boundary

The rollup is a point-in-time signal for Markdown note bytes and paths. Comparing
it with a trusted earlier value detects alteration. It is not proof of future
durability, hardware cache behavior, attachment integrity, or protection against
an attacker who can replace both data and reference evidence.

## Repairing a note identity divergence

A note carries its identity in three places: the frontmatter `id` field, the `.datacron/ulids.json`
sidecar, and the SQLite index. `get_health` reports every disagreement in `integrity.id_mismatches`,
and any mismatch keeps `status` at `degraded`.

No MCP write tool can edit the `id` field. `set_frontmatter` writes only lifecycle fields,
`patch_note_preamble` edits the body placed before the first heading, and `datacron ops repair`
resolves blocked operations rather than identities. A single divergent note therefore pinned
`degraded` with no sanctioned way out. Two `ops` commands close that gap.

### `datacron ops inspect-id`

```text
datacron ops inspect-id --vault PATH
```

Read-only. It lists every divergence with the value recorded by each of the three sources, the
`classification`, the exact `content_hash` to copy into the repair, and the action that would
repair it. Run it first: `ops repair-id` refuses to guess the hash for you.

A `mismatch` is one note whose sources disagree. A `duplicate` is several notes claiming the same
ID; it is reported and never repaired automatically, because choosing which note keeps the ID is
an editorial decision.

### `datacron ops repair-id`

```text
datacron ops repair-id --vault PATH --rel-path NOTE.md --action adopt-index --expected-hash HASH --confirm NOTE.md
```

Every parameter is mandatory. `--confirm` repeats `--rel-path` exactly, and `--expected-hash` is a
strict compare-and-swap against the note bytes: a note that changed since the inspection is
refused, not overwritten.

`--action adopt-index` is the nominal case. The canonical ID -- SQLite, or the sidecar when the
index holds none -- is written into the frontmatter through the ordinary atomic, journaled write
path. Only `id` and `updated` change; the BOM, the body, and its line endings survive byte for
byte.

`--action adopt-frontmatter` promotes the note's own ID to canonical and realigns the sidecar and
the index instead. It leaves the note untouched, and it is refused when the frontmatter ID is not a
canonical 26-character Crockford ULID. That refusal is the point: adopting a malformed ID would
propagate the very defect the command exists to remove.

The command never generates a new ID and never accepts one typed by hand. It also fails closed
when the note has no frontmatter, when no divergence is recorded for the path, when the divergence
is a duplicate, and when `.datacron/ulids.json.migrated` still maps the path to another ID -- that
file is merged over the primary sidecar by every identity reader, so a stale entry there would
silently restore the divergence.

After the write, the live index is realigned through the same incremental reconcile the `index`
command uses, so no offline `datacron reindex` is required. The command then rescans the vault and
prints the divergence count it cleared, for example `id_mismatches: 1 -> 0`; it exits non-zero if
the count did not fall.

## Offline atomic reindex

`datacron reindex --vault PATH` builds a complete SQLite database under a unique
temporary name in the live index directory. It reads notes without writing them,
stores byte-exact content hashes, and uses the configured fence- and Bash-aware
wikilink parser. Before publication it validates exact path, ID, and content-hash
equality against the vault, checks note count and next generation, runs SQLite
`integrity_check`, and flushes the temporary database.

Publication uses one same-filesystem atomic replacement followed by a directory
flush. A failure before replacement preserves the old complete generation; a
failure after replacement exposes the new complete generation. The command fails
closed if a live `-wal` or `-shm` sidecar exists. Run it as an offline maintenance
operation with note writers quiesced and a verified `.datacron` backup outside the
vault.

## Certified read-only mode

Set:

```text
DATACRON_READ_ONLY=true
```

The live MCP registry then omits `create_note_ai`, `append_journal`, `set_frontmatter`,
`patch_note_preamble`, `patch_note_section`, `delete_note_section`, `rename_note_section`, and
`revert_note`. Direct calls also fail with `ReadOnlyModeError`.

The guarantee includes the `.datacron` sidecar: startup recovery is skipped, the
prebuilt SQLite index opens with `mode=ro&immutable=1`, and search read-repair is
disabled. FileLogger output is outside the vault and remains writable. A prebuilt
index is required; certified mode never creates one.

## Durability mode

Set one of:

```text
DATACRON_DURABILITY=best-effort
DATACRON_DURABILITY=strict
```

`best-effort` is the default. If the startup directory-flush probe is unsupported,
writes continue with a loud FileLogger warning and the existing per-write fallback.

`strict` refuses every write with `DurabilityUnavailableError` when the probe is
unsupported. Reads remain available from a prebuilt immutable index.

On Windows the probe opens the existing directory with
`FILE_FLAG_BACKUP_SEMANTICS` and calls `FlushFileBuffers`. On POSIX it opens the
directory and calls `fsync`. The probe creates no file. Success proves only that
the primitive is supported for the current filesystem, permissions, and startup
moment; every real write still performs its own directory flush.
