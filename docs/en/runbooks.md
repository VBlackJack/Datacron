# Runbooks

**English** | [Français](../fr/runbooks.md)

A runbook is one folder per subject, one Markdown file per numbered theme
(`00-architecture`, `50-troubleshooting`, `90-variables`, and so on) and a
`quickref/` folder of per-service survival sheets. A vault can declare where its
runbooks live and what they look like, and `runbook_capture` then files each
durable fact in the right runbook and the right section, as soon as work
establishes it.

## The `runbooks` block

The block is optional. A vault without it behaves exactly as before. Unknown keys
are refused at startup, so a misspelled key fails loudly instead of being dropped.

```yaml
runbooks:
  roots:
    - folder: CPDC                       # vault-relative; its *-runbook children are runbooks
      directory_naming: "{slug}-runbook" # default
      template: infra
    - folder: Lab/zabbix-runbook         # or: this folder is one runbook
      layout: runbook
      template: infra
  protected_paths: [knowledge]           # always refused; empty by default
  templates:
    infra:
      language: en
      readme: README
      observation_label: On Mets
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$', title: Architecture}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', title: Troubleshooting}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$', title: Escalation}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$', title: Variables}
      quickref: {folder: quickref, language: fr}
      labels:
        symptom: Symptom
        cause: Cause
        fix: Fix
        not_captured: Not captured
        update: Update
        machine: Machine
        output: Output
        placeholder: Placeholder
        meaning: Meaning
        value: On Mets
        decisions: Decisions
        reason: Reason
        discarded: Discarded options
        open_questions: Open questions
        owner: Owner
      routing:
        error_fix: troubleshooting
        contact: escalation
        server: variables
        variable: variables
        decision: architecture
        fact: architecture
```

### Roots

| Key | Meaning |
|---|---|
| `folder` | Vault-relative folder. It must not lie in a protected path, and no segment may be an excluded directory (see below). Two roots must not overlap. |
| `layout` | `parent` (default): the folder's children named by `directory_naming` are runbooks. `runbook`: the folder is itself one runbook, and its own name must follow `directory_naming`. Declaring each repository as a `runbook` root keeps everything beside them out of reach. |
| `directory_naming` | Name of a runbook folder. It holds `{slug}` exactly once plus a literal part, so not every child folder of a root is a runbook. Default `{slug}-runbook`. |
| `template` | Name of a declared template. |

`protected_paths` (beside `roots`) lists vault-relative paths the runbook writer always
refuses, whatever `DATACRON_RUNBOOK_WRITE_PATHS` allows, compared without case. It is empty
by default: Datacron knows no folder of your vault in advance, so a vault that keeps
read-only material (a knowledge base, say) declares it here.

### Templates

| Key | Meaning |
|---|---|
| `language` | Language tag of the numbered files (`en`, `fr`). Every receipt repeats it, so the caller writes the entry in that language. |
| `readme` | Stem of the index file, without `.md`. No role may use it; the capture never writes it. |
| `roles` | One entry per file role (lowercase identifier). |
| `quickref` | Optional: `folder`, its own `language`, and `naming` with `{service}` exactly once (default `{service}`). The folder is reserved; the capture does not write survival sheets yet. |
| `observation_label` | Label of the dated observation boxes, for example `On Mets`. |
| `labels` | Every word Datacron renders, in the template language. A capture whose kind needs a label the template does not declare is refused, naming it. Datacron ships no runbook text. |
| `routing` | Capture kind to role: `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. A kind left out has no default role and a capture of it must name one. |

### Roles

A role is found by the pattern its file stem matches, not by its number: the WSUS
runbook files troubleshooting under `50-troubleshooting`, the AD runbook under
`70-troubleshooting`, and both match `^\d{2}-troubleshooting$`.

| Key | Meaning |
|---|---|
| `stem` | File stem a creation uses, without `.md`. It must match its own `match`, and no other role's pattern, or the next capture could not find the file again. |
| `match` | Regular expression matched against the whole stem. A pattern with nested repetition is refused. |
| `title` | The `# ` title of a file this role creates. Without it, the capture refuses to create the file and only writes into an existing one. |

## Capturing a fact: `runbook_capture`

The tool is registered only when the vault declares `runbooks`,
`DATACRON_RUNBOOK_WRITE_PATHS` is set, the server is not read-only, and the tool
profile is `standard`. Otherwise the tool list is unchanged.

| Argument | Meaning |
|---|---|
| `subject` | A registered subject (tag, last segment or alias), the slug of an existing runbook, or a runbook folder path such as `CPDC/wsus-runbook`. |
| `kind` | One of the eight kinds below. |
| `evidence` | `measured` (a command actually run, its real output), `said` (a dated meeting or a named person) or `unverified`. |
| `source` | One line saying where the fact comes from. It is kept in the operation journal, not in the runbook. |
| `fields` | The fields of the kind, below. An unknown field is refused. |
| `observed_on` | Date of the event, `YYYY-MM-DD`, not in the future. Default: today (UTC). |
| `target_role` | A role that overrides the routing. |
| `dry_run` | `true` returns the plan and writes nothing. |
| `request_id` | Makes a retry safe: an identical retry replays the first receipt. |

