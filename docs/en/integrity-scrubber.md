# Integrity scrubber

**English** | [Français](../fr/integrity-scrubber.md)

The Datacron scrubber compares primary filesystem bytes with the exact-byte
SHA-256 stored in the completed index generation. It detects missing notes,
content changes, truncation, and NUL-bearing mismatches. It only alerts: it never
rewrites a note, repairs an index row, restores history, or replaces a canary.

## Execution model

The scrubber is an explicit incremental CLI operation, not an MCP background
thread. Schedule repeated invocations with Windows Task Scheduler, cron, or an
equivalent operator. Each invocation resumes the current pass and stops at the
configured duration boundary.

```text
datacron scrub-init --vault G:\_DATA
datacron scrub --vault G:\_DATA
```

`scrub-init` is a separate, explicit provisioning action. It creates missing
configured canaries atomically and refuses to overwrite any existing canary with
different bytes. `scrub` never initializes canaries. A missing canary is a
critical alert.

`scrub` exits with code 0 when the current window has no anomaly, code 2 when an
anomaly is present, and code 1 for an operational failure. A code 0 partial pass
must be invoked again until health reports complete coverage.

No MCP tool is added. `get_health` reads the checkpoint without starting work, so
certified read-only servers remain sidecar-read-only.

## Configuration

All operational limits and paths are runtime settings:

| Environment variable | Default | Meaning |
|---|---:|---|
| `DATACRON_SCRUB_NOTES_PER_SECOND` | `50` | Maximum average files opened per second, including canaries |
| `DATACRON_SCRUB_MEBIBYTES_PER_SECOND` | `16` | Maximum average primary bytes read per second |
| `DATACRON_SCRUB_MAX_DURATION_SECONDS` | `30` | Cooperative duration per invocation |
| `DATACRON_SCRUB_CHECKPOINT_INTERVAL_NOTES` | `25` | Notes between durable cursor checkpoints |
| `DATACRON_SCRUB_CHECKPOINT_PATH` | `.datacron/scrubber/checkpoint.json` | Vault-relative checkpoint path |
| `DATACRON_SCRUB_CANARY_DIR` | `.datacron/scrubber/canaries` | Vault-relative canary directory |
| `DATACRON_SCRUB_CANARIES` | JSON mapping | Relative canary names to exact UTF-8 content |

Paths must be vault-relative and cannot contain traversal. Canary names are safe
relative paths beneath the configured canary directory.

The default canaries are:

```json
{
  "exact-byte-lf.md": "# Datacron integrity canary\n\nformat: utf-8-lf\nsequence: 0123456789abcdef\n",
  "exact-byte-crlf.md": "# Datacron integrity canary\r\n\r\nformat: utf-8-crlf\r\nsequence: fedcba9876543210\r\n"
}
```

The expected content comes from configuration, while observed canary bytes come
from the configured vault-sidecar directory. For example, a custom mapping can be
provided as JSON with escaped EOL bytes.

## Checkpoint and resume

The ASCII JSON checkpoint records:

- pass ID and schema version;
- numeric index generation and deterministic path/ID/hash generation digest;
- sorted-path cursor, checked notes, checked bytes, and total notes;
- start, update, and last-completion timestamps;
- canary coverage and deduplicated anomaly records.

Resume requires the same numeric generation, generation digest, and note count.
An index change starts a new pass. Checkpoints use durable atomic replacement.
After an uncatchable crash, up to one configured checkpoint batch may be read
again; alert records remain idempotent and cannot be duplicated.

## Health contract

`get_health.scrubber` contains:

- `status`: `not_run`, `running`, `stale`, `complete`, or `critical`;
- `last_scrub`, `pass_id`, and `index_generation`;
- coverage as checked notes, total notes, fraction, and completion flag;
- checked bytes;
- anomaly count and path/type evidence;
- canary checked/total/healthy evidence.

Any scrub anomaly makes top-level health `critical`. With no scrub anomaly, ID,
broken-link, and mixed-EOL debt remains the separate cosmetic `degraded` state.
Mixed EOL is not a scrub anomaly when its exact current bytes match the index.

## Failure and trust boundary

Observed note and canary content is read directly from the real filesystem after
`VaultScope` authorization. Retrieval results and indexed chunk content are never
used as the observed byte channel.

The note hash authority is still the derived index. A coordinated change to both
a note and its index hash is outside this unauthenticated checksum guarantee.
Canaries detect a shared content-path regression for their known files, but do not
turn the whole index into a cryptographically authenticated ledger.

Every anomaly is written to FileLogger and the checkpoint. Human verification is
required before any corrective operation. This preserves the FM-O-01 rule: never
repair from an indirect integrity signal.

## Excluded-note decision audit

Generate the read-only index-gap report with configurable heuristic thresholds:

```text
python scripts/audit_excluded_notes.py \
  --vault-root G:\_DATA \
  --output local/excluded_notes_audit.md
```

The report does not move, modify, or index any note.
