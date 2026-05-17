# Integration Plan — Datacron Phase 0 (4 weeks)

> **Audience**: Julien (orchestrator), Claude Code, Codex
> **Schedule**: 4 calendar weeks. Each week ends Friday EOD with PRs and cross-review.
> Monday morning = merge into `main` after addressing comments.

---

## 1. Workflow overview

```
       ┌────────────────────┐         ┌──────────────────┐
       │  Claude Code       │         │  Codex           │
       │  branch: claude-   │         │  branch: codex/  │
       │    code/phase0     │         │    phase0        │
       └─────────┬──────────┘         └────────┬─────────┘
                 │                              │
            commits + push                 commits + push
                 │                              │
                 ▼                              ▼
       ┌──────────────────────────────────────────────┐
       │  Friday EOD: open PRs against `main`         │
       └──────────────────────────────────────────────┘
                 │
                 ▼
       ┌──────────────────────────────────────────────┐
       │  Friday → Sunday: cross-review                │
       │  Each agent reviews the other's PR           │
       │  Inline comments, change requests             │
       └──────────────────────────────────────────────┘
                 │
                 ▼
       ┌──────────────────────────────────────────────┐
       │  Monday morning: address comments             │
       │  Julien reviews comments + final approval     │
       │  Julien merges to `main` before Monday EOD    │
       └──────────────────────────────────────────────┘
```

**Why two branches**: maximum traceability. Conflicts surface in PR review, not in
mysterious main breakage. Julien retains veto over every merge.

**Why Monday merge** (not Friday): weekend gives breathing room for thoughtful review.
Forces neither agent to merge their own PR. Julien is in the loop on every merge.

---

## 2. Setup steps (Julien, do this Sunday before Sem 1 Monday)

### 2.1 Initialize the Git repo

```bash
cd /g/_Projects/Datacron
git init
git add .
git commit -m "chore: design phase v2.1 frozen"
git branch -M main

# Create the two agent branches
git branch claude-code/phase0
git branch codex/phase0
```

### 2.2 Set up `.gitignore`

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
venv/

# Datacron runtime
.datacron/
*.db
*.db-wal
*.db-shm

# OS
.DS_Store
Thumbs.db

# IDE
.idea/
.vscode/

# Logs
logs/
*.log

# Secrets
.env
.env.local
```

### 2.3 Brief the agents

For each agent (separately):

```
You are working on the Datacron project at G:\_Projects\Datacron.
Please read these files in order:
1. docs/agent-briefs/00-shared-context.md
2. docs/agent-briefs/01-contracts.md
3. docs/agent-briefs/02-brief-claude-code.md   (if you are Claude Code)
   OR
   docs/agent-briefs/03-brief-codex.md          (if you are Codex)
4. docs/agent-briefs/04-integration-plan.md

Then check out your branch:
  git checkout claude-code/phase0   (if Claude Code)
  git checkout codex/phase0          (if Codex)

