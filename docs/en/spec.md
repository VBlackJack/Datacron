# Datacron - Public Vault and MCP Server Contract

**English** | [Français](../fr/spec.md)

> **Status**: Spec v2.0 - normative, synchronized with `main`
> **Author**: Julien Bombled
> **Date**: 2026-07-21
> **Replaces**: v1.1 (2026-05-17)
> **License**: [Apache 2.0](../../LICENSE)
> **Scope**: This document defines Datacron's observable formats, invariants, and surfaces.
> Design decisions and ADRs remain in [architecture.md](architecture.md).

The words "must", "must not", and "never" express contracts of the current implementation.
This document describes no future or aspirational behavior.

---

## 1. Vault reads and zero migration

Datacron accepts any folder containing Markdown files without requiring a migration, folder
structure, or frontmatter. Existing notes are not normalized or rewritten during reads or
indexing.

- YAML frontmatter is optional. When present and valid, Datacron parses it.
- The title comes from the `title` field, then the first non-empty H1, then the filename.
- Missing or invalid dates come from filesystem timestamps.
- Valid hashtags come from frontmatter and the Markdown body, excluding inline and fenced code.
- A note without a frontmatter identifier receives a deterministic ULID. It is kept in the
  sidecar when that sidecar is writable; in read-only mode, the same ULID is derived without a
  write. That identifier is not injected into the note.
- Invalid YAML frontmatter does not prevent a read: the whole file is treated as the Markdown
  body with empty metadata.

The Markdown vault remains the source of truth. The index and other sidecar data are derived or
operational data.

---

## 2. `.datacron/` sidecar and `VAULT.yaml`

The sidecar is located at the vault root. Its elements are created during initialization or on
demand, so each subdirectory is not guaranteed to exist before the feature that uses it.

| Path | Observable contract |
|---|---|
| `.datacron/VAULT.yaml` | Vault metadata and read settings |
| `.datacron/index/datacron.db` | SQLite FTS5 index, chunks, and index metadata |
| `.datacron/ulids.json` | Stable identifiers for notes without a frontmatter `id` |
| `.datacron/history/<sha256>` | Content-addressed prior bytes in `full` history mode |
| `.datacron/oplog/operations.jsonl` | JSONL journal of committed writes |
| `.datacron/oplog/pending/` | Recoverable manifests for in-progress writes |
| `.datacron/locks/` | Local advisory locks created during writes |
| `.datacron/scrubber/` | Integrity checkpoint and canaries |
| `.datacron/logs/` | Vault-local directory created by initialization but not selected for runtime logs by default |

Runtime logs default to `~/.datacron/logs`; `DATACRON_LOG_DIR` can select another location.

`VAULT.yaml` accepts, among others, `datacron_version`, `vault_id`, `created`, `encoding`,
`line_endings`, `history_retention_days`, `history_mode`, `folders`, `excluded_folders`,
`excluded_files`, and `query_expansion`. `line_endings` is `lf` or `crlf`; `history_mode` is
`full` or `redacted`. `datacron_version` is a provenance stamp for the build that wrote the file,
not a format compatibility gate.

The `folders` mapping is loaded as configuration metadata. It defines neither a state machine nor
write boundaries. Write boundaries come exclusively from `DATACRON_WRITE_PATHS`.

---

## 3. Memory frontmatter and lifecycle

`create_note_ai` writes a new Markdown note with the following fields. Optional fields remain
absent until given a value.

| Field | Observable contract |
|---|---|
| `id` | Canonical 26-character ULID |
| `title` | Non-empty title provided at creation |
| `created`, `updated` | ISO 8601 datetimes generated at creation; `updated` changes on a frontmatter update |
| `origin` | `ai`, `human`, or `merged` |
| `confidence` | `high`, `medium`, `low`, or `needs_verification` |
| `last_verified` | Supplied value or the current UTC date |
| `supersedes` | List of identifiers for fully replaced notes, empty by default |
| `rejected` | Optional list of at most 16 entries in `option -- reason` format |
| `tags` | List of tags supplied at creation |
| `valid_from` | Optional ISO date, validated but with no direct effect on current ranking |
| `invalid_at` | Optional ISO 8601 UTC datetime; the note becomes historical in default ranking |
| `invalidated_by` | Optional validated ULID, retained as provenance with no direct effect on current ranking |

