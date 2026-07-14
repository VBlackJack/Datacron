# Datacron — Settled decisions v2.1

**English** · [Français](../fr/decisions-v2.1.md)

> **Status**: Final arbitration after cross-review
> **Author**: Julien Bombled (synthesis arbitrated by Claude)
> **Date**: 2026-05-17
> **Cross-review sources**:
> - Unversioned local archives under `local/docs-ai/`
> - Anthropic docs web verification (Cowork = remote MCP only)
> **Replaces**: ARCHITECTURE.md v2.0 (which becomes v2.1 after patch)

---

## 1. Why v2.1

v2.0 (Claude post-pivot architecture) was submitted to a cross-review by **Gemini Pro and
ChatGPT 5.5 Pro** with a shared structured prompt (12 decisions to challenge + strict output
format).

Both models produced independent verdicts. This v2.1 is the **arbitration result**:

- Strong convergences (10+ out of 12 decisions) → we settle in the direction of convergence without hesitation
- Useful unique insights → we integrate them individually with justification
- One critical empirical verification (Cowork = remote MCP) was carried out via Anthropic docs web search
- ChatGPT's contrarian take (DVS not marketed as an open spec) was retained

---

## 2. The decisive factor: Cowork = remote MCP only

**ChatGPT raised a risk Claude had not seen**: *Cowork and claude.ai only support **remote**
MCP connectors brokered by Anthropic infrastructure — not **local** stdio MCP servers.*

**Empirical verification done** (Anthropic Help Center, 2026-05-17):

> "Local MCP servers configured in Claude Desktop via claude_desktop_config.json are a separate mechanism and do use your local network, but those aren't available in Cowork or claude.ai."
>
> "For remote connectors used with Cowork, your MCP server must be reachable over the public internet from Anthropic's IP ranges."

**Consequence for Datacron**:

| Client | Mode | Compatible with Datacron v1? |
|---|---|---|
| Claude Desktop | local stdio | ✅ direct |
| Claude Code | local stdio | ✅ direct |
| Cowork | remote HTTPS only | ⚠️ tunnel required (v1.x) |
| claude.ai web | remote HTTPS only | ⚠️ tunnel required (v1.x) |
| Mobile apps | remote HTTPS only | ⚠️ tunnel required (v1.x) |
| Cursor | local stdio | 🟡 to validate v1.1 |
| ChatGPT Desktop, Gemini | varies | v2 roadmap |

**Julien's arbitration**: v1 = **Claude Desktop + Claude Code only**. Cowork via a secure HTTPS
tunnel (integrated Cloudflare Tunnel) in **v1.x**, with honest documentation of the local-first
trade-off.

---

## 3. Comparative table of the 12 decisions

| # | v2.0 decision | Gemini | ChatGPT | **v2.1 arbitration** |
|---|---|---|---|---|
| 1 | MCP server hero | ✅ | 🟡 | ✅ — **"local context router read-only" for MVP** |
| 2 | DVS spec | ⚠️ | 🟡 | 🔄 — **DVS as overlay**, not marketed as open spec |
| 3 | L0-L5 trust model | 🟡 (3) | 🟡 (3 UX) | 🔄 — **3 visible UX levels**, L0-L5 in backend for extensibility |
| 4 | Custom FastMCP | ✅ | ✅ | ✅ — confirmed |
| 5 | LangGraph optional | ❌ | ⚠️ | 🔄 — **dropped entirely** from MVP, post-v2 |
| 6 | Retrieval stack | 🟡 | ⚠️ | 🔄 — **ripgrep + SQLite FTS5 only in v1**, embeddings after eval |
| 7 | Prompt injection | ❌ classifier | 🟡 | 🔄 — **sandbox delimiters only**, focus on tool-layer security |
| 8 | Multi-client | 🟡 (Claude+Cursor) | ❌ | 🔄 — **Claude Desktop+Code only v1**, Cursor v1.1 |
| 9 | Monorepo 5 packages | ✅ | 🟡 | 🔄 — **single Python package** v1, monorepo kept for Tauri stub |
| 10 | 4 distribution channels | ⚠️ | ⚠️ | 🔄 — **PyPI/pipx only v1**, brew v1.1 |
| 11 | 8 phases 20 weeks | ❌ | ❌ | 🔄 — **4-week read-only MVP**, rest unblocked by usage |
| 12 | Multi-machine sync | hands-off | single-writer | 🔄 — **single-writer rule v1**, other patterns documented "unsupported" |