Begin Week 1 deliverables. Push commits as you go. Open a PR to main on Friday EOD.
```

---

## 3. Weekly schedule

### Week 1: Bootstrap + contracts

| Day | Claude Code | Codex |
|---|---|---|
| Mon | Bootstrap: pyproject, LICENSE, .gitignore, layout, CI | (waits for `core/models.py` to land — work on fixtures + plan)
| Tue | `core/models.py` (frozen contracts) → **merge to main fast track** | Once `models.py` is on main: start `chunker.py` |
| Wed | `core/{config,logger,paths,hashing,frontmatter}.py` | `chunker.py` + tests |
| Thu | `core/vault.py` (VaultReader) | `chunker.py` polish + edge cases |
| Fri | CLI skeleton + `datacron init` / `datacron status` + test fixtures (`tests/fixtures/demo-vault/`) → **PR `[Sem 1] Bootstrap`** | **PR `[Sem 1] AST chunker`** |

**Fast-track exception**: `core/models.py` must be merged to `main` by **Tuesday EOD**
so Codex can import the types and start coding. This is the only mid-week merge allowed.
Julien handles it manually.

**Cross-review weekend (Sat-Sun Sem 1)**:
- Claude Code reviews Codex's chunker PR — focus: contract adherence, ordinal stability, edge cases.
- Codex reviews Claude Code's bootstrap PR — focus: model implementation matches contracts, CI is comprehensive.

**Monday Sem 2 (merge day)**: both Sem 1 PRs merged by Julien.

---

### Week 2: MCP server + FTS5 store + wikilinks

| Day | Claude Code | Codex |
|---|---|---|
| Mon | `mcp/sandbox.py` + tests | `fts5_store.py` schema + open/close |
| Tue | `mcp/server.py` FastMCP setup | `fts5_store.py` upsert/delete |
| Wed | `mcp/tools.py` `list_notes`, `get_note` | `fts5_store.py` search + snippet |
| Thu | `mcp/resources.py` (vault/map, info, policy) | `wikilinks.py` + tests |
| Fri | `mcp serve` CLI + integration test → **PR `[Sem 2] MCP server + tools`** | **PR `[Sem 2] FTS5 + wikilinks`** + (if needed) contract amendment for `Chunk.line_start/line_end` |

**Cross-review weekend (Sat-Sun Sem 2)**:
- Claude Code reviews Codex's FTS5 — focus: snippet format, transaction safety, performance.
- Codex reviews Claude Code's MCP — focus: tool signatures match Protocols, sandbox applied consistently.

**Contract amendment (if proposed)**: discussed during cross-review. Both agents must agree.
Julien merges the amendment first, then both work PRs are rebased onto it.

---

### Week 3: Search tools + ripgrep + installer

| Day | Claude Code | Codex |
|---|---|---|
| Mon | `mcp/tools.py` `search_text` (wraps `FTS5Store.search`) | `ripgrep.py` subprocess + JSON parsing |
| Tue | `mcp/tools.py` `search_regex` (wraps `RipgrepWrapper`) | `ripgrep.py` chunk resolution via FTS5 |
| Wed | `mcp/tools.py` `get_backlinks` (uses `WikilinksExtractor`) | `ripgrep.py` tests (mock + integration) |
| Thu | `installers/claude_desktop.py` + `mcp install` CLI | `ripgrep.py` polish + benchmarks |
| Fri | `index` / `reindex` CLI (delegates to indexing modules) → **PR `[Sem 3] Search tools + installer`** | **PR `[Sem 3] Ripgrep wrapper`** |

**Cross-review weekend (Sat-Sun Sem 3)**:
- Claude Code reviews ripgrep — focus: subprocess lifecycle, error handling, JSON streaming.
- Codex reviews search tools — focus: bounded results, sandbox wrapping, correct delegation.

---

### Week 4: Eval + polish + release

| Day | Claude Code | Codex |
|---|---|---|
| Mon | `cli.py eval` (delegates to `eval.harness`) | `eval/metrics.py` + tests |
| Tue | `docs/user-guide/getting-started.md` (FR) | `eval/harness.py` + tests |
| Wed | `docs/user-guide/troubleshooting.md` + `examples/demo-vault/` | `examples/eval-questions.example.yaml` template |
| Thu | `scripts/release.sh` + CHANGELOG | Eval harness polish, summary table output |
| Fri | Integration testing end-to-end → **PR `[Sem 4] Polish + docs + release`** | **PR `[Sem 4] Eval harness + metrics`** |

**Cross-review weekend (Sat-Sun Sem 4)**:
- **Both agents do the final pass**: full E2E review of the merged codebase as it would look post-merge.
- Each agent writes a short retro at `docs/retro-phase0-{agent}.md`.

**Monday Sem 5 (release day)**:
1. Julien merges both Sem 4 PRs.
2. Julien runs the **success test**: 30 real questions against Julien's vault from
   Claude Desktop. Beats folder dump on quality, latency, tokens.
3. If success: tag `v0.1.0`, `bash scripts/release.sh` publishes to PyPI.
4. If failure: identify regression, schedule v0.1.1 fix sprint.

---

## 4. Cross-review protocol (detailed)

### 4.1 What a cross-review looks like

When the **other agent** reviews your PR, they:

1. Read the PR description (especially `## Cross-review requests for [you]`).
2. Pull the branch locally: `git fetch origin && git checkout origin/<other-branch>`.
3. Run the full test suite: `pytest`, `ruff check`, `mypy --strict src/`.
4. Read every changed file.
5. Leave inline comments on specific lines using GitHub PR comments (or, if no GitHub:
   in a `review-<date>.md` file at the repo root).
