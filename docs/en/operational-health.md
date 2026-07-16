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
- `integrity`: live read-only counts for ID mismatches, broken wikilinks,
  mixed-EOL Markdown notes, supersedes cycles, and parse errors;
- `vault_checksum`: SHA-256 rollup of sorted relative paths and byte-exact note
  content hashes;
- `durability`: filesystem backend, directory-flush support, selected mode, and
  whether writes are currently allowed;
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
mismatches, broken wikilinks, mixed-EOL notes, supersedes cycles, or frontmatter
parse errors. This separates index freshness from known content-cleanup backlog.

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

The live MCP registry then omits `create_note_ai`, `append_journal`,
`set_frontmatter`, `patch_note_section`, and `revert_note`. Direct calls also fail
with `ReadOnlyModeError`.

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
