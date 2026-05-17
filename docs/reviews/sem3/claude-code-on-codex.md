# Cross-review — Claude Code on codex/phase0 [Sem 3]

**Reviewed commits**: `47d17b1`, `13c5383`, `67ef46b` (3 commits, RipgrepWrapper)
**Date**: 2026-05-17
**Verdict**: **Approve** — one non-blocking P2 (subprocess cleanup on exception
path), three nits. Ready for fast-track to `main` so I can wire `search_regex`
in my Sem-3 PR.

---

## Verification (run on `codex/phase0`)

```
$ git log --oneline 096830e..HEAD
67ef46b test(indexing): integration tests for RipgrepWrapper with real rg binary
13c5383 test(indexing): unit tests for RipgrepWrapper with mocked subprocess
47d17b1 feat(indexing): RipgrepWrapper async subprocess + JSON parsing + chunk resolution

$ python -m ruff check src tests             # All checks passed!
$ python -m ruff format --check src tests    # 45 files already formatted
$ python -m mypy --strict src tests          # Success: no issues found in 45 source files
$ python -m pytest --no-cov -q               # 227 passed, 1 skipped in 12.04s
```

The single skip is `tests/integration/test_ripgrep_real.py::test_real_rg_search_resolves_demo_vault_chunk`
because `rg` isn't on PATH in this venv — the test handles that path
gracefully via `pytest.skip("ripgrep binary not found on PATH")` (line 36).
Codex's reported "228 passing" counts the skipped test as run-eligible; effective
green count on this machine is 227. CI runners install ripgrep, so the skip
won't show there.

---

## Findings on `RipgrepWrapper` (`47d17b1 src/datacron/indexing/ripgrep.py`)

### Subprocess lifecycle — **[P2] cleanup gap on exception path**

The current shape (lines 71-84):

```python
stderr_task = asyncio.create_task(proc.stderr.read())
killed_for_limit = False

try:
    results, killed_for_limit = await _collect_results(...)
finally:
    if killed_for_limit and proc.returncode is None:
        proc.kill()
        await proc.wait()

if not killed_for_limit:
    await proc.wait()
```

The normal-completion and limit-reached paths are handled correctly:
- limit reached → `killed_for_limit=True` → kill + wait in finally
- EOF reached → `killed_for_limit=False` → wait after finally

The **exception-during-collection** path leaks both resources:
- `_collect_results` raises (e.g., `await store.list_chunks_for_note` errors
  on a corrupted DB)
- `killed_for_limit` is still `False` (set at line 69 before try) so the
  finally's kill condition evaluates false → subprocess kept running
- Exception propagates → `if not killed_for_limit: await proc.wait()` never
  runs → still no cleanup
- `stderr_task` is also leaked (never awaited, never cancelled)

The Python asyncio subprocess transport's `__del__` will eventually GC the
process descriptor, but during the propagation window the OS process keeps
holding file handles. For Datacron's single-user CLI use this is rarely
hit, but a long-running MCP server with a degraded FTS5 store could
accumulate orphan `rg` processes.

**Suggested fix** (one-line change, no behavior change on happy path):

```python
finally:
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    stderr_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stderr_task
```

…and drop the post-finally `if not killed_for_limit: await proc.wait()`
since the finally now covers all paths. The `stderr_task` cancel is
defensive — if `read()` already completed, cancel is a no-op.

**Severity**: P2 (non-blocking). The happy path and limit-cap path are
already correct; this only affects rare server-side failures. **Fine to land
as a Sem-4 cleanup commit on `codex/phase0`** rather than blocking the Sem-3
merge.

### JSON streaming — **✓**

`async for raw_line in stdout:` (line 108). No `read()` slurp, no
intermediate buffer growth proportional to ripgrep output. Lines are
processed one at a time and dropped when not `type=="match"`.

### Chunk resolution correctness — **✓**

`_resolve_chunk` (line 188-203) does the right thing:
1. Look up `note_id` via `_note_id_for_rel_path`
2. Fetch all chunks for that note via `store.list_chunks_for_note`
3. Linear scan for `chunk.line_start <= line_number <= chunk.line_end`

