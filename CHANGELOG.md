# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases use **Calendar Versioning**: `YYYY.MMDD.XX` - UTC year, zero-padded month and day,
and a two-digit same-day build counter starting at `00` (e.g. `2026.0714.00`). Git tags are
prefixed with `v` (e.g. `v2026.0714.00`).

## [2026.0716.00] - 2026-07-16

### Added

- A per-user Windows installer (`Datacron-Setup.exe`) with optional release signing and
  automatic shutdown of a running Datacron process before replacement.
- `datacron unregister` for removing Datacron from supported MCP client configurations.
- `datacron setup --reset` for a guarded, targeted reset of Datacron-managed state.

### Changed

- Frozen executables are registered directly in MCP client configurations instead of relying
  on a Python launcher.
- Guided setup registers MCP clients before indexing and reports an indexing failure without
  discarding a successful client registration.
- Frozen packaging uses the operating system certificate store through `truststore`.
- Installer guidance is available in English and French and linked from the main READMEs.

### Fixed

- Windows setup uses a portable reparse-point constant while retaining cross-platform mypy
  compatibility.
- Repository hygiene excludes local installer output from version control.

## [2026.0714.00] - 2026-07-14

### Added

- `datacron setup`, a guided end-to-end wizard that initializes the sidecar, builds the index,
  and wires an MCP client in one command, with location and option choices (interactive by
  default, `--yes` for unattended runs). Vault initialization is now shared through
  `datacron.bootstrap` between `init` and `setup`. Supports `--client claude-code`, which
  prints a ready-to-paste stdio MCP config snippet.
- Multi-client auto-detection and registration (`datacron setup --client all`, the default):
  detects installed AI clients - Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI,
  Windsurf, VS Code - and merges the Datacron MCP server into each config (JSON `mcpServers`,
  VS Code `servers` with `type`, or Codex TOML `mcp_servers`), at user and/or project scope
  (`--scope`), preserving existing entries. New `datacron.installers.mcp_clients` module.
- Standalone single-file executable build (PyInstaller) behind the optional `[build]` extra,
  with `scripts/build_installer.ps1` and `scripts/build_installer.sh`. Ships Datacron to users
  without Python (ADR-017, revising the PyPI/pipx-only distribution decision).
- `release` GitHub Actions workflow: on a `v*` tag it builds the standalone executable on
  Windows, macOS, and Linux, smoke-tests each binary, and attaches them to the GitHub Release.
- `scripts/bump_version.py`: computes the next CalVer (`YYYY.MMDD.XX`, UTC date + same-day
  counter) from `__init__.py`, so cutting a release never requires choosing a version number.
  `scripts/release.bat` wraps it for a one-click Windows release (bump, commit, tag, push,
  with a confirmation prompt).
- Fourteen MCP tools covering vault reads, lexical and regex search, confined writes,
  operational health, note history, operation-audit queries, and a cache-only
  `contradiction_scan` advisory (experimental, non-blocking).
- Content-addressed note history, durable operation evidence, integrity scrubbing, and
  byte-exact freshness contracts.
- A locked uv environment for the complete runtime and development dependency set.
- Import-purity regression coverage for core write-path and MCP modules.

### Changed

- CI installs frozen dependencies and validates Python 3.11 and 3.12 on Ubuntu and Windows.
- CI adds a `dependency-audit` job that scans locked dependencies for known CVEs (`pip-audit`).
- Vault encoding/line-ending defaults and the `datacron_version` stamp key are centralized in
  `core.config` (single source of truth shared by the writer and the `vault/info` resource).
- Durable-write primitives live in `datacron.core.durability`, removing the former
  `vault_writer` and `operation_log` import cycle.
- Logging configuration is explicit at CLI and MCP server entrypoints; importing Datacron
  modules no longer parses environment settings, creates log directories, or starts threads.

### Fixed

- Platform-specific durability and locking code now passes strict mypy checks on Linux and
  Windows.
- CLI logging teardown tolerates already-closed captured streams without suppressing unrelated
  errors.