| Kind | Required fields | Optional fields | Written as |
|---|---|---|---|
| `error_fix` | `symptom`, `fix` | `cause` | An H2 `Symptom: <symptom>` with `Cause`, `Fix` and the provenance. A missing cause is written with the `not_captured` label. |
| `command` | `machine`, `heading`, `command`; `output` when measured | `syntax`, `phase` | The machine, the command in a fence, its real output or `not_captured`, under `heading`. `phase` names the role when `target_role` is absent. |
| `server`, `variable` | `placeholder` (`<UPPER_SNAKE>`), `meaning`, `value`, `heading` | | A row of the table under `heading`, keyed by its placeholder. |
| `contact` | `name`, `role`, `channel`, `heading` | | A list item under `heading`. |
| `decision` | `decision`, `reason` | `discarded` | A dated list item under the `decisions` label. |
| `fact` | `heading`, `fact` | | A dated observation box (`> **On Mets, <date>.**`) under `heading`. |
| `open_question` | `question`, `owner` | | A list item with its owner under the `open_questions` label. |

### Routing

Each step refuses rather than guesses, and the refusal lists the candidates:

1. **Runbook.** The subject names one runbook folder. A `runbook` root is created on
   its first capture if its folder does not exist yet. Otherwise an unknown word never
   creates a runbook; a registered subject or a folder path may, under the only `parent`
   root, or under the `parent` root its path names.
2. **Role.** `target_role`, else `phase` for a command, else the template routing.
3. **File.** The one file whose stem matches the role's pattern. None: the role's
   `stem` is created, if the role has a `title`. Several: refused.
4. **Section.** The H2 the kind names. It is matched without case or spacing
   differences, and created at the end of the file when absent.

### What a capture does to a file

- **Provenance** is one neutral mark per entry, `<measured|said|unverified> YYYY-MM-DD`,
  with no tool, assistant or session named.
- **Already present** (the same text, whatever its provenance date): nothing is
  written, the receipt says `duplicate_skipped`.
- **A known symptom with new content** gets a dated `Update` paragraph in its
  section; the existing entry is kept as it was.
- **A table row** with the same placeholder is replaced in place; the previous
  bytes stay in the note history.
- **Everything else** is appended. An observation box carries its date, so the same
  fact observed again later is a new box, never an edit of the old one.
- **Frontmatter**: a file without one keeps none. A file with one gets
  `last_verified` only from a `measured` capture, and `updated` only when it already
  has that key. `origin` and `confidence` are never written.
- **Secrets**: a field that holds what looks like a secret (the redaction detector and
  `DATACRON_SECRET_REDACTION_PATTERNS`) is refused, without echoing it. It is refused
  rather than masked: a masked command is a wrong command. A thumbprint or fingerprint
  written as `thumbprint=...` is refused too; write it with a placeholder.
- **Structure**: prose that Markdown would read as a heading is refused, and every
  write is checked to leave every other heading of the file readable.

The receipt gives `runbook`, `rel_path`, `role`, `heading`, `action` (`created`,
`appended`, `updated`, `duplicate_skipped`), `language`, `runbook_created`,
`written`, and for a write `content_hash`, `operation_id` and `indexed`.

### How the habit reaches the model

The capture is meant to happen as work goes, not on request. Three server-side
signals carry that, and none of them costs a client rule file:

- **Server instructions.** When the server offers `runbook_capture`, its instructions
  are the memory protocol block followed by one runbook paragraph and nothing else.
  Without the tool they are exactly the protocol block. The paragraph is not part of
  the memory contract: its version and hash do not change, and `datacron protocol
  install` never writes it into a client's rules, whose budget (6000 characters for
  Windsurf) the block nearly fills already.
- **Tool description.** The description of `runbook_capture` repeats when to call it,
  the three provenance values and the refusal of guesses and secrets, for clients that
  ignore server instructions.
- **`session_context`.** With a `subject` that names one existing runbook, the result
  carries `runbook: {path, language}`. It is attached after the sources are fitted and
  dropped when it does not fit, so it never costs a source.

### Undoing a capture

Every write is journaled: `get_note_history` and `audit_query` list it. `revert_note`
does not reach runbooks, because it is bound to `DATACRON_WRITE_PATHS`. When a runbook is a
git working tree, `git diff` shows the capture and `git restore` undoes it.

## Write boundary

Declaring a root does not make it writable. Runbook writes have their own setting:

| Variable | Default | Role |
|---|---|---|
| `DATACRON_RUNBOOK_WRITE_PATHS` | empty | Runbook write allowlist, separated by the OS path separator. **Empty = no runbook is writable.** Only the runbook capture reads it; the generic write tools keep using `DATACRON_WRITE_PATHS` and never read this one. |

A runbook file is writable only when all of these hold, judged after the path is
resolved (a `..` segment is followed first):

1. it lies inside `DATACRON_RUNBOOK_WRITE_PATHS` and inside the vault;
2. it lies inside a declared runbook: a `runbook` root, or a child of a `parent` root
   named by `directory_naming`;
3. it is a Markdown file;
4. it is not under one of the vault's `protected_paths`;
5. no folder on its path is `.git`, `confluence-source` or `confluence-updates`.

Rule 5 is fixed: a vault cannot declare it away. Those three generic names are the only
folder names Datacron knows in advance; every other name, protected ones included, comes
from the vault. Rules 4 and 5 compare without case on every filesystem. The write itself then goes through the
ordinary writer, with its lock, its journal, its note admission and its refusal of a
path that crosses a symbolic link or a junction.

Datacron never runs git in a runbook: it writes files, and reviewing the diff and
committing stay manual.
