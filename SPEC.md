# Datacron — Internal Vault Conventions

> **Status**: v1.1 (overlay reference, not a marketed open standard)
> **Author**: Julien Bombled
> **Last updated**: 2026-05-17
> **License**: [Apache 2.0](LICENSE)
> **Scope**: This document describes the internal conventions Datacron uses when it
> reads metadata from a vault and when it writes new notes. It is **not a normative
> spec that other vaults must follow**. Datacron reads any Markdown folder without
> requiring migration.

---

## 1. Reading philosophy — zero migration

Datacron reads **any folder of Markdown files** without imposing structure or
frontmatter. The user's existing vault is untouched.

- No required frontmatter fields on existing notes.
- No required folder structure.
- Wikilinks `[[target]]`, hashtags `#tag`, and Obsidian-style block references
  `^block-id` are recognized when present, ignored when absent.
- YAML frontmatter is parsed if present, otherwise inferred (title from H1 or filename,
  timestamps from filesystem stat).

When a note has no stable identifier, Datacron generates a **ULID** and stores it as
side-metadata in `.datacron/`, never in the note's frontmatter.

---

## 2. The `.datacron/` sidecar

A single hidden folder at the vault root holds everything Datacron needs without
touching the user's notes:

```
my-vault/
├── .datacron/                       # gitignorable, auto-managed
│   ├── VAULT.yaml                   # vault-level metadata
│   ├── index/                       # SQLite FTS5 index + ULID side-table
│   │   └── datacron.db
│   ├── history/                     # content-addressed prior note bytes
│   │   └── <sha256>
│   ├── oplog/                       # committed and pending write evidence
│   │   ├── operations.jsonl
│   │   └── pending/
│   │       └── <operation-id>.json
│   ├── scrubber/                    # integrity checkpoint and canaries
│   │   ├── checkpoint.json
│   │   └── canaries/
│   ├── logs/                        # reserved vault-local log directory
│   └── ulids.json                   # stable IDs for notes without frontmatter IDs
├── … the user's notes, in any structure …
```

Example `.datacron/VAULT.yaml`:

```yaml
datacron_version: "0.1.0"
vault_id: 01HQXR7K9YZ8M2N3PQRSTV4WX5
created: 2026-05-17T14:32:06+02:00
encoding: utf-8
line_endings: lf
# Optional: customize reserved folder names if the user has a different convention
folders:
  drafts: "_drafts"          # default
  journal: "_journal"        # default
  # User can map to their own structure:
  # drafts: "00-Inbox/AI-Drafts"
```

---

## 3. Filesystem as state machine

Rather than a YAML state field, Datacron uses **folder location** to track lifecycle:

| Folder (default name) | Meaning | When Datacron writes there |
|---|---|---|
| Anywhere outside reserved folders | Approved canonical knowledge | Never (read-only canonical) |
| `_drafts/` | AI-generated drafts pending human review | When AI creates a new note (post-v0.2) |
| `_journal/` | Dated notes | Optional, user's own structure |
| `_journal/agent/` | AI append-only logs (auto-applied) | When AI appends an entry (post-v0.2) |

**To promote a draft to canonical**: the user simply **moves the file out of `_drafts/`**.
No frontmatter edit required. The filesystem is the state machine.

Folder names are configurable via `.datacron/VAULT.yaml` (see §2) — the user can map
`_drafts/` to `00-Inbox/AI-Drafts/` if that fits their existing PARA/Zettelkasten layout.

---

## 4. Frontmatter Datacron writes (v0.2+, when write tools arrive)

When Datacron itself creates a note (post-v0.2 — out of MVP scope), it writes a
**minimal** frontmatter:

```yaml
---
id: 01HQXR7K9YZ8M2N3PQRSTV4WX5
title: "Synthesis: Kafka adoption risks"
created: 2026-05-17T16:00:00+02:00
origin: ai                      # ai | human | imported
audit_run_id: 2026-05-17T15-58-12Z_a3c2
---
```

That's it. No `status`, no `trust_level` exposed in the frontmatter — those live in
the audit log and policy engine.