`set_frontmatter` can change `origin`, `confidence`, `last_verified`, `supersedes`, `rejected`,
`valid_from`, `invalid_at`, and `invalidated_by`; it also updates `updated` and preserves the
Markdown body. An empty `rejected` list removes that key. Every unknown frontmatter key is
preserved during serialization.

The observable temporal ranking is conservative:

- a note referenced by `supersedes`, or carrying `invalid_at`, is demoted by default;
- `include_superseded=true` disables this historical demotion;
- `confidence=low` and `confidence=needs_verification` apply a score penalty;
- `valid_from` and `invalidated_by` are validated and retained, but alone do not change
  eligibility or score.

`important: true` does not escalate any write policy. Its only current observable effect is a
`*` marker in the `datacron://vault/map` resource.

---

## 4. Wikilinks, tags, and resolution

Datacron recognizes these wikilink forms:

```markdown
[[target]]
[[target|display alias]]
[[target#Heading]]
[[target#^block-ref]]
```

The target, alias, heading, and block reference are extracted from the wikilink. A standalone
`^block-id` in the body is not indexed as a semantic reference.

Alias resolution follows three global tiers, in this order:

1. exact frontmatter `title` match;
2. filename match without `.md`;
3. match against a frontmatter `aliases` entry.

Ambiguity within the highest matching tier produces an unresolved target and a log; Datacron
never silently selects a note. `get_backlinks` also accepts a bare canonical ULID as a direct
target. The `@` prefix is not part of the resolution contract.

---

## 5. Chunk identifiers

Indexed chunks use a deterministic identifier:

```text
{note_id}::{header_slug_path}::{ordinal:04d}
```

`note_id` is the stable note ULID, `header_slug_path` is the slugged heading path, and `ordinal`
is a four-digit integer. Example:

```text
01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003
```

Identifier stability depends on indexed content and structure. The chunk freshness contract is
defined in section 15.

---

## 6. Path semantics

Vault folders encode no imposed business state. Datacron does not interpret `_drafts`,
`_journal`, or another folder as canonical, approved, or dangerous.

Setup proposes `_memory`, `_drafts`, and `_journal` as the common write allowlist when the user
enables write tools. This list is a setup default, not a format rule: explicit configuration can
authorize other vault subdirectories.

All read and write paths are resolved and confined to the vault. Symlinks or traversals escaping
the authorized roots are rejected.

---

## 7. Operation history and audit journal

Each committed write appends one ASCII JSON object per line to
`.datacron/oplog/operations.jsonl`. Format 2 records contain `prev_hash`, the SHA-256 of the
previous canonical JSON line, or `null` for the first line. Reads through `audit_query` verify
the full chain. A legacy journal is durably migrated to format 2 before its next append.

`operation_id` is a UUID v4 rendered as 32 hexadecimal characters. `note_id` remains a ULID. A
record also contains the UTC timestamp, operation, tool, path, before and after hashes, actor,
redacted parameters, and the `history_stored` flag.

The journal append is flushed and fsynced. `pending` manifests allow an interrupted operation to
finish or reconcile without duplicating an already committed record.

---

## 8. Compatibility and format version

- A vault without Datacron frontmatter remains readable and indexable.
- Unknown frontmatter keys are preserved by supported writes.
- Raw Markdown, callouts, embeds, and other uninterpreted syntax are preserved as content. The
  specific indexed semantics are limited to headings, tags, and the wikilink forms described in
  this spec; standalone block identifiers are not resolved.
