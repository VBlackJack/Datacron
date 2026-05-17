# Shared Context — Datacron implementation

> **For**: Claude Code and Codex (both agents implementing Datacron Phase 0).
> **Read first** before starting any work. Then read your specific brief
> (`02-brief-claude-code.md` or `03-brief-codex.md`).

---

## Project at a glance

**Datacron** is a local-first MCP server that lets Claude Desktop / Claude Code query
a Markdown vault by exposing chunks via the Model Context Protocol — saving 20-50× tokens
versus dumping notes in context.

You are implementing **Phase 0** = the MVP, **read-only**, **4 weeks** of work split
between two parallel agents.

- Owner: Julien Bombled (jbombled@proton.me)
- Repository (local): `G:\_Projects\Datacron`
- License: **Apache 2.0** (all code, all files, every header)
- Code language: **English** (code, comments, docstrings, identifiers, commits, PR descriptions)
- User docs language: **Français** when targeted at Julien; English for technical refs

---

## Mandatory reference reading

Before writing a line of code, read these in order:

| Doc | Why |
|---|---|
| [`README.md`](../../README.md) | Product positioning, what Datacron is and is not |
| [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) | Technical spec v2.1, MVP scope, ADRs |
| [`docs/decisions-tranchees-v2.1.md`](../decisions-tranchees-v2.1.md) | Why every decision was made, what was rejected and why |
| [`SPEC.md`](../../SPEC.md) | Internal vault conventions (DVS-lite) |
| [`01-contracts.md`](./01-contracts.md) | **The Pydantic/Protocol interfaces between you and the other agent. Frozen.** |
| Your specific brief (`02-` or `03-`) | Your scope, deliverables, checkpoints |
| [`04-integration-plan.md`](./04-integration-plan.md) | Weekly schedule + cross-review protocol |

If anything in your brief contradicts ARCHITECTURE.md or decisions-tranchees-v2.1.md,
**STOP and ask Julien**. Don't silently diverge.

---

## Standards (non-negotiable)

### Python

- **Apache 2.0 header** on every `.py` file. Template:
  ```python
  # Copyright 2026 Julien Bombled
  #
  # Licensed under the Apache License, Version 2.0 (the "License");
  # you may not use this file except in compliance with the License.
  # You may obtain a copy of the License at
  #
  #     http://www.apache.org/licenses/LICENSE-2.0
  ```
- **All code in English**. No French in identifiers, comments, or docstrings.
- **Zero hardcoding**. Config via `pydantic-settings` reading `.env`. No magic numbers in logic.
- **Logging**: use the `FileLogger` defined in `src/datacron/core/logger.py` (provided by
  Claude Code in Sem 1). Format: `[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`. Daily file in
  `~/.datacron/logs/datacron_{YYYYMMDD}.log`. Toggle via env var `DATACRON_LOG_LEVEL`.
- **Docstrings**: Google-style on all public functions/classes.
- **Async/await** for all I/O (filesystem, SQLite, subprocess).
- **No silent exception swallowing**. Log + re-raise.
- **`@final`** decorator on classes not meant for inheritance.
- **Lint/type checks**: `ruff check .`, `ruff format --check .`, `mypy --strict src/`
  must all pass clean.
- **Tests**: `pytest` with `pytest-asyncio`. Coverage target: ≥80% on your modules.

### Bash (if you write any script)

- Shebang `#!/usr/bin/env bash`
- `set -euo pipefail` on the line after the header
- Apache 2.0 header in `#` comments
- Logging functions (`log_info`, `log_warn`, `log_error`)
- Config block with `${VAR:-default}` pattern
- `--help` via `usage()` function
- `--dry-run` if state-changing
- `shellcheck` must pass clean

### Git workflow

- **Your branch**: `claude-code/phase0` or `codex/phase0` (specified in your brief).
- **Commit style**: imperative, English, prefixed: `feat(core): add FileLogger`,
  `fix(mcp): handle empty vault`, `test(indexing): cover chunker edge cases`,
  `docs(brief): clarify contract X`.
- **Atomic commits**. One logical change per commit. Squash before PR if needed.
- **PR target**: `main`. Open a PR every Friday for that week's deliverables.
- **PR description template**:
  ```
  ## What
  [one paragraph]
  
  ## Scope
  - file 1
  - file 2
  
  ## Tests
  [how to verify]
  
  ## Cross-review request
  Please review modules: [list]
  ```

### Cross-review protocol

Every Friday end-of-day:
1. You open a PR with the week's work.
2. The **other agent** reviews your PR (Julien briefs them).
3. Comments are added inline.
4. You address comments by Monday morning.
5. Julien approves and merges before Monday EOD.

See `04-integration-plan.md` for the full schedule.

---

## Repository layout (Phase 0 target)

