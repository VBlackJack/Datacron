# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases use **Calendar Versioning**: `YYYY.MMDD.XX` - UTC year, zero-padded month and day,
and a two-digit same-day build counter starting at `00` (e.g. `2026.0714.00`). Git tags are
prefixed with `v` (e.g. `v2026.0714.00`).

## [Unreleased]

### Added

- `datacron setup` now detects LM Studio from its real `~/.lmstudio` profile and merges the
  Datacron server into the user-only `~/.lmstudio/mcp.json`, preserving every other entry.
  The documentation includes an official `lmstudio://add_mcp` deeplink, while protocol
  installation deliberately excludes LM Studio because it has no documented global
  instruction file.

### Changed

- The installer write page and matching interactive setup prompts were reworded for
  non-technical users, explaining who may write, the three dedicated subfolders, what
  remains protected, and how to enable the permission later.
- The public specification was rewritten as v2.0 to match the current implementation.

### Removed

- The historical decision record was moved out of the public documentation; Git history
  remains the archive.

## [2026.0721.00] - 2026-07-21

### Added

- Interactive `datacron setup` now explains every prompted choice, its safe default, and
  the concrete effect of opting in before asking for an answer; `--yes` and explicitly
  supplied options remain quiet and script-compatible.
- Strictly matched English and French FAQs now cover vault-selection recovery, write-tool
  opt-in, Antigravity scopes, installed-version mismatches, index freshness, reset, silent
  installer switches, uninstall boundaries, and log diagnosis from current behavior.
- The Windows installer wizard now offers a **Write tools** page with two fail-safe
  opt-ins (unchecked by default): enable the confined write tools and apply the write
  allowlist to the user environment, mapped to `setup --enable-write` and
  `--machine-wide-write`. Silent installs get the matching `/ENABLEWRITE` and
  `/MACHINEWIDEWRITE` switches.
- `datacron setup` now detects Google Antigravity from its live profile and merges the
  Datacron server into `~/.gemini/config/mcp_config.json` and
  `<project>/.agents/mcp_config.json`, accepting an empty user config and preserving all
  other entries. Its memory protocol is supported in the workspace `GEMINI.md` only.

### Fixed

- `datacron setup --yes` no longer adopts the current directory as the vault silently:
  non-interactive runs require `--vault`, `DATACRON_VAULT_ROOT`, or an existing
  `.datacron/VAULT.yaml` in the current directory, and the user profile root is always
  refused as a vault target, even when passed explicitly.

## [2026.0720.00] - 2026-07-20

### Added

- `datacron setup` can optionally apply the write allowlist machine-wide through the user
  environment (`HKCU` registry value plus settings broadcast on Windows; printed `export`
  line on Unix), defaulting to the `_memory`, `_drafts`, and `_journal` folders, so every
  MCP client on the machine inherits it.
- Memory notes can record up to 16 structured rejected options in the optional `rejected`
  frontmatter list through `create_note_ai` and `set_frontmatter`.
- `list_notes` now accepts up to eight case-insensitive top-level frontmatter key/value
  filters with AND semantics and list-element matching.

## [2026.0719.01] - 2026-07-19

### Added

