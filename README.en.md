# Datacron

> Local MCP server to query and maintain a Markdown vault from Claude, Codex, Gemini, or
> another stdio MCP client, without sending the whole vault into the context.

<!-- mcp-name: io.github.VBlackJack/datacron -->

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![MCP: local stdio](https://img.shields.io/badge/MCP-local_stdio-purple)](#mcp-tools)
[![CI](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml)

[Français](README.md) | **English**

## What can you do with Datacron?

Recover project context, prepare for a conversation, and keep track of commitments.
Datacron gives your assistant durable memory in readable, editable Markdown. Your notes
remain usable independently of the client you choose.

| Need | Example request to your assistant |
|---|---|
| Resume a project | “Where did we leave off? Find the decisions and next actions.” |
| Prepare a meeting | “Summarize our recent conversations and open points, with sources.” |
| Remember a person | “Who is this person, how have we interacted, and what should we follow up on?” |
| Track objectives | “Find the commitments and achievements relevant to my next review.” |
| Preserve a reliable record | “Save this decision, link it to the project, and verify that it was stored.” |

The assistant orchestrates these requests using the available tools and granted permissions.
A shared protocol guides reading, people updates, and write verification. Ambiguous identities
require clarification; storing a deadline does not schedule a reminder.
[Explore daily follow-up](docs/en/memory-discipline.md).

**Start here:** [install](#installation) · [first session](#first-session) ·
[user guide](docs/en/user-guide.md) · [MCP reference](#mcp-tools) ·
[privacy](#privacy-and-security).

## Installation

### Windows: one double-click installer

The easiest way on Windows: download `Datacron-Setup.exe` from the
[latest Release](https://github.com/VBlackJack/datacron/releases/latest), double-click it,
and pick your vault. No Python, no terminal, no administrator rights; Datacron registers
itself with your AI clients automatically. Full guide:
[Windows installation](docs/en/installation-windows.md).

### Python: from PyPI

```bash
python -m pip install datacron
datacron setup
```

### From source

From a clone of the repository:

```bash
python -m pip install -e ".[dev]"
```

Or, to install only the application:

```bash
python -m pip install -e .
```

Runtime prerequisites:

- Python 3.11+
- `ripgrep` available on the `PATH` for `search_regex`
- a folder of Markdown notes
- a supported stdio MCP client, such as Claude Desktop, Codex CLI, or Gemini CLI

## First session

1. Choose your notes folder with the installer or `datacron setup`.
2. Reconnect Datacron in your MCP client to load the tools and instructions.
3. Ask: “Find the notes for my project and summarize its status with sources.”

For memory sessions, `session_context` returns bounded context and the shared protocol.
`prepare_follow_up` prepares sourced updates; existing writers apply them according to
permissions. `get_follow_up` retrieves the latest structured revisions. Existing prose notes
remain readable and are not automatically converted.

The server operates locally. Your client may send returned excerpts to its model provider;
see [privacy and security](#privacy-and-security).

## Quick start

The easy path - one command detects your AI clients, initializes the vault, indexes it, and
registers Datacron everywhere:

```bash
datacron setup            # interactive; add --yes for all defaults
```

See the [installation guide](docs/en/setup.md) for options (`--client`, `--scope`, writing,
durability). Or step by step:

```bash
datacron init /path/to/vault
datacron index --vault /path/to/vault
datacron status --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault
```

The `mcp install` subcommand above is dedicated to Claude Desktop. For Codex CLI, Gemini CLI,
Antigravity, LM Studio, Cursor, and the other clients, use multi-client setup with
`datacron setup --client <identifier>` or auto-detection with `--client all`.

### Add to LM Studio

LM Studio 0.3.17+ has one user configuration and no project scope. The preferred command is:

```bash
datacron setup --yes --vault "VAULT_PATH" --client lmstudio --scope user
```

For a Python installation where `datacron-mcp` is on `PATH`, the equivalent read-only
configuration can also be imported with this official deeplink:

[Add to LM Studio](lmstudio://add_mcp?name=datacron&config=eyJjb21tYW5kIjoiZGF0YWNyb24tbWNwIiwiYXJncyI6W10sImVudiI6eyJEQVRBQ1JPTl9WQVVMVF9ST09UIjoiPFlPVVJfVkFVTFQ%2BIiwiREFUQUNST05fUkVBRF9QQVRIUyI6IjxZT1VSX1ZBVUxUPiIsIkRBVEFDUk9OX0RVUkFCSUxJVFkiOiJiZXN0LWVmZm9ydCJ9fQ%3D%3D)

The link imports this example. Open LM Studio's MCP editor and replace both
`<YOUR_VAULT>` placeholders before starting the server:

```json
{
  "mcpServers": {
    "datacron": {
      "command": "datacron-mcp",
      "args": [],
      "env": {
        "DATACRON_VAULT_ROOT": "<YOUR_VAULT>",
        "DATACRON_READ_PATHS": "<YOUR_VAULT>",
        "DATACRON_DURABILITY": "best-effort"
      }
    }
  }
}
```

The example does not enable write tools. CLI setup is safer for packaged installations
because it writes the actual executable path automatically.

Restart the configured client or clients after installation.

To run the server manually:

```bash
datacron mcp serve --vault /path/to/vault
```

The direct script entry used by the installer is also available:

```bash
datacron-mcp
```

`datacron-mcp` reads the vault from `DATACRON_VAULT_ROOT`.

## Configuration

`datacron init` creates `.datacron/VAULT.yaml`. That file can carry vault-local
configuration, notably query expansion:

```yaml
query_expansion:
  supervision: [monitoring]
  sauvegarde: [backup]
  restauration: [restore]
  chiffrement: [encryption]
  sécurité: [security]
  validité: [validity]
  certificat: [certificate]
```

Useful environment variables:

| Variable | Default | Role |
|---|---:|---|
| `DATACRON_VAULT_ROOT` | unset | fallback after `--vault`; the current directory is accepted only when it contains `.datacron/VAULT.yaml` |
| `DATACRON_READ_PATHS` | empty | read allowlist; client setup sets it to the vault |
| `DATACRON_WRITE_PATHS` | empty | write allowlist; empty = write tools disabled |
| `DATACRON_MAX_RESULT_COUNT` | `20` | maximum number of results returned |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | token budget for search results |
| `DATACRON_REPAIR_MIN_INTERVAL_SECONDS` | `30` | minimum interval between repair-on-read sweeps; `0` = every read |
| `DATACRON_GET_NOTE_MAX_TOKENS` | `25000` | budget for `get_note(format="full")` |
| `DATACRON_CHUNK_MAX_TOKENS` | `1024` | target maximum chunk size |
| `DATACRON_RIPGREP_PATH` | `rg` | ripgrep binary |

Path lists use the OS separator (`:` on Unix, `;` on Windows).

## Writing

Writes are deliberately OFF by default. Without `DATACRON_WRITE_PATHS`, write tools return a
clear error and create no file.

To enable writing to a specific subfolder:

```powershell
$env:DATACRON_VAULT_ROOT = "G:\_DATA"
$env:DATACRON_READ_PATHS = "G:\_DATA"
$env:DATACRON_WRITE_PATHS = "G:\_DATA\_memory"
datacron mcp serve --vault G:\_DATA
```

`datacron setup` can also apply the allowlist machine-wide (user environment
variable, opt-in) so every MCP client inherits it; default: `_memory`, `_drafts`,
`_journal`. See the [setup guide](docs/en/setup.md).

Available write tools:

- `create_note_ai`: creates a typed Markdown note, without overwrite.
- `append_journal`: adds an entry under a heading of an existing note.
- `set_frontmatter`: updates lifecycle fields and the `rejected` options list without modifying the Markdown body.
- `patch_note_preamble`: replaces or removes the Markdown preamble before the first recognized Markdown heading (ATX or Setext), with mandatory CAS control.
- `patch_note_section`: replaces the content under an existing heading with CAS control.
- `delete_note_section`: explicitly deletes an H2-H6 section (ATX or Setext) and its subtree.
- `rename_note_section`: renames only the title of an H2-H6 section (ATX or Setext).
- `revert_note`: restores the exact bytes of a version kept in history.
- `apply_organization_manifest`: validates and then applies a local content-addressed bundle
  after confirmation bound to the exact admitted organization pre-state.

Guarantees:

- strict note confinement within `DATACRON_WRITE_PATHS`; organization-batch note sources and
  targets must also stay inside the unchanged live `organization.scope` and pass the live
  note-admission policy, including exclusions
- two internal exact-CAS targets for an organization batch: `.datacron/VAULT.yaml`, only to change
  the top-level `organization` mapping without changing `organization.scope`, and
  `.datacron/ulids.json`, only when Datacron derives the key migration required by a
  `move_replace_exact`
- atomic overwrite via temporary file + `os.replace`
- content-addressed history before modifying an existing note
- synchronous `reconcile()` after a normal write; immediate searchability is guaranteed only when
  reconciliation succeeds
- local audit log
- for an organization manifest: crash-consistent recovery and atomic replacement of each file;
  simultaneous visibility across several paths is not guaranteed

Concurrent multi-machine mode is not supported for writes: keep a single-writer rule on the
vault.

For `apply_organization_manifest`, also stop every other Datacron client and server during the
maintenance window. Before applying, keep a verified byte-exact backup outside the vault of the
affected notes and the complete `.datacron` directory until every post-commit check is green. Call
`mode="validate"` first, review the bounded hashes it returns, then reuse
the exact `confirmation_token` with `mode="apply"`. The token binds the manifest and payloads, all
admitted Markdown notes inside `organization.scope`, the exact vault configuration and identity
sidecars, and the projected report. It deliberately does not bind unrelated note bytes outside
`organization.scope`. A change to any authenticated component invalidates the confirmation before
mutation. `history_mode=full` is required at validation time. If Datacron derives identity-sidecar
case-collision cleanup, also review `identity_sidecar_case_canonicalization_count` and its
content-free SHA-256 before applying; both proofs are token-bound and retained in the durable
receipt.
An existing `replace_exact` or `move_replace_exact` source must carry its `id` in frontmatter; an
identity available only from the sidecar is unsupported by this v1 schema. If the batch is already
durably committed but index reconciliation or the planner oracle fails, the response says so
explicitly (`committed_index_incomplete` or `committed_report_mismatch`) and the same call can be
retried with the same token.
An organization-batch blocker is reported by `datacron ops inspect` with a `pending_batch_` reason
and both single-note repair actions unavailable; use the full offline rollback procedure in the
operational-health guide rather than repairing or quarantining one member.

## Available capabilities

Datacron indexes a folder of Markdown notes, exposes a local MCP server, then returns the
relevant notes or chunks to the client instead of a full dump. The vault stays an ordinary
Markdown folder: Datacron only adds a `.datacron/` sidecar for the index, logs, internal
ULIDs, history, and the operation journal.

| Surface | Current state |
|---|---|
| Vault reading | `list_notes`, `get_note`, resources `datacron://vault/map`, `vault/info`, `policy/active` |
| Search | SQLite FTS5/BM25, FR↔EN query expansion, temporal re-rank, `ripgrep` via `search_regex` |
| Local graph | Wikilinks and backlinks via `get_backlinks` |
| Writing | 8 confined note tools + 1 organization batch, journaled and disabled by default without `DATACRON_WRITE_PATHS` |
| MCP transport | Python MCP SDK v2 through `MCPServer`, local stdio only; modern `2026-07-28` protocol and legacy `2025-11-25` compatibility, with no HTTP listener |
| Index | `datacron index` incremental, `datacron reindex` full, conditional repair on read |
| Organization | Optional `organization` block in `VAULT.yaml`; `datacron reorganize --dry-run` measures the gap read-only, `apply_organization_manifest` applies |
| Evaluation | `datacron eval` over the real MCP pipeline: recall@k, MRR, nDCG, freshness, latency, and payload tokens |
| Guided setup | `datacron setup`: init + index + MCP registration in one command |
| Clients | Auto-detect and register via `datacron setup --client all`: Claude Desktop, Claude Code, Cursor, Gemini CLI, Antigravity, LM Studio, Codex CLI, Windsurf, VS Code |
| Daily memory | `session_context`, `prepare_follow_up`, `get_follow_up`: bounded context, sourced follow-up, and structured state |
| Memory protocol | Shared versioned server/client contract; `protocol status` checks distribution, not model behavior |
| Distribution | Windows installer (`Datacron-Setup.exe`), standalone executable (PyInstaller) with no Python required, or installation from source |

## MCP Tools

### Reading

| Tool | Description |
|---|---|
| `session_context` | Bounded session context and versioned common protocol. |
| `prepare_follow_up` | Prepare sourced follow-up plans without writing. |
| `get_follow_up` | Latest structured follow-up revisions. |
| `list_notes` | returns a paginated list, filterable by folder, tags, and frontmatter key/value pairs, with ULID, title, tags, aliases, and dates |
| `get_note` | reads a note by ULID, chunk id, or relative path, as paginated content, chunk, or heading outline |
| `search_text` | runs a BM25 search on the FTS5 index with ranked snippets and stale notes demoted by default |
| `search_regex` | runs a regex search via ripgrep and resolves the found lines to indexed chunks |
| `get_backlinks` | returns chunks whose wikilinks target a ULID or a resolved alias |

### Writing

| Tool | Description |
|---|---|
| `create_note_ai` | creates a new typed `_memory` note, confined to allowed paths, without overwrite and with a durable journal |
| `append_journal` | adds a Markdown entry under a heading, with confinement, exact history, and atomic write |
| `set_frontmatter` | updates only the lifecycle fields, the `rejected` list, and the `updated` date, preserving the Markdown body |
| `patch_note_preamble` | replaces or removes the preamble before the first recognized Markdown heading (ATX or Setext), with mandatory CAS and suffix preservation |
| `patch_note_section` | replaces the content of an existing heading with CAS, exact history, and preservation of other sections |
| `delete_note_section` | explicitly deletes an H2-H6 section (ATX or Setext) and its subtree, with optional CAS and exact history |
| `rename_note_section` | renames the title of an H2-H6 section (ATX or Setext) without modifying its content or subtree |
| `revert_note` | restores a note from its content-addressed history; the operation stays durable, reversible, and audited |
| `apply_organization_manifest` | validates a local content-addressed bundle containing at least one exact note operation and/or an exact `organization` configuration replacement, then applies its declared members and any required derived ULID-sidecar migration under CAS; application is journaled and crash-consistent |

### Operational

| Tool | Description |
|---|---|
| `get_health` | returns the real state of index freshness, integrity, checksum, durability, and invariants |
| `get_note_history` | lists the committed operation metadata of a note without reading historical content or modifying the journal |
| `audit_query` | queries operation metadata by period, tool, or note without modifying the journal or the vault |

### Advisory (experimental)

| Tool | Description |
|---|---|
| `contradiction_scan` | live, deterministic, bounded scan of contradictions/refinements between sections; proposes and confirms an explicit CAS call read-only, without ever writing automatically |

MCP resources:

- `datacron://vault/map`
- `datacron://vault/info`
- `datacron://policy/active`

## Search

`search_text` combines several signals:

- FTS5/BM25 for the base lexical score
- FR↔EN query expansion configured in `VAULT.yaml`
- conservative temporal re-rank:
  - a note referenced in another note's `supersedes` is strongly demoted
  - `confidence: low` and `confidence: needs_verification` apply a light penalty
  - `include_superseded=true` brings historical notes back up

`search_regex` stays literal: it applies neither query expansion nor temporal re-rank.

<details>
<summary>Historical search measurements — July 17, 2026</summary>

These measurements cover 19 questions and one configuration. They are not a benchmark of
the current release or a guarantee for another vault.

Local measurement of the `tool/impl` pipeline actually received by the agent, 19 questions,
8k-token / 20-result configuration, July 17, 2026:

```text
recall@5       0.89
recall@10      0.95
recall@20      0.95
MRR            0.73
nDCG@10        0.79
latency p50    57 ms
latency p95    276 ms
payload tokens 90567
```

On this historical set, tool-level recall@5 matched the BM25 store. Use `datacron eval`
with a suitable question set to measure behavior on your own notes.

</details>

## Privacy and security

- Datacron does no telemetry.
- Datacron calls no cloud LLM.
- The MCP client, for example Claude, Codex, or Gemini, may send the chunks that Datacron
  returns to its provider. Datacron does not send it the full vault.
- Content returned to clients is wrapped in `<vault_content>...</vault_content>`.
- Results are bounded by count and by token budget.
- Filesystem access is confined by `DATACRON_READ_PATHS` and `DATACRON_WRITE_PATHS`.
- MCP operations are audited in the local logs.

## CLI commands

```bash
datacron setup                      # guided path: init + index + client config
datacron setup --yes                # all defaults, no prompts
datacron setup --client all --scope both --vault /path/to/vault
datacron setup --protocol           # also install client memory rules
datacron protocol install --client all
datacron protocol status --client all --scope user
datacron init /path/to/vault
datacron status --vault /path/to/vault
datacron index --vault /path/to/vault
datacron reindex --vault /path/to/vault
datacron scrub-init --vault /path/to/vault
datacron scrub --vault /path/to/vault
datacron reorganize --vault /path/to/vault --dry-run          # measure organization, read-only
datacron reorganize --vault /path/to/vault --dry-run --json   # stable machine-readable report
datacron eval --questions examples/eval-questions.example.yaml --vault /path/to/vault
datacron eval --questions local/golden.yaml --vault /path/to/vault --save-baseline
datacron eval --questions local/golden.yaml --vault /path/to/vault --compare --json
datacron mcp serve --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault  # Claude Desktop only
datacron unregister --client all --scope both --vault /path/to/vault
datacron protocol uninstall --client all
```

## Current limitations

- Lexical search only: no vector search or embeddings.
- No autonomous agent: the MCP client orchestrates.
- No GUI.
- No concurrent multi-machine writes.
- Client detection in `datacron setup` is best-effort (a config directory or a binary on the
  `PATH`); an install in a non-standard location may be missed and can then be configured by
  hand.

## Documentation

Full index: [docs/en/index.md](docs/en/index.md) | [Index français](docs/fr/index.md).

To get started:

- [Installation and configuration guide](docs/en/setup.md)
- [Use Datacron with Ollama](docs/en/ollama.md)
- [Frequently asked questions](docs/en/faq.md)
- [User guide](docs/en/user-guide.md)
- [Daily memory, people, and commitments](docs/en/memory-discipline.md)

Technical references:

- [Vault conventions (SPEC)](docs/en/spec.md)
- [Vault organization](docs/en/organization.md)
- [Architecture and public surface](docs/en/architecture.md)
- [Security boundary](docs/en/security-boundary.md)
- [Integrity scrubber](docs/en/integrity-scrubber.md)
- [Operational health and durability](docs/en/operational-health.md)
- [Freshness contract](docs/en/freshness-contract-v1.md)

## Development

CI runs the invariants and the entire regression suite on Linux/Python 3.12 for changes limited to the READMEs, CHANGELOG, and Markdown pages under `docs/fr/` or `docs/en/`. All other changes retain the six Linux/Windows and Python 3.11–3.13 combinations. Publications require the full matrix, as do empty or unverifiable diffs. ShellCheck, the dependency audit, and the required `Quality gate` remain active in both paths.

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy
pytest
```

## License

Copyright 2026 Julien Bombled.

Licensed under the [Apache License, Version 2.0](LICENSE).

[Reliable writes and quality gates](docs/en/improvements.md)