```
datacron/
├── README.md
├── SPEC.md
├── LICENSE
├── pyproject.toml
├── .gitignore
├── src/datacron/
│   ├── __init__.py
│   ├── cli.py                          # Claude Code
│   ├── core/                           # Claude Code
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── logger.py
│   │   ├── paths.py
│   │   ├── hashing.py
│   │   ├── frontmatter.py
│   │   ├── models.py                   # Pydantic models from 01-contracts.md
│   │   └── vault.py
│   ├── mcp/                            # Claude Code
│   │   ├── __init__.py
│   │   ├── server.py
│   │   ├── tools.py
│   │   ├── resources.py
│   │   └── sandbox.py
│   ├── indexing/                       # Codex
│   │   ├── __init__.py
│   │   ├── chunker.py
│   │   ├── fts5_store.py
│   │   ├── ripgrep.py
│   │   └── wikilinks.py
│   ├── eval/                           # Codex
│   │   ├── __init__.py
│   │   ├── harness.py
│   │   └── metrics.py
│   └── installers/                     # Claude Code
│       ├── __init__.py
│       └── claude_desktop.py
├── tests/
│   ├── conftest.py                     # shared fixtures (Claude Code)
│   ├── fixtures/
│   │   └── demo-vault/                 # small test vault
│   ├── integration/                    # Claude Code
│   │   └── test_mcp_e2e.py
│   └── unit/
│       ├── core/                       # Claude Code
│       ├── mcp/                        # Claude Code
│       ├── indexing/                   # Codex
│       └── eval/                       # Codex
├── docs/
│   ├── ARCHITECTURE.md
│   ├── decisions-tranchees-v2.1.md
│   ├── agent-briefs/                   # you are here
│   └── user-guide/                     # Claude Code, Sem 4
├── examples/
│   └── demo-vault/                     # full demo vault (Claude Code, Sem 4)
├── scripts/
│   └── release.sh                      # Claude Code, Sem 4
└── .github/
    └── workflows/
        └── ci.yml                      # Claude Code, Sem 1
```

**Disjoint directories**: Claude Code owns `core/`, `mcp/`, `cli.py`, `installers/`.
Codex owns `indexing/`, `eval/`. Tests follow the same split. **No collisions possible.**

---

## Communication norms

- Code goes in the repo. Status updates go in PR descriptions.
- If you need to ask Julien something, write it at the top of your PR description as
  `## Questions for Julien`. Don't block waiting for an answer — code defensive defaults
  and flag the assumption.
- If you discover that a contract in `01-contracts.md` is wrong or insufficient,
  **propose a contract amendment** in a separate PR titled `contract: amend X`. The other
  agent must approve before the amendment merges.

---

## Success criteria for Phase 0 (read this often)

At end of Sem 4, the project must:

1. Install cleanly via `pipx install -e .` (or local equivalent).
2. `datacron init <path>` works on Julien's real Markdown vault.
3. `datacron index` builds an FTS5 index from the vault.
4. `datacron mcp install --client claude-desktop` writes correct config.
5. Claude Desktop connects to Datacron and lists/searches/reads notes via MCP tools.
6. The eval harness runs against 30 questions and produces metrics
   (`recall@k`, `citation_precision`, `latency_ms`, `tokens_returned`).
7. CI passes: `ruff` + `mypy --strict` + `pytest` + `shellcheck`.

**The decisive test** (from ChatGPT's review, verbatim):
> "From Claude Desktop, ask 30 real questions against Julien's vault and beat manual
> folder dumping on answer quality, latency, and token cost."

If we hit that, v0.1.0 ships on PyPI. If not, we iterate.

---

## What's out of scope (do NOT build)

These were explicitly reported / killed in v2.1 arbitration. Don't sneak them in:

- ❌ Write tools (`create_*`, `patch_*`, `delete_*`, `append_*`)
- ❌ LangGraph / Ollama / any local LLM call
- ❌ Vector embeddings / LanceDB / Contextual Retrieval
- ❌ Tauri / GUI / Studio app
- ❌ Docker / Homebrew formulae
- ❌ HTTP transport / tunnel mode (stdio only)
- ❌ Multi-client matrix (only Claude Desktop + Claude Code)
- ❌ Trust model L0-L5 UX exposure (engine dormant)
- ❌ Watcher daemon (`datacron reindex` is explicit)

If you find yourself starting one of these, **stop and re-read** `decisions-tranchees-v2.1.md`.

---

## Quick-reference index

- Architecture details: `docs/ARCHITECTURE.md`
- Why each decision: `docs/decisions-tranchees-v2.1.md`
- Vault format conventions: `SPEC.md`
- Pydantic/Protocol contracts: `docs/agent-briefs/01-contracts.md`
- Your scope: `docs/agent-briefs/02-brief-claude-code.md` or `03-brief-codex.md`
- Weekly schedule: `docs/agent-briefs/04-integration-plan.md`
- Standards: this file (§Standards)

Good luck. Ship clean code.