**Existing notes are never retroactively normalized.** Datacron ships no normalization command.

---

## 5. Trust model — 3 user-facing states

Internally the policy engine supports a 6-level lattice (L0-L5), but the user only sees
**three categories**:

| Category | Backend levels | UX behavior |
|---|---|---|
| **auto-create** | L0, L1 | AI creates without friction (e.g. journal append, new draft) |
| **review-patch** | L2, L3 | Diff shown to user, single approve action required |
| **dangerous** | L4, L5 | Double confirmation + Git tag + audit entry |

A note marked with frontmatter key `important: true` is automatically escalated to
**dangerous** category when AI tries to modify it.

In MVP (v1, read-only), this entire model is dormant — Datacron writes nothing.

---

## 6. Wikilinks and references

Datacron recognizes Obsidian-compatible wikilink syntax when present:

```markdown
[[target-note]]
[[target-note|display text]]
[[target-note#Header]]
[[target-note#^block-ref]]
```

Resolution order:
1. Exact `title` frontmatter match
2. Filename match (without `.md`)
3. `aliases` frontmatter match
4. ULID match (advanced, when prefixed with `@`)

Ambiguous resolutions are **flagged**, never silently resolved.

---

## 7. Chunk identifiers (retrieval)

For retrieval indexing, Datacron uses deterministic chunk IDs:

```
{note_id}::{header_slug_path}::{ordinal}
```

Where:
- `note_id` is the ULID (generated and stored in `.datacron/index/`)
- `header_slug_path` is the slash-joined slug of parent headings
- `ordinal` is a zero-padded 4-digit index within the section

Example: `01HQXR7K9YZ8M2N3PQRSTV4WX5::architecture/chunking::0003`

---

## 8. Audit log

Committed write operations append one JSON object per line to
`.datacron/oplog/operations.jsonl`. Format version 2 records contain `prev_hash`, the
SHA-256 hash of the preceding canonical JSONL record (`null` for the first record),
forming a tamper-evident chain. Existing unversioned journals are migrated to version
2 in one durable pass before their next append. Subsequent appends write and fsync only
the new record; `audit_query` performs full-chain verification when it reads the
journal.

```json
{
  "format_version": 2,
  "operation_id": "01J00000000000000000000042",
  "timestamp": "2026-07-12T12:00:00.000000+00:00",
  "prev_hash": null,
  "op": "patch_section",
  "tool": "patch_note_section",
  "note_id": "01J00000000000000000000043",
  "rel_path": "notes/example.md",
  "before_hash": "<sha256>",
  "after_hash": "<sha256>",
  "actor": "mcp-client",
  "parameters": {"heading": "Example"},
  "history_stored": true
}
```

---

## 9. Compatibility commitments

- **Forward compatibility** — Datacron preserves all unknown frontmatter keys; never deletes user data.
- **Backward compatibility** — A vault with zero Datacron frontmatter is fully supported.
- **Obsidian compatibility** — All Obsidian default vault formats work without modification (wikilinks, callouts, embeds, tags, blocks).
- **Logseq / Foam / VSCode** — Same. Markdown is Markdown.

---

## 10. Versioning

Datacron conventions follow semantic versioning at the `.datacron/VAULT.yaml#datacron_version`
field. Datacron refuses to operate on a vault declaring a higher major version than it supports.

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-05-17 | Initial draft (deprecated, was overly normative) |
| 1.1 | 2026-05-17 | Pivot to overlay reference, filesystem-as-state-machine, no migration required |

---

## 11. Future evolution

This document may **eventually** be extracted as a public standard if community demand
emerges (third-party implementations for Logseq, Neovim, etc.). For now, it remains an
internal reference. Marketing Datacron's storage layer as an open standard before it has
proven itself in production would be premature.

---

## 12. License

This document is released under the **Apache License, Version 2.0**. Anyone may
implement, distribute, or extend Datacron-compatible tooling without restriction.

The reference implementation is [Datacron](README.md) (also Apache 2.0).
