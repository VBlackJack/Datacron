# Installation and configuration guide

**English** · [Français](../fr/setup.md)

This guide takes you from a folder of Markdown notes to a running Datacron server wired into
Claude Desktop or Claude Code. It complements the [README](../../README.en.md) and the
[user guide](user-guide.md).

> Datacron never modifies your notes unless you explicitly enable writing, and never sends
> anything to a cloud service. It only adds a `.datacron/` folder next to your notes.

## Guided setup (the easy path)

A single command does everything — initialize the sidecar, build the index, and wire the MCP
client:

```bash
datacron setup
```

It asks questions with sensible defaults (vault location, client, whether to enable writing,
durability, read-only), then runs `init`, indexes, and writes the client config, before
printing a summary and how to verify. Useful options:

- `datacron setup --yes` — accept every default, no prompts (unattended install).
- `datacron setup --vault PATH --client claude-desktop` — target a specific vault.
- `datacron setup --enable-write --write-path PATH` — enable writing on a subfolder (default: `<vault>/_memory`).
- `datacron setup --durability strict --read-only` — strict durability and certified read-only mode.
- `datacron setup --no-index` — skip building the index.
- `datacron setup --client claude-code` — print a ready-to-paste stdio config snippet for Claude Code.
- `datacron setup --client none` — configure the vault without writing or printing any client config.

The sections below describe the **same steps manually**, if you prefer full step-by-step
control.

## 1. Prerequisites

Before you start, make sure you have:

| Requirement | Detail |
|---|---|
| Python 3.11+ | `python --version` must report 3.11 or newer. |
| `ripgrep` | The `rg` binary must be on your `PATH`. Required for `search_regex`. |
| A Markdown vault | Any folder of `.md` files. It stays an ordinary folder. |
| An MCP client | Claude Desktop (automatic installer) or any stdio MCP client. |

Quick `ripgrep` check:

```bash
rg --version
```

If `rg` is not found, install it (`winget install BurntSushi.ripgrep.MSVC` on Windows,
`apt install ripgrep` / `brew install ripgrep` elsewhere) or point Datacron at a specific
binary with the `DATACRON_RIPGREP_PATH` variable.

## 2. Installation

From a clone of the repository:

```bash
python -m pip install -e ".[dev]"
```

To install only the application, without the development tooling:

```bash
python -m pip install -e .
```

Installation exposes two commands:

- `datacron` — the CLI (init, index, status, eval, MCP server management).
- `datacron-mcp` — the direct stdio server entry point, used by the installer.

Check that the CLI responds:

```bash
datacron --help
```

## 3. Initialize the vault

`datacron init` creates the `.datacron/` sidecar (index, logs, history, operation journal)
and a `VAULT.yaml` configuration file.

```bash
datacron init /path/to/vault
```

Expected output:

```text
Initialized Datacron vault at /path/to/vault
  sidecar:    /path/to/vault/.datacron
  config:     /path/to/vault/.datacron/VAULT.yaml
  vault_id:   01J...
```

If a `VAULT.yaml` already exists, `init` leaves it untouched; use `--force` to overwrite it.
The command creates the vault folder if it does not yet exist.

## 4. Build the index

The FTS5 index is what enables BM25 search. Build it once:

```bash
datacron index --vault /path/to/vault
```

- `datacron index` is **incremental**: it skips unchanged notes (mtime gate), re-chunks
  modified notes, and deletes vanished ones.
- `datacron reindex` rebuilds a complete index in a separate database, validates it (hash +
  SQLite), then atomically swaps it over the live index. Use it if the index looks
  inconsistent.

Note: the MCP server also repairs the index on read, so a manual `index` is strictly needed
only for the first population or after a large offline change.

## 5. Check the state

```bash
datacron status --vault /path/to/vault
```

Typical output:

```text
Datacron 0.1.0.dev0
  vault_root: /path/to/vault
  initialized: yes
  vault_id:   01J...
  created:    2026-07-14T...
  notes:      312
  index:      built (312 notes, 1450 chunks)
  log file:   /path/to/vault/.datacron/logs/datacron-20260714.log
```

If `initialized: no` appears, run `datacron init` again. If `index: not built` or `empty`,
run `datacron index` again.

## 6. Wire up an MCP client

### Claude Desktop (automatic installer)

```bash
datacron mcp install --client claude-desktop --vault /path/to/vault
```