- `datacron_version` records the writing build and never blocks a vault read.
- This spec version is independent from the package CalVer.

| Spec version | Date | Change |
|---|---|---|
| 1.1 | 2026-05-17 | Previous overlay reference |
| 2.0 | 2026-07-21 | Observable contracts aligned with the `main` implementation |

---

## 9. Surface of 14 MCP tools

In standard mode, the server registers 14 tools. In certified read-only mode
(`DATACRON_READ_ONLY=true`), the five mutating tools are removed and nine tools remain exposed.

| Category | Tool | Observable contract |
|---|---|---|
| Read | `list_notes` | Paginated list, filterable by folder, tags, and top-level frontmatter |
| Read | `get_note` | Read by ULID, chunk ID, or path, in `full`, `chunk`, or `map` format |
| Read | `search_text` | FTS5 BM25 search with optionally historical temporal ranking |
| Read | `search_regex` | Ripgrep regex search with a bounded indexed fallback, filterable by glob |
| Read | `get_backlinks` | Chunks whose wikilinks target a ULID or resolved alias |
| Advisory | `contradiction_scan` | Deterministic candidates and a proposed write call; never writes |
| Operational | `get_health` | Freshness, integrity, checksum, durability, and invariant evidence |
| Write | `create_note_ai` | Creates a memory note without overwrite |
| Write | `append_journal` | Appends an entry under a heading in an existing note |
| Write | `set_frontmatter` | Changes only allowed lifecycle fields and `updated` |
| Write | `patch_note_section` | Replaces content under an existing heading while preserving its heading line |
| Write | `revert_note` | Restores exact bytes from a content-addressed history version |
| Operational | `get_note_history` | Lists committed operation metadata for a note without reading prior bytes |
| Operational | `audit_query` | Filters the committed journal by time, tool, or note without changing it |

Note bodies and snippets returned to the client are confined, redacted according to the secret
policy, and sandbox-wrapped as untrusted content. Titles, paths, tags, and other retrieval
metadata are sanitized and redacted according to the same policy.

---

## 10. Write tools, allowlist, CAS, and history

The five write tools are opt-in at the effect level:

- `DATACRON_READ_ONLY=true` removes them from the MCP surface;
- otherwise they are registered, but an empty `DATACRON_WRITE_PATHS` allowlist makes every
  target unauthorized and `policy/active` reports writes as disabled;
- each target must be inside the vault and below at least one allowlisted root;
- the durability mode must permit the write.

Each write tool accepts an optional `expected_hash`. When supplied, the write fails unless the
SHA-256 of the current bytes matches: this is compare-and-swap (CAS). Creation always refuses to
overwrite an existing file.

When a mutation targets an existing note, it stores the prior bytes by SHA-256 in
`history_mode=full`. Every committed mutation writes a pending manifest, atomically replaces or
creates the note, appends the chained journal, then removes the manifest. `redacted` mode retains
hashes and the journal but not prior bytes, so `revert_note` cannot read a historical version.
Retention defaults to 30 days and is configurable through `history_retention_days`.

After a successful MCP write, Datacron reconciles the index synchronously. The response carries
`indexed: true` only after that reconciliation.

---

## 11. Read allowlist

`DATACRON_READ_PATHS` is a list of absolute roots after expansion and resolution. In an
environment variable, entries are separated by the OS path separator.

- If the list is empty, `DATACRON_VAULT_ROOT` is the implicit read boundary.
- If the list is non-empty, the server refuses to start unless the served vault is contained in
  an allowed root.
- After startup, every read remains confined to the served vault; an escaping traversal or
  symlink is rejected.

The read allowlist grants no write permission.

---

## 12. Supported MCP clients

Setup knows exactly eight client identifiers. `user` scope is available for all eight;
`project` scope is written only when a project path is defined for that client.