- CI now auto-publishes `server.json` to the MCP Registry via GitHub OIDC after the PyPI
  publish succeeds, with a bounded PyPI-propagation wait and an idempotent registry pre-check
  (#27).

### Changed

- The memory protocol now routes contradiction and refinement detection to the
  `contradiction_scan` tool, kept distinct from consolidation (#26).
- The memory protocol ships index-freshness guidance - trust writes returning `indexed: true`,
  use `get_health` only on suspicion, and run `datacron reindex` if the index is inconsistent -
  replacing the prior `get_health` line (#28).

### Fixed

- `contradiction_scan` statements no longer truncate mid-word and now mark truncation visibly
  (#25).
- Atomic writes retry `os.replace` on transient Windows sharing errors (WinError 5/32/33),
  fixing an intermittent concurrent-write failure on Windows (#29).

## [2026.0719.00] - 2026-07-19

### Added

- The memory protocol now covers every MCP client supported by Datacron. Windsurf receives
  an always-on block in its global rules file, and VS Code receives a dedicated user-profile
  `datacron.instructions.md` rule with `applyTo: "**"`. Claude Code, Gemini CLI, Codex,
  Cursor, and Claude Desktop keep their existing native or MCP-initialization behavior.

### Changed

- The Windows installer now installs the Datacron memory protocol after registering detected
  AI clients and removes Datacron-managed instruction blocks during uninstall.

## [2026.0718.04] - 2026-07-18

### Added

- `get_health` now reports `write_paths_configured` and `effective_writes_enabled` under
  `durability`, distinguishing the write-policy gate from whether a write can actually land.
  An effective write requires both an enabling policy and at least one configured write path.
- Release binaries now ship a `SHA256SUMS` manifest and a build-provenance attestation.

### Changed

- The `datacron://policy/active` MCP resource now reports the real write policy, including
  mode, `write_tools_enabled`, and configured write paths, instead of a static read-only
  placeholder.
- `bump_version` now updates `server.json` together with `__init__.py`, and a blocking
  invariant fails CI if the package version and `server.json` drift.
- All GitHub Actions are pinned to commit SHAs, and the PyPI publish and release workflows
  are gated on the invariant suite before building or publishing.
- The regex search fallback, used only when ripgrep is unavailable, is documented as
  best-effort with an advisory timeout and rejects a broader set of catastrophic patterns.
- History purge now runs at most once per configurable interval, off the hot write path, and
  temporal retrieval metadata is cached by index generation.
- Contradiction provenance labels are sourced from configuration instead of being hardcoded.
- CI enforces a minimum coverage floor.

### Dependencies

- `pydantic` is constrained below 3.0 to guard against the known breaking major.

## [2026.0718.03] - 2026-07-18

### Changed

- Updated the MCP registry namespace marker to match the GitHub account casing
  (`io.github.VBlackJack/datacron`).

## [2026.0718.02] - 2026-07-18

### Added

- PyPI releases can be published through a dedicated Trusted Publishing workflow with a
  separate build job and a manually approved `pypi` environment. The publish job receives
  only the short-lived OIDC permission and uses no persistent PyPI credential.
- Tested and supported on Python 3.13, which is now included in the CI matrix.

### Changed

- The PyPI project description now uses the English README and carries the MCP ownership
  marker for the future `io.github.vblackjack/datacron` registry entry.
- Distribution versions use PEP 440 normalization: Git tag `v2026.0718.01` and source
  version `2026.0718.01` map to PyPI/registry version `2026.718.1`. PEP 440 removes leading
  zeroes from numeric release segments while preserving version ordering.

## [2026.0718.01] - 2026-07-18

### Added

- Cursor project rules are now a first-class protocol target. `datacron protocol
  install` and `datacron protocol uninstall` accept `--scope user|project|both`
  and `--project PATH` (defaulting to the current directory). At project scope
  for Cursor, Datacron writes a dedicated, canonically owned
  `<project>/.cursor/rules/datacron.mdc` rule (MDC frontmatter with
  `alwaysApply: true`; body is the shared protocol block). Re-installs are
  idempotent, a `datacron.mdc` without Datacron markers is refused rather than
  overwritten, and uninstall removes only a Datacron-owned rule. `setup` installs
  the project rule at the code-project root when project scope is selected,
  without conflating it with the vault root.

## [2026.0718.00] - 2026-07-18

### Fixed

- `datacron setup --client <name>` now installs the requested non-Claude MCP client
  (Gemini CLI, Cursor, Codex CLI, Windsurf, VS Code). The setup dispatcher previously
  handled only `all`, `claude-desktop`, and `claude-code`; other client identifiers fell
  through without writing a configuration or emitting a warning. Specific clients now use
  the shared detected-client installer with an explicit include filter and keep the
  requested scope; an unknown or undetected client produces an explicit warning.
- Protocol installation no longer writes an unsupported Cursor user-global rule file
  (`~/.cursor/rules/datacron.mdc`). Cursor global rules are configured through the Cursor
  UI (Settings > Rules), so Datacron returns copyable manual instructions instead and
  safely migrates Datacron-marked blocks out of the two obsolete home paths without
  removing unrelated user content.

### Changed

- The release workflow runs on Node 24 runtimes: `actions/upload-artifact@v6`,
  `actions/download-artifact@v7`, and `softprops/action-gh-release@v3`.

## [2026.0717.03] - 2026-07-17

### Added

- `contradiction_scan` accepts a `detail` parameter (`summary` by default, or `full`).
  Summary responses drop the pre-rendered blocks from alternative mutations and shorten
  evidence excerpts; `full` preserves the previous verbose payload for debugging.
  Confirmation is unaffected: the exact write-tool call, including its block, is still
  recomputed from the proposal token.
- A per-note-pair candidate cap (`contradiction_max_per_note_pair`, default 2), applied
  after deterministic ranking and before the overall candidate limit, so a single pair of
  notes can no longer dominate scan results.
- A configurable summary evidence length (`contradiction_summary_evidence_chars`,
  default 160).

### Changed

- Scan results default to the compact `summary` payload. On a real vault this roughly
  halves the response size while preserving candidate identity and ordering.
- A suggested `CONTRADICTION` between notes with disjoint `project/` tags and no explicit
  temporal ordering is downgraded to an open question. All classification options remain
  available at confirmation, so a human can still choose contradiction.

## [2026.0717.02] - 2026-07-17

### Fixed

- `contradiction_scan` results no longer fail structured-output validation: optional
  output keys are nullable, matching how absent keys are serialized before the
  protocol-level schema check. Scan calls now return their payload on every client.
  An end-to-end regression test exercises the tool through the full validation layer.

## [2026.0717.01] - 2026-07-17

### Added

- A live contradiction scan (`contradiction_scan`, `schema_version: 2`): deterministic,
  bounded, index-driven detection of potentially conflicting sections across notes, with
  per-candidate classification (contradiction, refinement, or open question), evidence
  excerpts from both sides, and conservative defaults. The scan is strictly read-only.
- Stateless, content-addressed mutation proposals with idempotent confirmation:
  confirming a proposal returns the exact write-tool call to execute (expected hash
  included). The client performs the mutation through the existing write tools, and a
  replayed write is rejected by the compare-and-swap check.
- Typed elicitation for classification and scope on clients that support it, with a
  token-based confirmation fallback on clients that do not.

### Changed

- Ambiguous or duplicate section headings are refused as mutation targets: the candidate
  is reported as non-addressable instead of guessing which section to change.

### Removed

- The frozen cache-only contradiction advisory and its packaged replay artifacts. The
  `contradiction_scan` tool name is unchanged; its output now reports `schema_version: 2`.

### Fixed

- Logging shutdown restores log propagation, so test runs no longer depend on execution
  order.

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
