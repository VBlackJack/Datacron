# ChatGPT 5.5 Pro — Independent review of Datacron v2.0

> **Reviewer**: ChatGPT 5.5 Pro
> **Date**: 2026-05-17
> **Context**: Cross-review request from Julien Bombled after Claude (Anthropic) produced
> the v2.0 architecture (README.md, SPEC.md DVS v1.0, ARCHITECTURE.md v2.0).
> Same prompt sent to Gemini Pro for comparative arbitration.

---

## Verdict

**Needs major pivot** — not away from MCP, but away from the "any MCP client / Cowork-first / 20-week platform" story: v1 should be a narrow Claude Desktop/Claude Code read-only retrieval gateway until host compatibility, retrieval quality, and write UX are proven.

## Decisions reviewed

1. **MCP server as hero**: 🟡 — Correct priority, but the hero should be "local context router," not "agent." Studio should stay dead until the CLI/MCP loop has daily usage; however, do not frame Cowork as equivalent to Claude Desktop unless you solve the local-vs-remote MCP connector problem.

2. **Decoupling via DVS**: 🟡 — The abstraction is directionally right, but DVS is too normative for v1 if it forces every note into required `id/title/created` frontmatter. Make DVS an overlay: read any Markdown vault, generate IDs in `.datacron/`, and only add canonical frontmatter when Datacron writes.

3. **L0-L5 trust model**: 🟡 — The six-level model is a decent internal policy lattice, but it is too much UX surface for a personal tool. Expose three user states: "auto-create," "review patch," and "dangerous operation"; keep L0-L5 behind the policy engine.

4. **FastMCP custom**: ✅ — Custom is justified because DVS, direct filesystem access, surgical write policy, audit logging, and non-Obsidian support are core constraints, not decorative features. Adapting an Obsidian server would save a week and cost you the architecture.

5. **LangGraph optional**: ⚠️ — Remove it from MVP entirely. "Optional" still means dependency surface, design coupling, docs, tests, and false promises; scheduled digests can be deterministic CLI jobs until you have evidence that agentic orchestration is needed.

6. **Retrieval stack**: ⚠️ — LanceDB + ripgrep is acceptable, but Contextual Retrieval in Phase 2 is premature gold-plating. Start with ripgrep + SQLite FTS/BM25 + hard citation discipline; add vectors only after a measured failure set proves lexical search is insufficient.

7. **Prompt injection mitigation**: 🟡 — Delimiters, top-k limits, path confinement, and "treat note content as untrusted data" are necessary; the classifier-on-every-read is likely latency theater. Put security budget into tool descriptions, write authorization, descriptor integrity, and preventing cross-tool exfiltration; recent MCP security work shows the client/tool layer is the weak point, not just note text.

8. **Multi-client compatibility**: ❌ — Reject as a v1 commitment. Claude Desktop/Claude Code local MCP is the only serious first target; Cowork/claude.ai remote connectors are cloud-brokered and Anthropic explicitly distinguishes them from local Desktop config, while OpenAI Apps SDK and Gemini/Code Assist have their own MCP host semantics rather than drop-in local stdio parity.

9. **Monorepo**: 🟡 — Keep one repo, but not five Python packages plus Rust/Tauri on day one. Use one Python package with subcommands and internal modules; leave `crates/datacron-studio` as a stub until you have users.

10. **Distribution channels**: ⚠️ — PyPI/pipx is primordial; Homebrew comes after repeated macOS installs hurt; Docker is mostly a CI/demo artifact; Tauri binaries can wait. Four channels before product fit creates release engineering debt without proving demand.

11. **8-phase roadmap**: ❌ — The roadmap is fantasy-scheduled for a solo project: Phase 0–3 alone contain parser, MCP server, indexer, embeddings, graph extraction, write transactions, Git safety, and prompt-injection mitigation. Cut everything after read-only retrieval plus maybe `_ai_generated/` staging; the published Phase 6–8 items are product/company work, not MVP work.

12. **Multi-machine sync — opinion**: Treat sync as explicitly out of scope for v1, but do not be "hands-off" once write tools exist. Git is a rollback mechanism, not a sync strategy; Syncthing/Obsidian Sync can mutate files behind Datacron and break `content_hash_before`, index freshness, and audit assumptions. The safe v1 rule is **single-writer vault**: Datacron writes only on one machine, secondary machines are read-only, and any external sync pattern must be documented as unsupported for concurrent writes.

## Top 3 risks Julien hasn't seen

1. **Cowork may break the core user story** — The architecture assumes local MCP works across Claude Desktop/Cowork/Code, but current Anthropic docs distinguish local Desktop MCP config from remote connectors, and remote connectors originate from Anthropic infrastructure rather than the local machine. Impact: the "local-first token router for Cowork" may not work without a public HTTPS endpoint/tunnel; mitigation: v1 targets Claude Desktop and Claude Code only, with Cowork marked unsupported until a working E2E exists.

2. **HITL is being delegated to clients that may not provide the UX** — Returning `needs_review` to an MCP client does not guarantee a diff viewer, typed confirmation, resumable approval, or consistent user intent capture. Impact: safe writes become either unusable or unsafe; mitigation: Datacron owns approval via `datacron review`, local web UI, or a minimal diff TUI before relying on Claude/Cursor/ChatGPT host behavior.

3. **Token efficiency is not the same as answer quality** — A 20–50× token reduction is only valuable if recall is high; one missed note destroys user trust faster than a high bill. Impact: users will revert to dumping whole folders; mitigation: build an evaluation harness from Julien's real vault with recall@k, citation precision, latency, token count, and "would I trust this answer?" labels before adding Contextual Retrieval or LangGraph.

## Top 3 simplifications I would make

1. **Read-only v1** — Ship only `list_notes`, `get_note`, `search_regex`, `search_text`, and maybe `search_hybrid` if vectors are trivial. No write path, no trust levels in UI, no LangGraph, no daemon, no Studio.

2. **DVS-lite** — Read any Markdown file; require no migration. Store generated IDs, hashes, and index metadata in `.datacron/`; write DVS frontmatter only for new AI-created notes or when the user explicitly runs `datacron normalize`.

3. **One package, one install path** — `pipx install datacron`, then `datacron init`, `datacron index`, `datacron mcp install --client claude-desktop`. No Homebrew, Docker, Tauri, LangSmith, OTel, or multi-client matrix until one client is excellent.

## What I'd build first if I were Julien (MVP scope, 4 weeks max)

A single Python package exposing a local stdio MCP server for Claude Desktop/Claude Code, read-only, with `list_notes`, `get_note`, `search_regex`, `search_text`, strict path confinement, bounded result sizes, deterministic citations, and an index stored under `.datacron/`. The only success metric: from Claude Desktop, ask 30 real questions against Julien's vault and beat manual folder dumping on answer quality, latency, and token cost. Everything else is noise until that works.

## One contrarian take (mandatory)

DVS should not be marketed as an open spec yet. Nobody is waiting to implement another vault standard; the winning wedge is "works on my messy existing Markdown folder without touching it." Make Datacron a ruthless context router first, and let DVS emerge later from write operations that users already trust.
