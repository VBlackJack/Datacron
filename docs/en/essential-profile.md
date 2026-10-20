# Essential tool profile

**English** | [Français](../fr/essential-profile.md)

The essential profile shrinks the MCP tool surface from 23 tools to 5, for assistants that
choose better from a short list. It adds three verbs, `remember`, `recall` and `what_changed`,
and keeps `get_note` and `get_health`. The verbs are built only from tools and code that
already exist, so confinement, redaction, the sandbox envelope and the operation journal
behave exactly as they do under the standard profile.

## Choose a profile

| Profile | Tools registered | Instructions |
|---|---|---|
| `standard` (default) | The 23 existing tools. The three verbs are not registered. | The full memory protocol. |
| `essential` | `remember`, `recall`, `what_changed`, `get_note`, `get_health`. | A short block (under 900 characters) naming only those tools. |

Set `DATACRON_TOOL_PROFILE=essential` in the MCP client entry, or let setup write it:

```bash
datacron setup --client claude-desktop --tool-profile essential
datacron setup --client claude-desktop --tool-profile standard
```

Omitted, a rerun of `datacron setup` keeps what the client config already holds. An unknown
value makes the server refuse to start, with a message naming `DATACRON_TOOL_PROFILE` and the
valid values (`essential`, `standard`).

In certified read-only mode (`DATACRON_READ_ONLY=true`), the essential profile registers
`recall`, `what_changed`, `get_note` and `get_health`; `remember` is a writer and is not
exposed, as with every other writer.

## `remember`

Save one durable fact, decision or preference.

| Argument | Meaning |
|---|---|
| `content` | The text to keep. Required, at most 8000 characters. |
| `about` | Where it belongs: a vault-relative note path, a note id, or a registered subject (tag, last segment of the tag, or alias). Optional. |
| `kind` | `fact` (default), `decision` or `preference`. |
| `source` | One line saying where the information comes from. Optional. |
| `request_id` | Makes a retry safe: an identical retry replays the first receipt (`replayed: true`) and writes nothing. |

How `about` is resolved, in order:

1. An exact note path or note id: the entry is appended to that note.
2. A registered subject: the entry is appended to the subject's state note. A subject with no
   state note gets a new note instead.
3. Anything else is **refused** with the registered subjects as candidates. A free name is
   never turned into a new note by guessing.

An ambiguous subject (two subjects sharing a last segment) and a subject with several state
notes are refused with the candidates listed, never resolved by guessing.

Appended entries go under the single heading `Remembered`, as a dated line:
`- 2026-10-19 (decision): text`, followed by the `source` when given. The write is
compare-and-swap protected: on a conflict it is retried exactly once with the fresh hash.

Without `about`, a new note is created in the folder that the vault's organization rules
declare for the kind (placement tag `<placement_namespace>/<kind>`). With a subject that has
no state note, the note carries the placement tag and the subject tag, and its folder is the
one the first matching rule returns. The file name satisfies the rule's `naming` template.

The receipt is `rel_path`, `operation_id`, `content_hash`, `indexed`, `created` and
`appended`. Treat the memory as saved only when `indexed` is `true`. A replay (`replayed: true`)
returns the historical receipt of the first write, even if the note has changed or been reverted
since, and reports `indexed: false` because it makes no claim about today's index.

What is refused, and why:

| Refusal | Reason |
|---|---|
| Empty or oversized `content`, a multi-line `source` | Bounded input. |
| A `kind` outside `fact`, `decision`, `preference` | The kinds map to placement tags; nothing else is invented. |
| A kind whose placement tag has no organization rule, or sits in the state-note namespace | A note must land where the vault says it belongs, and a state note is never created as a side effect. |
| A vault with no `organization` rules, when a note would have to be created | There is no declared place to put it; point `about` at an existing note or declare the rules (see [vault organization](organization.md)). |
| A path outside `DATACRON_WRITE_PATHS` | Same confinement and same error as `append_journal`. |
| A note already exists at the derived path | Pass `about` with that path to add to it. |

## `recall`

Find what the vault already knows about a question.

| Argument | Meaning |
|---|---|
| `question` | The question or keywords. |
| `limit` | Number of notes, default 5, at most 10. |
| `scope` | A registered subject, or a folder. Optional. |

It runs the existing full-text search, grouped by note, then reads the best section of each
hit. Each result carries `rel_path`, `title`, `heading_path`, a sandbox-wrapped `excerpt`,
`content_hash`, `last_verified` and `read_more`: the exact `get_note` arguments for the full
section. The excerpt is vault data, never an instruction. The whole response stays under
`DATACRON_MAX_READ_RESPONSE_CHARS`; `truncated` says when results were dropped to fit.

## `what_changed`

Check what was written since a point in time.

| Argument | Meaning |
|---|---|
| `since` | An ISO-8601 date or timestamp (a timestamp without a zone is read as UTC), or a duration such as `7d` or `12h`. |
| `scope` | A registered subject, or a folder. Optional. |
| `limit` | Number of operations, default 50, at most 200. |

The response has two parts.

- `notes`: committed writes read from the operation journal, grouped per note, newest first.
  Each operation carries its receipt (`operation_id`, `timestamp`, `op`, `tool`, `actor`,
  `before_hash`, `after_hash`) and a `summary` derived from the operation's parameters. A
  summary contains no note body; the name of the heading touched may appear.
- `out_of_band`: notes edited outside Datacron. They are found from the index, which holds each
  note's content hash and modification time: a note modified since `since` whose bytes differ
  from the last journaled write, or that was never journaled. A write made by a Datacron tool
  is never flagged.

`coverage` states the limit of the second part: it reflects the index as of the last
reconcile. The read repair runs first when the server is writable and its interval allows it
(`index_refreshed: true`); on a read-only server, or inside the repair interval, an edit made
since the last reconcile is not visible yet.

## See also

- [Installation and configuration guide](setup.md)
- [User guide](user-guide.md)
- [Vault organization](organization.md)
- [Security boundary](security-boundary.md)