This writes the server entry into the Claude Desktop configuration and sets the read
allowlist to the vault. **Restart Claude Desktop** for the change to take effect. Only
`claude-desktop` is supported by the automatic installer today.

To target a specific configuration file (testing, non-standard install):

```bash
datacron mcp install --client claude-desktop --vault /path/to/vault --config-path /path/config.json
```

### Claude Code or another stdio client

Clients that can launch a stdio MCP server can use either of the two entry points directly:

```bash
datacron mcp serve --vault /path/to/vault
```

or the script entry, which reads the vault from `DATACRON_VAULT_ROOT`:

```bash
DATACRON_VAULT_ROOT=/path/to/vault datacron-mcp
```

Declare this command in the client's MCP configuration. The server reads JSON-RPC messages
on stdin and replies on stdout; logs go to the FileLogger, never to stdout (reserved for the
protocol).

## 7. Environment variables

| Variable | Default | Role |
|---|---|---|
| `DATACRON_VAULT_ROOT` | `--vault` or current directory | Vault served by the server. |
| `DATACRON_READ_PATHS` | empty | Read allowlist; the installer sets it to the vault. |
| `DATACRON_WRITE_PATHS` | empty | Write allowlist; **empty = writing disabled**. |
| `DATACRON_MAX_RESULT_COUNT` | `20` | Maximum number of results returned. |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | Token budget for search results. |
| `DATACRON_GET_NOTE_MAX_TOKENS` | `25000` | Budget for `get_note(format="full")`. |
| `DATACRON_CHUNK_MAX_TOKENS` | `1024` | Target maximum chunk size. |
| `DATACRON_RIPGREP_PATH` | `rg` | ripgrep binary. |

Path lists use the OS separator: `:` on Unix, `;` on Windows.

## 8. Enable writing (optional)

Write tools are **disabled by default**. Without `DATACRON_WRITE_PATHS` they return a clear
error and create no file. To allow writing to a specific subfolder:

```powershell
$env:DATACRON_VAULT_ROOT = "G:\_DATA"
$env:DATACRON_READ_PATHS = "G:\_DATA"
$env:DATACRON_WRITE_PATHS = "G:\_DATA\_memory"
datacron mcp serve --vault G:\_DATA
```

Writing stays confined to `DATACRON_WRITE_PATHS`, atomic (temp file + `os.replace`),
content-addressed in history before any modification, and audited. Keep a **single-writer**
rule: concurrent multi-machine writing is not supported.

Details and guarantees: [Security boundary](security-boundary.md).

## 9. Integrity (optional)

To watch for silent corruption of the index and notes, initialize the integrity canaries
then run a scrub pass:

```bash
datacron scrub-init --vault /path/to/vault
datacron scrub --vault /path/to/vault
```

`scrub` is resumable and alert-only; it exits with code 2 if anomalies are detected. See
[Integrity scrubber](integrity-scrubber.md) and
[Operational health](operational-health.md).

## 10. Final verification

From your MCP client (Claude), ask for a `get_health` call: it returns the real state of
index freshness, integrity, checksum, durability, and invariants. If everything is green and
`list_notes` returns your notes, the installation is operational.

For the next steps, move on to the [user guide](user-guide.md).

## Build the standalone executable (optional)

To ship Datacron to users without Python, a standalone single-file executable (~22 MB) can be
built with PyInstaller:

```powershell
# Windows
pip install -e ".[build]"
./scripts/build_installer.ps1        # produces dist/datacron.exe
```

```bash
# Linux / macOS
pip install -e ".[build]"
./scripts/build_installer.sh         # produces dist/datacron
```

The binary bundles the full CLI (including `datacron setup`) and its packaged data; it needs no
installed Python. `dist/` and `build/` are not version-controlled. Context: ADR-017 in the
[architecture](architecture.md).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No vault root provided` | No `--vault`, no `DATACRON_VAULT_ROOT`, no `.datacron/VAULT.yaml` in the current folder. | Pass `--vault` or set `DATACRON_VAULT_ROOT`. |
| `search_regex` fails | `ripgrep` not found. | Install `rg` or set `DATACRON_RIPGREP_PATH`. |
| Write tools return an error | `DATACRON_WRITE_PATHS` empty (normal default behavior). | Set the write allowlist (section 8). |
| `index: not built` in `status` | Index never built. | `datacron index --vault ...`. |
| Claude Desktop does not see Datacron | Client not restarted after `mcp install`. | Restart Claude Desktop. |
