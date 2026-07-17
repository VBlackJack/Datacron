# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases use **Calendar Versioning**: `YYYY.MMDD.XX` - UTC year, zero-padded month and day,
and a two-digit same-day build counter starting at `00` (e.g. `2026.0714.00`). Git tags are
prefixed with `v` (e.g. `v2026.0714.00`).

## [2026.0717.00] - 2026-07-17

### Added

- A memory protocol for MCP clients: server instructions covering session start,
  search-first answering, and conditional proactive persistence, plus trigger-led
  descriptions on the MCP tools, with structural test coverage.
- `datacron protocol install` and `datacron protocol uninstall` manage an idempotent,
  tagged Datacron block in supported agent instruction files; clients that honor the
  server instructions natively are skipped.
- Bi-temporal note metadata: `valid_from`, `invalid_at`, and `invalidated_by`
  frontmatter keys. Notes past `invalid_at` are demoted in search results the same way
  superseded notes are. Fully backward compatible, no vault migration required.
- Evaluation harness v2: deduplicated note- and chunk-level metrics, MRR, nDCG@10,
  forbidden-path checks for knowledge-update questions, latency percentiles, measured
  payload token counts, per-stage timing, pipeline and transport selection
  (`--pipeline store|tool`, `--transport impl|e2e`), and a versioned baseline with
  `--compare` that fails on regression.
- MCP tool annotations (read-only and destructive hints), typed output schemas, and
  structured tool errors that preserve the JSON error payload.
- `--version` on the CLI and progress reporting while indexing.

### Changed

- Note creation resolves ULIDs through the index and sidecar authorities instead of
  walking the whole vault, cutting creation cost from proportional-to-vault-size to
  roughly 16 ms on an 1800-note vault. Unreadable files are skipped with a warning.
- The write tools share a common execution and error-reporting path.
- Repair-on-read is throttled (`DATACRON_REPAIR_MIN_INTERVAL_SECONDS`, default 30
  seconds; `0` restores the previous always-repair behavior). `get_health` remains
  exhaustive. The freshness contract documents this amendment.
- Personal defaults moved out of the code base; folder exclusions are read from the
  vault configuration.
- CI workflows track current major versions of the checkout and setup-python actions.

### Fixed

- Temporal re-ranking no longer merges non-comparable BM25 scores from the strict
  (AND) and fallback (OR) search passes; result tiers are preserved through
  re-ranking. Tool-pipeline recall@5 is restored to parity with the store pipeline.

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
