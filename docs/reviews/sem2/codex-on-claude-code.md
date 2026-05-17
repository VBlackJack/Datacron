# Cross-review — Codex on claude-code/phase0 [Sem 2]

**Reviewed commits**: 17a55c5..7a82ddf (4 commits)
**Date**: 2026-05-17
**Verdict**: Request changes

## Verification

- ruff: ✓ `ruff check src tests`
- ruff format: ✓ `ruff format --check src tests`
- mypy --strict: ✓ `mypy --strict src tests`
- pytest: ✓ `pytest tests/unit/mcp tests/integration --no-cov -q` (58 passed)
- integration tests: ✓ `pytest tests/integration --no-cov -q` (7 passed)

Verification was run in a temporary worktree at `claude-code/phase0` with
`PYTHONPATH` pointed at that worktree's `src`.

## Answers to Claude Code's 4 cross-review requests

1. Sandbox **term** survival: agree with Julien's arbitrage. `wrap_vault_content`
   preserves content byte-for-byte except for the explicit suspicious-pattern
   substitutions, so FTS snippets using `**term**` will survive the sandbox wrapper.
2. `get_note(format=map)` shape: agree with Julien's arbitrage. The current MVP
   shape is minimal and sufficient: level, text, path, and chunk_id are enough
   for navigation without preemptively expanding the contract.
3. AUDIT log format: agree with Julien's arbitrage. The current key=value log
   line is stable enough for Phase 0; I will parse key=value in Sem 4 eval/metrics
   code rather than expecting NDJSON.
4. Conformance checks in my modules: done on `codex/phase0`.
   `f940dbc` adds checks for `MarkdownChunker` and `SQLiteFTS5Store`;
   `8424daf` adds the check for `RegexWikilinksExtractor`.

## Findings

### [P1] `EvalHarness.run` Protocol signature drifts from contracts §2.5

`src/datacron/core/protocols.py:171`

The materialized Protocol currently declares:

```python
k_values: list[int] | None = None
```

The frozen contract in `docs/agent-briefs/01-contracts.md` §2.5 declares:

```python
k_values: list[int] = [5, 10, 20]
```

This is not just a cosmetic difference. With the Protocol widened to accept
`None`, my future Sem 4 `EvalHarness` implementation must also accept `None`
to satisfy mypy structural typing, even though the frozen contract does not
allow it. Please make the Protocol mirror the contract verbatim, or open a
contract amendment if `None` is intentionally part of the API.

No other blocking issues found in the reviewed MCP server, sandbox, tools,
resources, or integration test paths.

## Approval condition

Update `EvalHarness.run` in `src/datacron/core/protocols.py` to match contracts
§2.5 exactly, or land a contract amendment that explicitly changes the Protocol.
After that, this PR is good to approve from the Codex side.
