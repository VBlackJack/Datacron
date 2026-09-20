# Daily workflows and measured validation

**English** | [Français](../fr/daily-workflows.md)

## Start with useful context

`session_context` prioritizes explicit `note_paths`, then subject candidates, then
configured startup notes. Domain filters still apply to discovered candidates.
Historical candidates are demoted by explicit lifecycle signals. The index is not
repaired by this read-only tool, so its candidate coverage remains explicit.

The complete memory contract remains the default. A client that already holds the
exact instructions can send `known_contract_hash` from the previous response. An
exact match returns `contract.delivery="unchanged"` with its ID, version and hash;
a missing or different hash returns the complete instructions. After context loss,
request the full contract again. The hash is a cache acknowledgement, not proof of
client compliance.

When only one excerpt remains over budget, the tool reduces that excerpt while
retaining the live hash, sandbox and exact `next_read` continuation. Explicit
section preferences may fall back to a bounded full-note excerpt under this pressure.
`omitted` and `truncated` remain authoritative; no response promises exhaustive coverage.

## Current state and history

Search demotes explicit `archived: true`, `status: archived`, and legacy
`memory/archive` or `meta/archive` frontmatter tags. Paths and modification times do
not establish archival status. Existing invalidation and supersession signals retain
priority. Historical results expose `lifecycle` as `archived`, `invalidated` or
`superseded`; missing lifecycle is not a certificate of current truth.

Use `set_frontmatter(rel_path=..., archived=true, expected_hash=..., request_id=...)`
to archive a note through the existing durable writer. `archived=false` clears that
field; any separate legacy archive tag/status must also be resolved before a note
is considered nonhistorical. The writer requires a current hash for this decision.
Archival does not delete or hide notes. `include_superseded=true` disables historical
demotion while preserving labels; direct reads remain available. The organization
tag policy remains unchanged; do not introduce undeclared tags to archive notes.

## Resume partial updates

Retain the `request_id`, target and arguments of every prepared write. `note` accepts
the vault-relative path or the note ULID, as `get_note_history` and `revert_note` do. Call:

```json
{"requests":[
  {"note":"project.md","request_id":"project-update-1"},
  {"note":"person.md","request_id":"person-update-1"}
]}
```

with `get_write_progress`. Optionally add each original `expected_hash`. Each item
has its 1-based `request_index`; counts summarize the batch. The tool is read-only
and does not claim an atomic snapshot across notes.

| Status | Meaning and next step |
|---|---|
| `committed_current` | Receipt matches current bytes and index. Reread the note. |
| `committed_changed` | The operation committed, then current bytes diverged. Read current state; do not repeat it. |
| `committed_reverted` | The operation committed and was then undone: current bytes are the ones it replaced. Replay the identical arguments with `expected_hash`. |
| `committed_index_incomplete` | Bytes match the commit but the index does not. Repair indexing without a new mutation. |
| `conflict` | No receipt found and the original CAS hash differs. Inspect the original request before preparing remaining work. |
| `not_recorded` | No committed receipt found. Inspect recovery or replay identical arguments with the same key. |
| `target_unavailable` | Current target could not be read. Inspect before retrying. |

An absent receipt returns `committed=null`, never proof that no pending operation
exists. Committed hashes are historical; `current_hash` is a point-in-time read.
All references are confined before journal evidence is returned: a reference that is
not an admitted live note, or that escapes the vault, is refused with the typed
`note_not_admitted` error before any item is inspected. Oversized requests are refused;
split them into smaller groups. No receipt permits a new write by itself.

## Understand diagnostics

`get_health` now includes `guidance`: stable codes, severity and operator actions
for stale indexes, identity mismatches, recovery blockers and disabled writes.
Recovery blockers take priority. Known tool errors include `next_action`; internal
errors retain their correlation ID without returning host paths. Client connection
state is not observable through these diagnostics: compare the returned server
version with the installed candidate and reconnect a client after replacement.

## Evaluate complete conversations

`tests/integration/test_daily_workflows.py` exercises actual MCP child processes,
reconnects, idempotent replay, partial completion, conflicts and corrections. The
existing memory-discipline scenarios also cover meetings, objectives and homonyms.
These are scripted protocol tests, not measurements of autonomous model behavior.

`examples/conversations/` supplies six provider-independent acceptance cases with
turn prompts. Run them through the client/model being evaluated, export each tool
exchange as one JSONL object with `session`, `tool`, `arguments`, `result`, and final
answers as objects with `session` and `answer`. Then run:

```text
uv run --frozen python scripts/evaluate_conversation_trace.py --case examples/conversations/project-resume.json --trace local/trace.jsonl
```

The grader checks session separation, required successful tools, source reads for
citations, exact acceptance/forbidden phrases and post-write read verification.
It exits nonzero on failure. Adapt paths and literal answers to the fixture before
the campaign, never after seeing failures. The supplied trace is evidence from its
exporter; these deterministic checks do not certify semantic truth. No model endpoint
is contacted automatically.

## Validate runtime behavior

```text
uv run --frozen python scripts/benchmark_sessions.py --sizes 100 1000 5000 --clients 3 --repeats 20
```

This creates disposable vaults and independent MCP processes. JSON reports version,
host, raw startup/search/write measurements, median and nearest-rank p95, plus errors.
Increase repeats for a longer soak. Initial search can include read repair; do not
compare it with a warm-only latency claim. Small-sample p95 often equals the maximum.
Any tool error, missing search hit or absent indexing confirmation fails the run.

Windows CI requires actual file/directory symlink capability before the full suite,
so privilege-based skips cannot silently stand in for that security evidence.
The manual `Runtime validation` workflow builds a candidate installer, installs and
reinstalls it on a disposable hosted Windows VM with Python removed from runtime
PATH, preserves fixture/configuration hashes and checks a fresh installed MCP
connection. It also records concurrent-session benchmarks as artifacts.

`scripts/verify_windows_install.py` refuses normal machines unless explicitly opted
in with both `--allow-install` and `DATACRON_DISPOSABLE_MACHINE=1`, and refuses an
existing Datacron installation/registry entry. Never use it on a working desktop.
Hosted Windows is not a pristine retail Windows image. Interactive installer pages,
accessibility and a true clean-image bootstrap still require separate VM evidence.
No successful remote run is implied by adding this workflow.

It runs seven scenarios, each in its own installation, and writes one entry per
scenario into the report. Six of them exist because a default install never
reaches the state that breaks: a vault path carrying an ampersand, one given with
a trailing backslash, one containing a double quote, a PATH entry the user wrote
by hand, a superseded vault whose unregistration fails, and a silent install with
no `/VAULT=`. Run a subset with `--only <scenario>`, repeated. The report lists
what it did not cover: choosing "Keep my current configuration" after passing
`/RESETCONFIG` is a wizard interaction and still needs a person.

For a clean Windows Sandbox image, use the candidate installer and standalone
validator from the workflow's `windows-sandbox-inputs` artifact:

```text
uv run --frozen python scripts/prepare_windows_sandbox.py --installer INPUT/Datacron-Setup.exe --validator INPUT/datacron-validation.exe --output local/sandbox-candidate
```

The destination must not exist. Open its `validate.wsb` on a host with Windows Sandbox
installed. Networking is disabled; executable inputs are mapped read-only and only
the dedicated results folder is writable. Inspect `results/exit-code.txt` (must be 0),
`results/install-evidence.json` and the log. The validator is self-contained, so no
Python installation in the clean guest is needed. Preparing the bundle does not
launch a VM or count as a successful clean-image test.
