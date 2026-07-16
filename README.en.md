# Datacron

> Local MCP server to query and maintain a Markdown vault from Claude Desktop or Claude Code,
> without sending the whole vault into the context.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![MCP: local stdio](https://img.shields.io/badge/MCP-local_stdio-purple)](#mcp-tools)
[![CI](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/datacron/actions/workflows/ci.yml)

[Français](README.md) | **English**

Datacron indexes a folder of Markdown notes, exposes a local MCP server, then returns the
relevant notes or chunks to the client instead of a full dump. The vault stays an ordinary
Markdown folder: Datacron only adds a `.datacron/` sidecar for the index, logs, internal
ULIDs, history, and the operation journal.

## What is in place

| Surface | Current state |
|---|---|
| Vault reading | `list_notes`, `get_note`, resources `datacron://vault/map`, `vault/info`, `policy/active` |
| Search | SQLite FTS5/BM25, FR↔EN query expansion, temporal re-rank, `ripgrep` via `search_regex` |
| Local graph | Wikilinks and backlinks via `get_backlinks` |
| Writing | 5 confined, reversible tools, disabled by default without `DATACRON_WRITE_PATHS` |
| Index | `datacron index` incremental, `datacron reindex` full, automatic repair on read |
| Evaluation | `datacron eval` with recall@k, precision, latency, and tokens |
| Guided setup | `datacron setup`: init + index + MCP registration in one command |
| Clients | Auto-detect and register via `datacron setup --client all`: Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI, Windsurf, VS Code |
| Distribution | Windows installer (`Datacron-Setup.exe`), standalone executable (PyInstaller) with no Python required, or installation from source |

Store-level measurement on the Julien golden set (FTS5 + query expansion), excluding
temporal re-rank and the MCP tool layer; an end-to-end evaluation is planned:

```text
recall@5  0.89
recall@10 0.95
recall@20 0.95
precision 0.32
tokens    39984
```

## Installation

### Windows: one double-click installer

The easiest way on Windows: download `Datacron-Setup.exe` from the
[latest Release](https://github.com/VBlackJack/datacron/releases/latest), double-click it,
and pick your vault. No Python, no terminal, no administrator rights; Datacron registers
itself with your AI clients automatically. Full guide:
[Windows installation](docs/en/installation-windows.md).

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
- Claude Desktop or another stdio MCP client

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

Restart the client (e.g. Claude Desktop) after installation.

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
| `DATACRON_VAULT_ROOT` | current directory or `--vault` | vault served by the server |
| `DATACRON_READ_PATHS` | empty | read allowlist; the Claude Desktop installer sets it to the vault |
| `DATACRON_WRITE_PATHS` | empty | write allowlist; empty = write tools disabled |
| `DATACRON_MAX_RESULT_COUNT` | `20` | maximum number of results returned |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | token budget for search results |
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

Available write tools:

- `create_note_ai`: creates a typed Markdown note, without overwrite.
- `append_journal`: adds an entry under a heading of an existing note.
- `set_frontmatter`: updates lifecycle fields without modifying the Markdown body.
- `patch_note_section`: replaces the content under an existing heading with CAS control.
- `revert_note`: restores the exact bytes of a version kept in history.

Guarantees:

- strict confinement within `DATACRON_WRITE_PATHS`
- atomic overwrite via temporary file + `os.replace`
- content-addressed history before modifying an existing note
- `reconcile()` after write to make the note immediately searchable
- local audit log

Concurrent multi-machine mode is not supported for writes: keep a single-writer rule on the
vault.

## MCP Tools

### Reading

| Tool | Description |
|---|---|
| `list_notes` | returns a paginated list, filterable by folder and tags, with ULID, title, tags, aliases, and dates |
| `get_note` | reads a note by ULID, chunk id, or relative path, as paginated content, chunk, or heading outline |
| `search_text` | runs a BM25 search on the FTS5 index with ranked snippets and stale notes demoted by default |
| `search_regex` | runs a regex search via ripgrep and resolves the found lines to indexed chunks |
| `get_backlinks` | returns chunks whose wikilinks target a ULID or a resolved alias |

### Writing

| Tool | Description |
|---|---|
| `create_note_ai` | creates a new typed `_memory` note, confined to allowed paths, without overwrite and with a durable journal |
| `append_journal` | adds a Markdown entry under a heading, with confinement, exact history, and atomic write |
| `set_frontmatter` | updates only the lifecycle fields and the `updated` date, preserving the Markdown body |
| `patch_note_section` | replaces the content of an existing heading with CAS, exact history, and preservation of other sections |
| `revert_note` | restores a note from its content-addressed history; the operation stays durable, reversible, and audited |

### Operational

| Tool | Description |
|---|---|
| `get_health` | returns the real state of index freshness, integrity, checksum, durability, and invariants |
| `get_note_history` | lists the committed operation metadata of a note without reading historical content or modifying the journal |
| `audit_query` | queries operation metadata by period, tool, or note without modifying the journal or the vault |

### Advisory (experimental)

| Tool | Description |
|---|---|
| `contradiction_scan` | cache-only advisory report over the frozen contradiction pool; not validated on real content, uncalibrated confidence, never blocks writes, merges, health, or CI |

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

## Privacy and security

- Datacron does no telemetry.
- Datacron calls no cloud LLM.
- The MCP client, for example Claude Desktop, may send the chunks that Datacron returns to
  its provider. Datacron does not send it the full vault.
- Content returned to clients is wrapped in `<vault_content>...</vault_content>`.
- Results are bounded by count and by token budget.
- Filesystem access is confined by `DATACRON_READ_PATHS` and `DATACRON_WRITE_PATHS`.
- MCP operations are audited in the local logs.

## CLI commands

```bash
datacron setup                      # guided path: init + index + client config
datacron setup --yes                # all defaults, no prompts
datacron init /path/to/vault
datacron status --vault /path/to/vault
datacron index --vault /path/to/vault
datacron reindex --vault /path/to/vault
datacron scrub-init --vault /path/to/vault
datacron scrub --vault /path/to/vault
datacron eval --questions examples/eval-questions.example.yaml --vault /path/to/vault
datacron mcp serve --vault /path/to/vault
datacron mcp install --client claude-desktop --vault /path/to/vault
```

## Current limitations

- No vector search / embeddings: the current measurement does not justify it.
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
- [User guide](docs/en/user-guide.md)

Technical references:

- [Vault conventions (SPEC)](docs/en/spec.md)
- [Architecture and public surface](docs/en/architecture.md)
- [Settled decisions v2.1](docs/en/decisions-v2.1.md)
- [Security boundary](docs/en/security-boundary.md)
- [Integrity scrubber](docs/en/integrity-scrubber.md)
- [Operational health and durability](docs/en/operational-health.md)
- [Freshness contract](docs/en/freshness-contract-v1.md)

## Development

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
