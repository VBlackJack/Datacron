# Installation and configuration guide

**English** | [Français](../fr/setup.md)

This guide takes you from a folder of Markdown notes to a running Datacron server wired into
Claude Desktop or Claude Code. It complements the [README](../../README.en.md) and the
[user guide](user-guide.md). For symptom-first troubleshooting, see the
[frequently asked questions](faq.md).

> Datacron never modifies your notes unless you explicitly enable writing, and never sends
> anything to a cloud service. It only adds a `.datacron/` folder next to your notes.

## Guided setup (the easy path)

A single command does everything - initialize the sidecar, wire the MCP client, and build
the index:

```bash
datacron setup
```

By default (`--client all`), it **detects every installed AI client and registers Datacron
with each**: Claude Desktop, Claude Code, Cursor, Gemini CLI, Antigravity, Codex CLI,
Windsurf, and VS Code. Each config is merged without clobbering existing servers (JSON or
TOML depending on the client). It asks questions with sensible defaults (vault location,
client, scope, writing, user-wide write environment, durability, read-only), then runs
`init`, registers the clients, indexes, and prints a per-client summary. An indexing failure
is deferred and never undoes client registration.
Useful options:

- `datacron setup --yes` - accept every default, no prompts (unattended install).
- `datacron setup --scope both` - write config at **user** and **project** scope (default); use `user` or `project` to restrict.
- `datacron setup --vault PATH --client claude-desktop` - target a single specific client.
- `datacron setup --vault PATH --client antigravity` - register Antigravity in its user and
  workspace MCP configurations.
- `datacron setup --enable-write --write-path PATH` - enable writing on one explicit subfolder; without `--write-path`, the defaults are `<vault>/_memory`, `<vault>/_drafts`, and `<vault>/_journal`.
- `datacron setup --enable-write --machine-wide-write` - also opt in to the user environment allowlist for future clients.
- `datacron setup --durability strict --read-only` - strict durability and certified read-only mode.
- `datacron setup --no-index` - skip building the index.
- `datacron setup --client claude-code` - print a ready-to-paste stdio config snippet for Claude Code.
- `datacron setup --client none` - configure the vault without writing or printing any client config.
- `datacron setup --protocol` - also install the memory protocol in detected client instructions; without this flag the unattended default is no. The Windows installer performs this step automatically.

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

- `datacron` - the CLI (init, index, status, eval, MCP server management).
- `datacron-mcp` - the direct stdio server entry point, used by the installer.

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
Datacron 2026.0714.00
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
allowlist to the vault. **Restart Claude Desktop** for the change to take effect. The
`mcp install` command targets only `claude-desktop`; to register Datacron with **every**
detected client at once, use `datacron setup` instead (see "Guided setup" above).

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

Antigravity is detected only from its live `~/.gemini/antigravity` profile. Its user MCP
configuration is `~/.gemini/config/mcp_config.json`; its project configuration is
`<vault>/.agents/mcp_config.json`. Both use the standard top-level `mcpServers` object. An
existing empty user configuration is treated as a new configuration, while non-Datacron
entries in either file are preserved.

### Install the client-side memory protocol

The MCP connection exposes the tools and already sends the standard MCP `instructions`
field. Native client rules reinforce that behavior in clients that have their own instruction
files: search first, read `_memory/INIT.md`, proactively persist confirmed durable facts, and
never persist speculation. Install the protocol in every detected client:

```bash
datacron protocol install --client all
```

You can also target `claude-code`, `cursor`, `gemini-cli`, `antigravity`, `codex-cli`,
`windsurf`, or `vscode`. Datacron automatically installs global rules for Claude Code,
Gemini CLI, Codex, Windsurf, and VS Code. Antigravity is project-scoped only:
`--client antigravity --scope project` manages the marked block in `<project>/GEMINI.md`
and does not write a user-global instruction file. Cursor still requires a paste in
**Settings > Rules** because its global user rules are only exposed through the UI. Claude
Desktop relies on the MCP server instructions.

Datacron only writes between `<!-- datacron:protocol:begin -->` and
`<!-- datacron:protocol:end -->`; reinstalling replaces that block and preserves the rest of
the file. The VS Code rule is a dedicated file with `applyTo: "**"`. Datacron refuses to
write if its addition would exceed Windsurf's 6,000-character global limit. To remove
Datacron-managed rules:

```bash
datacron protocol uninstall --client all
```

## 7. Environment variables

| Variable | Default | Role |
|---|---|---|
| `DATACRON_VAULT_ROOT` | `--vault` or current directory | Vault served by the server. |
| `DATACRON_READ_PATHS` | empty | Read allowlist; the installer sets it to the vault. |
| `DATACRON_WRITE_PATHS` | empty | Write allowlist; **empty = writing disabled**. |
| `DATACRON_MAX_RESULT_COUNT` | `20` | Maximum number of results returned. |
| `DATACRON_MAX_RESULT_TOKENS` | `8000` | Token budget for search results. |
| `DATACRON_REPAIR_MIN_INTERVAL_SECONDS` | `30` | Minimum interval between repair-on-read sweeps; `0` = every read. |
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
$env:DATACRON_WRITE_PATHS = "G:\_DATA\_memory;G:\_DATA\_drafts;G:\_DATA\_journal"
datacron mcp serve --vault G:\_DATA
```

Writing stays confined to `DATACRON_WRITE_PATHS`, atomic (temp file + `os.replace`),
content-addressed in history before any modification, and audited. Keep a **single-writer**
rule: concurrent multi-machine writing is not supported.

### Machine-wide write allowlist

The guided setup offers a separate, explicit opt-in to apply the resolved write allowlist to
the user environment. On Windows it writes `DATACRON_WRITE_PATHS` under
`HKCU\Environment` and broadcasts `WM_SETTINGCHANGE` for future processes. If a value already
exists, setup displays it and asks whether to keep or replace it; values are never merged
silently. Already-open MCP clients must still be restarted.

On Unix, setup does not edit shell dotfiles. It prints an
`export DATACRON_WRITE_PATHS=...` line to copy into the appropriate shell profile. Vaults on
UNC/network paths or in well-known synchronized folders also trigger a reminder that only one
writer may be active.

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
