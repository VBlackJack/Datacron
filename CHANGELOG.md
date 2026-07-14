# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `datacron setup`, a guided end-to-end wizard that initializes the sidecar, builds the index,
  and wires an MCP client in one command, with location and option choices (interactive by
  default, `--yes` for unattended runs). Vault initialization is now shared through
  `datacron.bootstrap` between `init` and `setup`.
- Thirteen MCP tools covering vault reads, lexical and regex search, confined writes,
  operational health, note history, and operation-audit queries.
- Content-addressed note history, durable operation evidence, integrity scrubbing, and
  byte-exact freshness contracts.
- A locked uv environment for the complete runtime and development dependency set.
- Import-purity regression coverage for core write-path and MCP modules.

### Changed

- CI installs frozen dependencies and validates Python 3.11 and 3.12 on Ubuntu and Windows.
- Durable-write primitives live in `datacron.core.durability`, removing the former
  `vault_writer` and `operation_log` import cycle.
- Logging configuration is explicit at CLI and MCP server entrypoints; importing Datacron
  modules no longer parses environment settings, creates log directories, or starts threads.

### Fixed

- Platform-specific durability and locking code now passes strict mypy checks on Linux and
  Windows.
- CLI logging teardown tolerates already-closed captured streams without suppressing unrelated
  errors.
