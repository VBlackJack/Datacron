# User guide

**English** | [Français](../fr/user-guide.md)

This guide explains how to use Datacron day to day from Claude, once installation is done
(see the [Installation guide](setup.md)). Datacron is not an app with a UI: it is an MCP
server that your client (Claude Desktop or Claude Code) queries. You work in natural
language, and Claude calls the Datacron tools for you.

## The mental model in one minute

Datacron indexes your notes folder and, instead of sending the whole vault into the context,
returns only the relevant notes or fragments (chunks) to Claude. Concretely:

- You ask a question or request an action on your notes.
- Claude picks the right Datacron tool (search, read, write...).
- Datacron returns a bounded result (limited count and token budget), wrapped in
  `<vault_content>...</vault_content>`.
- Your notes stay ordinary Markdown files, editable by hand at any time.

## Tools, by use

### Read and search

| Tool | What it does |
|---|---|
| `list_notes` | Paginated list of notes, filterable by folder and tags; returns ULID, title, tags, aliases, and dates. |
| `get_note` | Reads a specific note by ULID, chunk id, or relative path; paginated content, single chunk, or heading outline. |
| `search_text` | BM25 search over the FTS5 index: ranked snippets, stale notes demoted by default. |
| `search_regex` | Literal regular-expression search via ripgrep, resolved to indexed chunks. |
| `get_backlinks` | Returns chunks whose wikilinks point to a given ULID or alias. |

### Write (if enabled)

These tools only work if `DATACRON_WRITE_PATHS` is set (see
[Installation guide, §8](setup.md#8-enable-writing-optional)). They are confined, atomic,
versioned, and audited.

| Tool | What it does |
|---|---|
| `create_note_ai` | Creates a new typed note without overwriting any existing file. |
| `append_journal` | Adds an entry under an existing heading of a note. |
| `set_frontmatter` | Updates lifecycle fields without touching the Markdown body. |
| `patch_note_section` | Replaces the content under an existing heading, with compare-and-set (CAS). |
| `revert_note` | Restores the exact bytes of a version kept in history. |

### Supervise

| Tool | What it does |
|---|---|
| `get_health` | Real state: index freshness, integrity, checksum, durability, invariants. |
| `get_note_history` | Metadata of a note's committed operations, without reading historical content. |
| `audit_query` | Queries the operation journal by period, tool, or note, read-only. |

Three MCP resources round out these tools: `datacron://vault/map` (vault map),
`datacron://vault/info` (metadata), and `datacron://policy/active` (active policy).

## How search works

`search_text` combines several signals, which is why results are not a plain "word match":

- **FTS5 / BM25** for the base lexical score.
- **FR↔EN query expansion** configured in `VAULT.yaml`: for example "sauvegarde" also
  surfaces notes that mention "backup".
- **Conservative temporal re-ranking**:
  - a note referenced in another note's `supersedes` field is strongly demoted;
  - `confidence: low` and `confidence: needs_verification` receive a light penalty;
  - `include_superseded=true` brings historical notes back up.

`search_regex` stays **literal**: no query expansion, no temporal re-ranking. Use it when you
are after an exact string (an identifier, a path, a snippet of code).

Rule of thumb: `search_text` for "what was I saying about X", `search_regex` for "where did I
write exactly this string".

## Note trust states

The notes' frontmatter carries signals that Datacron honors at ranking time. The most useful
day to day:

- `confidence: low` / `confidence: needs_verification` - the note is still considered but
  slightly demoted; handy to flag a draft or an item to verify.
- `supersedes: <ULID>` - designates the replaced note, which will be strongly demoted in
  everyday searches.

The result: you can keep history in the vault without polluting answers, while still being
able to recall it explicitly with `include_superseded=true`.

## Concrete requests

You phrase things in natural language; Claude translates them into tool calls. A few
examples:

- "What did I note about certificate rotation?"
  → `search_text` then `get_note` on the best results.
- "Show me the outline of the enterprise-deployment note."
  → `get_note(format="map")`.
- "Where did I write exactly `DATACRON_WRITE_PATHS`?"
  → `search_regex`.
- "Which notes link back to the security-boundary one?"
  → `get_backlinks`.
- "Add today's journal entry under 'Tracking' in the Datacron project note."
  → `append_journal` (requires writing enabled).
- "Set this note to confidence: low."
  → `set_frontmatter`.
- "Is the index fresh and intact?"
  → `get_health`.

## Best practices

- **Keep a single writer** on the vault: concurrent multi-machine writing is not supported.
- **Leave writing disabled** until you need it; enable it on a targeted subfolder (`_memory`,
  for example), not on the whole vault.
- **Edit freely by hand**: Datacron repairs the index on read, so your manual changes are
  picked up automatically.
- **Check via `get_health`** after a large change rather than guessing.
- **Retire stale notes** with `supersedes` instead of deleting them: you keep traceability
  without degrading search.

## Privacy

Datacron does no telemetry and calls no cloud LLM. However, the MCP client (for example
Claude Desktop) may itself forward to its provider the chunks that Datacron returns -
Datacron never sends it the whole vault, only the relevant, bounded fragments. Details:
[Security boundary](security-boundary.md).

## Going further

- [Installation and configuration guide](setup.md)
- [Architecture and public surface](architecture.md)
- [Vault conventions (SPEC)](spec.md)
- [Documentation index](index.md)