**Score**: 11 pivots out of 12 decisions. v2.0 was too ambitious. v2.1 is executable.

---

## 4. Arbitrated decisions — motivated detail

### 4.1 MCP server = local context router read-only

**v2.0**: "the hero of the project", exposes read AND write tools.

**v2.1**: The **read-only** hero for v1. Write tools (`create_*`, `patch_*`, `delete_*`) are
**deferred post-v1** for two cumulative reasons:

- *Concurrency/file lock* (Gemini risk #1): patching a file open in Obsidian/VSCode = corruption. No robust file-locking strategy in the MVP.
- *HITL delegated to clients* (ChatGPT risk #2): we have no guarantee that Claude Desktop actually shows a diff viewer or a typed confirmation for L3+ writes. As long as Datacron does not *own* the approval UX, we do not give the AI the scissors.

**Impact**: Phase 0 + Phase 1 = complete MVP. Phase 3 (write path) awaits a separate decision
after real use of the read-only version.

### 4.2 DVS as overlay (not a marketed spec)

**v2.0**: normative SPEC.md DVS v1.0, hardcoded reserved folders (`_inbox/`, `_ai_generated/`,
`_journal/agent/`), required frontmatter.

**v2.1**:

- Datacron **reads any Markdown vault without migration**. No existing note is forced to conform to DVS.
- The `id` ULID, `content_hash`, and Datacron metadata are stored as **side-metadata in `.datacron/`**, not in existing notes' frontmatter.
- DVS frontmatter is written **only on notes Datacron creates**.
- No normalization command is delivered; the existing vault stays unchanged.
- "Reserved folders" become **configurable** in `.datacron/VAULT.yaml` — the user can map `_inbox/` onto their own `00-Inbox/` PARA if they wish.
- **DVS is not marketed as an "open spec"** (ChatGPT contrarian take, retained). The `SPEC.md` file remains internal reference documentation. If community demand emerges, we will extract it into `jbombled/datacron-spec` later.

**Impact**:
- README.md removes the "The open contract: Datacron Vault Specification" section and replaces it with a sober mention.
- SPEC.md becomes `docs/dvs-reference.md` (internal).
- Datacron adoption on an existing vault = **zero friction**.

### 4.3 Trust model — 3 visible UX levels, L0-L5 backend

**v2.0**: 6 levels L0-L5 exposed to the user.

**v2.1**: Backend keeps 6 levels for extensibility, UX shows **3 categories**:

| UX category | Backend levels | Validation |
|---|---|---|
| **auto-create** | L0, L1 | No friction |
| **review-patch** | L2, L3 | Visual diff + approve |
| **dangerous** | L4, L5 | Double confirmation + Git tag |

The user configures at these 3 levels in Studio/CLI. The policy engine internally maps to the 6
fine-grained levels. Forward-compat preserved.

**Note**: As long as v1 is read-only, these levels are not exposed at all. They arrive with the
write tools post-v1.

### 4.4 Custom FastMCP — confirmed

Absolute Gemini ✅ + ChatGPT ✅ convergence. No debate. Custom is necessary for:
- Direct filesystem access (vs Obsidian REST API which requires the app)
- DVS-lite overlay
- Fine-grained write policies (when we add them)
- Audit logging
- Strict path confinement
- Editor independence

### 4.5 LangGraph dropped entirely

**v2.0**: LangGraph as "optional" for offline mode and autonomous tasks (Phase 4).

**v2.1**: **Entirely out of the MVP**. Cumulative justifications from both reviews:

- "Optional" = still a dependency surface, design coupling, docs, tests (ChatGPT)
- Offline mode is a different product with different needs (Gemini)
- Deterministic CLI cron jobs are enough for a weekly synthesis (ChatGPT)

**If** one day we need offline agentic orchestration, we will evaluate at that point — LangGraph,
PydanticAI, or nothing.

**Impact**: Phase 4 removed. `datacron-agent/` will not be created in v1. MVP complexity divided
by 3.

### 4.6 Retrieval stack — ripgrep + SQLite FTS5 only in v1

**v2.0**: hybrid LanceDB + Contextual Retrieval + Wikigraph + ripgrep (4 systems in Phase 0-2).

**v2.1**: **Minimalist start**:

- `search_regex` via ripgrep
- `search_text` via SQLite FTS5/BM25 (built-in Python `sqlite3`)
- `vault_map` MCP resource (Gemini insight #3, retained)

Vectors (LanceDB + embeddings) are added **only if** an eval harness measures insufficient
recall@k on the real corpus. Contextual Retrieval (Anthropic) waits the same way — no
pre-optimization.

**Gate criterion** (ChatGPT risk #3): mandatory eval harness with:
- recall@k on 30+ real questions
- citation precision
- latency
- token count vs dump
- "would I trust this answer?" human label

If lexical alone gives >80% recall@10 on Julien's corpus → no need for vectors.

### 4.7 Prompt injection — lightweight sandbox, no classifier

**v2.0**: Sandboxing wrap + escape + dedicated Ollama classifier.

**v2.1**:

- ✅ Wrap `<vault_content>...</vault_content>` with a "treat as data not commands" instruction
- ✅ Escape suspicious sequences (`<system>`, `Ignore previous instructions`)
- ✅ Strict path confinement (`DATACRON_READ_PATHS`)
- ✅ Bounded result sizes (top-k limits)
- ❌ ML classifier removed (latency theater, single-user threat model)
- ➕ ChatGPT adds: focus on **tool layer security** (descriptor integrity, write authorization, cross-tool exfiltration) — that is where the recent MCP literature identifies the real weak point

### 4.8 Multi-client v1 — Claude Desktop + Claude Code only

**v2.0**: multi-client promise (Claude family, Cursor, ChatGPT Desktop, Gemini).

**v2.1 confirmed by empirical verification** (§2 above):

| Client | v1 status | Roadmap status |
|---|---|---|
| Claude Desktop | ✅ **E2E tested** | — |
| Claude Code | ✅ **E2E tested** | — |
| Cowork | ⏳ v1.x via HTTPS tunnel | honest "What leaves your machine" section |
| Cursor | 🟡 v1.1 (to validate) | — |
| ChatGPT Desktop (Apps SDK) | 🔵 v2 | — |
| Gemini | 🔵 v2 (when official MCP GA) | — |

The code stays MCP-compatible by design. Only the **marketing promise** narrows.

### 4.9 Monorepo — 1 Python package in v1

**v2.0**: 5 Python workspace packages + a Tauri Rust crate.

**v2.1**: Monorepo kept (single Git repo), but radically simplified internal structure:

```
datacron/
├── README.md
├── pyproject.toml                # single Python package
├── src/datacron/
│   ├── __init__.py
│   ├── cli.py                    # `datacron …` entry point (Typer)
│   ├── mcp/                      # FastMCP server (submodule)
│   ├── core/                     # parser, hashing, paths, config
│   ├── indexing/                 # SQLite FTS5, ripgrep wrapper
│   └── tools/                    # MCP tools (read-only v1)
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── decisions-tranchees-v2.1.md  (this document)
│   ├── dvs-reference.md          # ex-SPEC.md (internal)
│   ├── Gemini_v2-review.md
│   ├── ChatGPT_v2-review.md
│   └── …
├── examples/
│   └── demo-vault/
├── scripts/                       # Bash conforming to bash-standards
│   ├── reindex_full.sh
│   └── release.sh
├── .github/workflows/
│   └── ci.yml                     # Tests + lint + shellcheck
└── LICENSE                        # Apache 2.0
```

The repo stays ready to host `crates/datacron-studio/` when we build Studio, and a split into
multiple packages when we have ≥3 genuinely distinct subsystems (Agent, Daemon, etc.).

### 4.10 Distribution — PyPI/pipx only in v1

**v2.0**: PyPI + Homebrew + Docker + Tauri binaries.

**v2.1**: **PyPI/pipx only** for v1.

- ✅ `pipx install datacron` — a single official channel
- 📅 Homebrew tap → v1.1 if macOS feedback asks for it
- 📅 Docker → CI/demo only, not distributed (uid/gid hell for file-local)
- 📅 Tauri binaries → deferred to Studio v2 if demanded

### 4.11 Roadmap — 4-week read-only MVP

**v2.0**: 8 phases ~20 weeks.

**v2.1**: **Phase 0 only** = a 4-week MVP. Everything else becomes post-MVP, unblocked by real
usage.

**Phase 0 (4 weeks)** — *"Local context router read-only"*:

| Week | Deliverable |
|---|---|
| 1 | `pyproject.toml`, Apache 2.0 headers, Python FileLogger, frontmatter parser, path confinement, `datacron init`, `datacron status` |
| 2 | FastMCP stdio server, tools `list_notes`, `get_note`, resource `vault_map` |
| 3 | SQLite FTS5 indexer, `search_text`, ripgrep wrapper, `search_regex`, `datacron index`, `datacron reindex` |
| 4 | `datacron mcp install --client claude-desktop`, eval harness, dogfooding on Julien's vault, polish, release `datacron 0.1.0` on PyPI |

**Phase 0 success criterion** (ChatGPT, verbatim): *"From Claude Desktop, ask 30 real questions
against Julien's vault and beat manual folder dumping on answer quality, latency, and token
cost."*

**Post-MVP** (unblocked by v0.1.0 usage):
- v0.2: write tools (`append_journal`, `create_draft_note`) + Git snapshot
- v0.3: future HTTPS tunnel for Cowork, no command delivered to date
- v0.4: embeddings + LanceDB if Phase 0 eval shows a need
- v0.5: Contextual Retrieval if v0.4 eval still shows a gap
- v1.0: stabilization + MkDocs docs + Homebrew tap
- v2.0+: Tauri Studio, LangGraph offline, multi-client Cursor/ChatGPT/Gemini

### 4.12 Sync — single-writer vault rule in v1

**v2.0**: "Hands-off, the user manages Git/Syncthing/iCloud".

**v2.1** (ChatGPT pivot):

- **v1**: Datacron writes from **a single machine**. All other machines are in read-only mode (or do not use Datacron).
- Any other pattern (Datacron writer on 2 machines with Syncthing between them) is documented as **explicitly unsupported**, because it breaks `content_hash_before`, index freshness, and the audit log.
- Git remains for rollback only, not for distributed sync.

---

## 5. Retained unique insights

| Source | Insight | Status |
|---|---|---|
| Gemini | Concurrency/file-lock corruption | ✅ → write tools deferred post-v1 |
| Gemini | Vault Map in the system prompt | ✅ → MCP resource `datacron://vault/map` (MVP) |
| Gemini | Filesystem as state machine (no YAML `ai.status`) | ✅ → `_drafts/` instead of YAML status (v0.2+) |
| Gemini | Explicit `datacron reindex` vs perfect watcher | ✅ → MVP command |
| ChatGPT | Mandatory eval harness before any retrieval addition | ✅ → gate in Phase 0 |
| ChatGPT | HITL owned by Datacron (TUI/CLI) | ⏸ → v0.2 when write arrives |
| ChatGPT | Single-writer vault rule | ✅ → documented v1 |
| ChatGPT | Cowork = remote MCP **(empirically verified)** | ✅ → v1 scoping adjusted |
| ChatGPT | DVS not marketed as open spec | ✅ → contrarian retained |
| ChatGPT | Tool-layer security (descriptors, exfiltration) | ✅ → v0.2 focus when write arrives |

---

## 6. Non-retained insights, with justification

| Source | Insight | Why not retained |
|---|---|---|
| Gemini | "Local-first is an illusion" (contrarian) | A polemical, partly fair position, but Datacron *can* be strictly local-first if the user stays on Ollama. The pitch is kept but **clarified honestly**: a "What leaves your machine" section in the README that explains what goes to Anthropic when using the Claude API. No lying, no buzzword gargling. |
| Gemini | "Use Obsidian's standards directly, no DVS" | Obsidian has no convention for `origin: ai`, audit, or trust. DVS stays useful *as an invisible overlay*, just not marketed. |
| ChatGPT | Cowork "marked unsupported until working E2E exists" | Harder than necessary. v2.1 position: v1.x via HTTPS tunnel with honest trade-off docs, rather than total "unsupported". |

---

## 7. The 4-week MVP — executable spec

### Exposed technical surface

```
MCP tools (read-only, MVP):
  • list_notes(folder?, tags?, limit?) → array of {path, title, frontmatter}
  • get_note(id_or_path, format=full|map) → full content OR document-map
  • search_text(query, limit=20) → array of {chunk, path, score}      [SQLite FTS5/BM25]
  • search_regex(pattern, glob?, limit=20) → array of matches         [ripgrep wrapper]
  • get_backlinks(target) → array of source paths                     [wikilinks parser]

MCP resources:
  • datacron://vault/map     → folder/file tree, lightweight (~2k tokens max)
  • datacron://vault/info    → vault metadata (path, note count, last index)
  • datacron://policy/active → current policy (empty/permissive in MVP)

CLI:
  • datacron init <path>             → initialize .datacron/ in any markdown folder
  • datacron status                  → show vault state, index freshness
  • datacron index                   → build/rebuild index
  • datacron reindex                 → force full reindex
  • datacron mcp serve               → start FastMCP server (stdio)
  • datacron mcp install --client claude-desktop → write client config
  • datacron eval                    → run eval harness on test questions
```

### Technical guardrails

- Strict path confinement via `DATACRON_READ_PATHS`
- Bounded result sizes: `maxMatchesPerHit=20`, content truncation if > 8k tokens
- Sandboxing `<vault_content>...</vault_content>` wrapping
- Deterministic chunk IDs (ULID + header_path + ordinal)
- Apache 2.0 headers on every `.py` file (dev-standards)
- Shellcheck clean on every `.sh` (bash-standards)
- Python FileLogger (`~/.datacron/logs/datacron_{YYYYMMDD}.log`)

### Success criterion

On Julien's personal vault, **30 real questions** asked via Claude Desktop with the Datacron MCP
must beat the baseline (folder dump) on:
- ✅ Answer quality (human "would I trust this?" label)
- ✅ Latency (P50 < 5s, P95 < 12s)
- ✅ Token cost (reduction ≥10× vs equivalent dump)
- ✅ Citation precision (at least 1 citable chunk per non-trivial answer)

If these 4 criteria pass → release v0.1.0 on PyPI. Otherwise → iteration.

---

## 8. Standards compliance

Unchanged v2.0 → v2.1:
- Python: Apache 2.0 headers, English, zero hardcoding, FileLogger, ruff + mypy strict, async/await, no `try/except: pass`.
- Bash: `env bash` shebang, `set -euo pipefail`, logging, dry-run, trap, prereqs, getopts, `--help`, shellcheck clean.

---

## 9. Documents to patch after this one

| File | Action | Status |
|---|---|---|
| `README.md` | v2.1 patch: narrow multi-client, no Studio v1, honest "What leaves your machine", DVS overlay | To do |
| `SPEC.md` | Rename to `docs/dvs-reference.md`, simplify (overlay, no migration, filesystem state machine) | To do |
| `docs/ARCHITECTURE.md` | v2.1 patch: 4-week MVP, no LangGraph, no embeddings v1, no write tools v1, no Tauri, 1 package | To do |
| `docs/architecture-overview.svg` | Update: Cowork removed from the top layer, Studio removed, LangGraph removed from the MVP | To do |

---

## 10. Methodology — what we keep for the future

This **design → cross-review → arbitration → executable spec** loop produced, in 24h, a spec
more solid than a month of siloed design. Repeat at every major pivot:

1. Produce a defensible, structured v0 design.
2. Submit it to **two independent models** with a strict shared prompt and an identical output format.
3. Read both returns **with humility** — actively seek the points where they are right, do not defend against them.
4. Identify **strong convergences** (= clear signals) and **unique insights** (= added value).
5. **Empirically verify** critical claims (here: Cowork = remote, via Anthropic docs web search).
6. Arbitrate each point with a motivated justification.
7. Re-spec by eliminating **everything not in the MVP**.

Cost: ~2h of prompt engineering + 1h of reading/synthesis + 1h of writing. Gain: avoided pivots,
4-week MVP instead of 20 weeks, scope creep eliminated.

---

*v2.1 document frozen on 2026-05-17. Any decision can be reopened via a new dated ADR, but the
reopening threshold is high: strong convergence of both reviews + empirical verification form a
solid base.*