6. Submit a top-level review: **Approve**, **Request changes**, or **Comment**.

### 4.2 What to look for

| Reviewer's focus | What to check |
|---|---|
| **Contract adherence** | Does the implementation match `01-contracts.md` exactly? Signatures, behaviors, ordering? |
| **Standards compliance** | Apache header? English everywhere? No hardcoded paths/URLs/numbers? `FileLogger` used (not `print`)? |
| **Import discipline** | No imports across forbidden boundaries (see `01-contracts.md` §3) |
| **Async correctness** | All I/O is `async`? No `asyncio.run()` inside an async function? |
| **Error handling** | No silent except? Errors logged and re-raised or handled explicitly? |
| **Test coverage** | New code has tests? Edge cases covered? Tests fail before fix? |
| **Performance** | Are there obvious O(n²) loops where O(n) would do? Database calls in loops? |
| **Security** | Path confinement enforced? Sandbox applied on every content return? Sensitive data not logged? |
| **API consumer impact** | If you changed a Protocol, did you also update the consumer? |

### 4.3 Comment etiquette

- Be specific: cite line numbers, suggest fixes.
- Be technical: cite contracts, ARCHITECTURE.md sections, standards.
- Be constructive: "consider X because Y" beats "this is wrong".
- Be quick: comments by Saturday EOD; agent under review responds by Sunday.

### 4.4 Disagreements

If the two agents disagree on a comment:
1. Each posts a clear case in the PR thread.
2. Julien arbitrates with a final comment + decision.
3. The losing agent updates code or accepts the comment.

No infinite loops. Julien is the tiebreaker.

---

## 5. Contract amendments

If during implementation an agent finds that `01-contracts.md` is wrong/insufficient:

1. **Open a separate PR** titled `contract: amend <model or protocol>`.
2. PR description:
   - What's wrong (specific line/field).
   - What changes (before/after diff).
   - Why (failing case it would enable; current bug it fixes).
   - Impact on other agent (what they'd have to change).
3. The other agent reviews within 24h.
4. If agreed: Julien merges the amendment first, then both work PRs rebase onto it.
5. The amendment also updates `01-contracts.md` §7 "Frozen status" table with the date.

**Pre-known amendment in Sem 2**: Codex will likely propose adding `line_start` and
`line_end` to `Chunk` so that ripgrep results can be resolved to chunks. This is expected
and pre-approved in principle.

---

## 6. Julien's role during the 4 weeks

You are the **orchestrator and arbitrator**. Your weekly checklist:

### Each Monday
- [ ] Pull `main`, check that Friday's PRs merged cleanly.
- [ ] Read the merged PR descriptions and the retro of any issues.
- [ ] Briefly check the test report and CI green status.
- [ ] Re-brief each agent on Week N's deliverables (paste the relevant section of their brief).

### Each Wednesday (mid-week check-in, optional)
- [ ] `git log claude-code/phase0..main` and `git log codex/phase0..main` — are they making progress?
- [ ] If either branch is silent, ping that agent.
- [ ] If a contract amendment PR is open, ensure both agents have reviewed.

### Each Friday EOD
- [ ] Verify both PRs are open against `main`.
- [ ] Read each PR description.
- [ ] Brief the OTHER agent to do the cross-review.

### Each Sunday EOD
- [ ] Read cross-review comments.
- [ ] If disagreement, arbitrate with a final comment.

### Each Monday (merge day)
- [ ] Verify all comments addressed.
- [ ] Run the test suite locally on each PR's tip.
- [ ] Squash + merge each PR.
- [ ] If conflict, request rebase; do not merge with conflicts.

### Week 4 Monday (release)
- [ ] Final E2E test from a clean checkout.
- [ ] Run the **30-question success test** yourself.
- [ ] If pass: tag and release v0.1.0.
- [ ] If fail: open issues, plan v0.1.1.

---

## 7. Emergency protocols

### One agent is blocked waiting for the other

- Agent posts in their PR description: `## Blocked on: [specific deliverable from other agent]`.
- Julien escalates: tells the blocker to prioritize that deliverable.
- Blocked agent works on tests, docs, or out-of-critical-path improvements until unblocked.

### A contract amendment is contested

- Julien convenes a "design discussion" in a PR comment thread.
- 24h timeline. Final decision by Julien.

### CI is red on `main`

- The PR that caused it is reverted immediately by Julien.
- The agent re-opens a fixed PR within 24h.

### One agent's PR is fundamentally wrong (e.g., violates contracts wholesale)

- Julien posts a clear "Reject and rework" comment.
- Agent rewrites that week's deliverable.
- Schedule slips by ~1 week. Adjust subsequent weeks.

---

## 8. End state at Week 4 Friday

```
G:\_Projects\Datacron\
├── README.md                              (Claude Code, Sem 4)
├── SPEC.md
├── LICENSE                                (Claude Code, Sem 1)
├── pyproject.toml                         (Claude Code, Sem 1)
├── CHANGELOG.md                           (Claude Code, Sem 4)
├── .gitignore
├── .python-version
├── src/datacron/
│   ├── __init__.py                        (Claude Code, Sem 1)
│   ├── cli.py                             (Claude Code, Sem 1-4)
│   ├── core/                              (Claude Code, Sem 1)
│   │   ├── models.py                      [contracts §1]
│   │   ├── config.py
│   │   ├── logger.py
│   │   ├── paths.py
│   │   ├── hashing.py
│   │   ├── frontmatter.py
│   │   └── vault.py                       [VaultReader Protocol §2.6]
│   ├── mcp/                               (Claude Code, Sem 2-3)
│   │   ├── server.py
│   │   ├── tools.py                       (5 read-only tools)
│   │   ├── resources.py
│   │   └── sandbox.py
│   ├── indexing/                          (Codex, Sem 1-3)
│   │   ├── chunker.py                     [ASTChunker Protocol §2.1]
│   │   ├── fts5_store.py                  [FTS5Store Protocol §2.3]
│   │   ├── ripgrep.py                     [RipgrepWrapper Protocol §2.4]
│   │   └── wikilinks.py                   [WikilinksExtractor Protocol §2.2]
│   ├── eval/                              (Codex, Sem 4)
│   │   ├── harness.py                     [EvalHarness Protocol §2.5]
│   │   └── metrics.py
│   └── installers/                        (Claude Code, Sem 3)
│       └── claude_desktop.py
├── tests/
│   ├── conftest.py                        (Claude Code, Sem 1)
│   ├── fixtures/
│   │   └── demo-vault/                    (Claude Code, Sem 1)
│   ├── unit/
│   │   ├── core/                          (Claude Code)
│   │   ├── mcp/                           (Claude Code)
│   │   ├── indexing/                      (Codex)
│   │   └── eval/                          (Codex)
│   └── integration/
│       └── test_mcp_e2e.py                (Claude Code)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── decisions-tranchees-v2.1.md
│   ├── retro-phase0-claude-code.md        (Claude Code, Sem 4)
│   ├── retro-phase0-codex.md              (Codex, Sem 4)
│   ├── agent-briefs/                      (this directory)
│   └── user-guide/
│       ├── getting-started.md             (FR, Claude Code, Sem 4)
│       └── troubleshooting.md             (FR, Claude Code, Sem 4)
├── examples/
│   ├── demo-vault/                        (Claude Code, Sem 4)
│   └── eval-questions.example.yaml        (Codex, Sem 4)
├── scripts/
│   └── release.sh                         (Claude Code, Sem 4)
└── .github/
    └── workflows/
        └── ci.yml                         (Claude Code, Sem 1)
```

All on `main`. `v0.1.0` tag points to the release commit. `pipx install datacron` works.
The 30-question success test passes from Claude Desktop.

---

## 9. After Phase 0

If the success test passes, immediately schedule Phase v0.2 (write tools) with the
same workflow. The contracts file expands; new Protocols are added (`WriteToolPolicy`,
`GitSnapshotter`, etc.). Cross-review continues.

If the success test fails, root-cause and run v0.1.1 as a fix sprint. Contracts likely
don't need to change; just implementation.

Either way, Phase 0 has built the foundation that makes everything else cheap.

---

## 10. Final notes for Julien

This integration plan is **deliberate**: it trades a bit of velocity for high
correctness and traceability. With two agents working in parallel, the cost of a bad
contract or a silent divergence is large — the discipline above prevents both.

You can shorten any week if both PRs land Tuesday and there's nothing to add. You can
extend a week if a contract amendment lands mid-week and pushes things. The schedule
is a target, not a contract.

The contracts file is the contract.