| CLI ID | Display name | Configuration scopes | Format |
|---|---|---|---|
| `claude-desktop` | Claude Desktop | `user` | JSON `mcpServers` |
| `claude-code` | Claude Code | `user`, `project` | JSON `mcpServers` |
| `cursor` | Cursor | `user`, `project` | JSON `mcpServers` |
| `gemini-cli` | Gemini CLI | `user`, `project` | JSON `mcpServers` |
| `antigravity` | Antigravity | `user`, `project` | JSON `mcpServers` |
| `codex-cli` | Codex CLI | `user`, `project` | TOML |
| `windsurf` | Windsurf | `user` | JSON `mcpServers` |
| `vscode` | VS Code | `user`, `project` | JSON `servers` |

Discovery is best-effort. For Antigravity, it requires the live
`~/.gemini/antigravity` profile directory; `antigravity-ide` and `antigravity-backup` do not
count. Antigravity project scope targets `<project>/.agents/mcp_config.json`; user scope targets
`~/.gemini/config/mcp_config.json`. An empty JSON file is treated as absent configuration.
Installation and unregistration change only the `datacron` entry and preserve other servers.

---

## 13. stdio transport

The exposed server transport is MCP over stdio. `datacron mcp serve` and the `datacron-mcp`
entry point start the FastMCP stdio loop and stop when the client disconnects or the process is
interrupted.

The server opens no network listener, and the current CLI exposes no HTTP transport. A Python
installation configures the `datacron-mcp` executable without arguments; a frozen binary
configures its own executable with `mcp`, `serve` arguments. Both forms pass the vault
environment variables.

---

## 14. `strict` and `best-effort` durability

`DATACRON_DURABILITY` accepts exactly `strict` or `best-effort`; the default is `best-effort`.
Directory-entry flush capability is probed on the vault backend.

| Mode | If directory flush is supported | If it is unsupported |
|---|---|---|
| `best-effort` | Atomic write is allowed | Write is allowed with an explicit warning |
| `strict` | Atomic write is allowed | Every write is refused |

Certified read-only mode always refuses writes, regardless of durability mode. `get_health` and
`datacron://policy/active` expose the relevant effective state.

---

## 15. Freshness contract

The observable freshness contract is `freshness-contract-v1`; its calculation details are
defined in [freshness-contract-v1.md](freshness-contract-v1.md).

- A write performed by a write tool reconciles the index before returning `indexed: true`.
- Before an index-backed read, Datacron attempts a serialized incremental repair with an `mtime`
  gate and `content_hash` authority. Sweeps are spaced 30 seconds apart by default. A policy
  that forbids index mutations performs no such repair.
- Between sweeps, a read can serve the current index; `get_health` provides exact state to
  diagnose drift after an out-of-band change.
- A client-held `chunk_id` becomes stale if its parent note identity or `content_hash` no longer
  matches the index. `get_note(chunk_id)` then returns an explicit error requesting reindex and
  retry; it never silently serves a stale chunk.

A large offline change should be followed by `datacron index`. `datacron reindex` rebuilds the
index when a complete repair is required.

---

## 16. MCP resources

The server registers exactly three pull-only resources:

| URI | Type | Observable contract |
|---|---|---|
| `datacron://vault/map` | `text/markdown` | Lightweight tree with titles and tags, truncated to budget; `important: true` adds `*` |
| `datacron://vault/info` | `application/json` | Root, initialization, note count, index path and statistics, and result limits |
| `datacron://policy/active` | `application/json` | `read-only` or `read-write` mode, effective write-tool activation, and write allowlist |

`policy/active` returns empty lists for `auto-create`, `review-patch`, `dangerous`, and
`active_policies`. The L0-L5 trust engine is not exposed by the current server.

---

## 17. Documentation boundary and license

This spec is the reference for observable contracts. Internal topology, component choices, ADRs,
design security, and architectural limits are documented in [architecture.md](architecture.md);
they are not duplicated here.

This spec and the reference [Datacron](../../README.en.md) implementation are published under the
[Apache License, Version 2.0](../../LICENSE).