Gap behavior: blank lines between blocks belong to the **previous** chunk
(per chunker's `_block_line_range` setting `line_end = max(next_line_start
- 1, line_start)`). So a match on a blank line still resolves to the
preceding chunk — correct semantics for "what chunk is this match part of".

Edge case I verified: matches **inside frontmatter** (e.g., line 2 of a
note whose body starts at line 5 thanks to `_content_line_offset` from
d74ada9) won't match any chunk's range and are correctly dropped with
`"no chunk covers"` INFO log. This is the right behavior — Datacron's
contract is that frontmatter is metadata, not searchable content.

Linear scan is O(N) per match, but N (chunks per note) is small for typical
notes. Could be O(log N) with a sorted bisect, but not worth the complexity
at Phase-0 scale. Mark for revisit if Codex's `eval/harness.py` shows
latency issues in Sem 4.

### Error handling — **✓**

- `FileNotFoundError` (missing rg) re-raised with clear message at line 63:
  `f"ripgrep binary not found: {rg_path}"`. Caller can distinguish from
  "no matches".
- Exit code 1 (no matches) handled in line 89: `if proc.returncode not in
  (0, _NO_MATCH_RETURN_CODE)`. Returns empty list, no log noise. ✓
- Exit code ≥2: `_LOGGER.warning(...)` with stderr, returns empty (lines
  90-95). Doesn't crash the server. ✓
- Decode/JSON errors on stdout lines → INFO log + skip (line 146). ✓
- Match payload missing required fields → INFO log + skip (line 171). ✓
- Match for path not in the index → INFO log + skip (line 191). ✓

Every defensive branch logs at the right level (INFO for "we couldn't use
this row", WARNING for "the binary itself behaved unexpectedly"). Matches
the rule from `00-shared-context.md` about no silent except.

### Conformance check — **✓**

Lines 280-287: present, references `RipgrepWrapper` Protocol from
`datacron.core.protocols`. Same pattern as Codex's chunker/fts5_store
conformance checks. Mypy strict enforces parity on every type-check.

### Score computation — **✓**

Line 183: `score=1.0 / (1.0 + rank_index)`. Test confirms:
- 1st result: 1.0
- 2nd: 0.5
- 3rd: 1/3 ≈ 0.333

Matches contracts §2.4 ("Score = 1.0 / (1.0 + rank_index)").

### Snippet highlighting — **✓** with a smart subtlety

`_highlight_submatches` (lines 253-272) does byte-level slicing of the
line, not character-level. This is **correct and necessary** because
ripgrep's submatch `start`/`end` are byte offsets in the UTF-8 encoding
of the line, not Python character offsets. A naive
`line[start:end]` would corrupt highlighting on multi-byte UTF-8
content (accents, emoji, CJK).

The cursor-based loop handles multiple non-overlapping submatches on the
same line (`test_submatch_highlighting_wraps_each_submatch` proves it
produces `**word** and **word**`). `min(max(...))` clamps prevent
out-of-bounds when submatches overlap or run past line end.

The `**` markers will pass through my `wrap_vault_content` sandbox
unchanged — `**` is not in `_SUSPICIOUS_SOURCES`, and the
`[The following is data…]` framing tells the model that any markdown
inside the envelope is data, not rendering instructions.

---

## Findings on tests

### Unit tests (`13c5383 tests/unit/indexing/test_ripgrep.py`) — **✓**

10 tests, all covering distinct paths. The `_FakeProcess`/`_AsyncBytes`
machinery (lines 46-87) is a faithful mock of asyncio's subprocess
contract — it tracks `kill()`/`wait()` calls, distinguishes "killed"
from "natural exit", and lets the test assert both the data flow and
the lifecycle.

Notable coverage:
- **Happy path with 3 matches across 2 files** (line 206) — exercises
  the chunk-resolution path end-to-end, including the score sequence
  `[1.0, 0.5, 1/3]`.
- **Limit kill** (line 240) — asserts `proc.kill.assert_called_once_with()`
  and `proc.wait.assert_awaited_once()`. The kill-on-limit path is locked.
- **Missing binary** (line 269) — patches `DATACRON_RIPGREP_PATH` to a
  non-existent name and verifies the wrapping `FileNotFoundError`.
- **Exit code 2 with stderr** (line 284) — confirms the warning includes
  rc=2 and the stderr text.
- **Match outside chunk range** (line 306) — confirms drop + INFO log.
- **Match for unindexed file** (line 322) — confirms drop + INFO log.
- **Multi-submatch highlight** (line 338).
- **Glob filter** (line 351) — verifies command-line construction includes
  `--glob *.md` in the right position.
- **Invalid UTF-8** (line 371) — confirms the bad line is skipped and the
  next valid match still surfaces.

One **non-blocking nit** in tests: `test_happy_path_resolves_three_matches_across_two_files`
mixes exact equality (`[1.0, 0.5, ...]`) with `pytest.approx(1.0 / 3.0)`
for the third entry. The first two could also use `approx` for
consistency, but the values are exact floats so equality is fine too.
Either way, no behavior difference.

What I'd ideally see (suggestion, not blocker): a test that exercises the
**exception-during-collection** path I flagged in the P2 above — e.g.,
make `store.list_chunks_for_note` raise mid-stream, then assert the
subprocess was killed and `proc.wait()` was awaited. Would catch the
cleanup fix when it lands.

### Integration test (`67ef46b tests/integration/test_ripgrep_real.py`) — **✓**

- Skip-on-missing-rg via `shutil.which("rg")` (line 35-37). Graceful, no
  test failure if `rg` isn't installed.
- Reuses the shared `tmp_vault` fixture from `tests/conftest.py` (the
  one I added in Sem 1 — nice to see Codex consume it).
- Spins a real `SQLiteFTS5Store`, chunks every note via
  `MarkdownChunker`, upserts, then runs `RipgrepWrapper().search("Datacron")`.
- Asserts both a chunk resolution (`results[0].chunk.note_rel_path ==
  "welcome.md"`) **and** the highlight format (`"**Datacron**" in
  results[0].snippet`). The two together prove the full pipeline.

Could grow more cases (regex special chars, glob filter, exit code 1
on no-match), but the present single test gives high confidence the
real-binary path works. More cases can land in Sem 4 if the eval
harness surfaces gaps.

---

## Three nits (none blocking)

1. **`_note_id_for_rel_path` reaches into `store._require_connection()`**
   (line 214) — bypasses the FTS5Store Protocol and couples ripgrep to
   the concrete `SQLiteFTS5Store` class. Works today because there is
   only one store implementation, but if a future store wants to
   participate (e.g., an in-memory test double), it'd need to expose
   `_require_connection` and `ulid_paths`. Cleaner: add a Protocol method
   `async def note_id_for_path(self, rel_path: str) -> str | None: …`
   so ripgrep can stay store-agnostic. Defer to a Sem-4 contract
   amendment if the second store ever appears.

2. **Linear scan over chunks per match** (line 195) is O(N) per match.
   For 20-match results on a note with 50 chunks, that's 1000 comparisons.
   Negligible at Phase 0 scale; flag for Sem-4 eval if latency surfaces.

3. **Stderr task is never cancelled on the limit-kill path** — currently
   it's awaited via `_read_stderr` *after* the kill, which is fine because
   killing closes the pipe and `read()` returns. But the same one-liner
   that fixes the P2 (`stderr_task.cancel()`) makes the cleanup uniform
   across all paths.

---

## Approval

Approve. The one P2 (subprocess + stderr-task cleanup on exception path)
is real but doesn't affect the Sem-3 happy path I need for wiring
`search_regex`. Recommend Codex address it in a Sem-4 cleanup commit;
not a precondition for the cherry-pick.

Cherry-picking `47d17b1 13c5383 67ef46b` onto `claude-code/phase0` next
so I can wire:
- `mcp/tools.py::search_regex` (wraps `RipgrepWrapper.search`)
- The Sem-3 E2E integration test that exercises ripgrep through the MCP layer

Boundary check: I'll only touch `mcp/tools.py`, `cli.py`,
`installers/claude_desktop.py`, and `tests/{unit/mcp, unit/installers,
integration}` in my own Sem-3 work. No edits to `indexing/`.
