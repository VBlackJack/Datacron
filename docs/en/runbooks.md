# Runbooks

**English** | [Français](../fr/runbooks.md)

A runbook is one folder per project, one Markdown file per numbered theme
(`00-architecture`, `50-troubleshooting`, `90-variables`, and so on) and a
`quickref/` folder of per-service survival sheets. Datacron keeps runbooks in one
folder of your vault, the runbook vault. When a conversation is about a project,
`runbook_capture` files each durable fact in that project's runbook and the right
section, as soon as work establishes it, and creates the runbook from your template
when the project has none yet.

## Setting up the runbook vault

The runbook vault is optional. Without one, runbooks go to `_runbooks`, beside
`_memory`: in the memory vault when you use one, at the vault root otherwise.

To choose another folder, interactive `datacron setup` asks for it (Enter keeps the
current setting or the default), the Windows installer has a third, optional folder
field on its vault page, and the command line takes it directly:

```powershell
datacron setup --vault C:\Notes --runbook-vault C:\Notes\Runbooks
```

`setup` writes `DATACRON_RUNBOOK_VAULT` into the client entry; a rerun without the
option keeps it. You can also set the variable yourself:

| Variable | Default | Role |
|---|---|---|
| `DATACRON_RUNBOOK_VAULT` | `_runbooks` beside `_memory` | The folder that holds runbooks, one sub-folder per project. |

The runbook vault must lie inside the served vault: any folder of the documentation
vault, or `_runbooks` (or a folder under it) in the memory vault. Its files then share
the vault's paths, locks and operation journal, so a recovery after a crash finds them
where they were written. Another folder is refused by `setup`, and the server logs a
warning and does not offer the capture. In Dual-Vault mode a `_runbooks` folder of the
documentation vault is masked by the memory vault's, as `_memory` is. The name of a
folder you choose is yours; Datacron knows none in advance.

## The `runbooks` block

The block in `.datacron/VAULT.yaml` says what a runbook looks like. It is optional:
a vault without it behaves exactly as before, and the capture is not offered.
Unknown keys are refused at startup, so a misspelled key fails loudly instead of
being dropped.

```yaml
runbooks:
  directory_naming: "{slug}-runbook"   # default
  default_template: infra              # required with several templates
  assignments: {bench: lab}            # optional: runbook slug -> template
  protected_paths: [shared/archive]    # relative to the runbook vault; empty by default
  templates:
    infra:
      language: en
      readme: README
      observation_label: On site
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$', title: Architecture}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', title: Troubleshooting}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$', title: Escalation}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$', title: Variables}
      quickref: {folder: quickref, language: fr}
      labels:
        readme_title: "{slug} runbook"
        documents: Documents
        skeleton: "Not captured yet."
        symptom: Symptom
        cause: Cause
        fix: Fix
        not_captured: Not captured
        update: Update
        machine: Machine
        output: Output
        placeholder: Placeholder
        meaning: Meaning
        value: Value
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

| Key | Meaning |
|---|---|
| `directory_naming` | Name of a runbook folder in the runbook vault. It holds `{slug}` exactly once plus a literal part, so not every folder there is a runbook. Default `{slug}-runbook`. |
| `templates` | One or more templates (below). |
| `default_template` | The template of a runbook with no assignment. Required when several templates are declared. |
| `assignments` | Optional map from a runbook slug to the template it uses, for runbooks that do not follow the default. |
| `protected_paths` | Paths, relative to the runbook vault, the runbook writer always refuses, compared without case. Empty by default. |

### Templates

| Key | Meaning |
|---|---|
| `language` | Language tag of the numbered files (`en`, `fr`). Every receipt repeats it, so the caller writes the entry in that language. |
| `readme` | Stem of the index file, without `.md`. No role may use it. |
| `roles` | One entry per file role (lowercase identifier). |
| `quickref` | Optional: `folder`, its own `language`, and `naming` with `{service}` exactly once (default `{service}`). The folder is reserved; the capture does not write survival sheets yet. |
| `observation_label` | Label of the dated observation boxes, for example `On site`. |
| `labels` | Every word Datacron renders, in the template language. A capture whose kind needs a label the template does not declare is refused, naming it. Creating a runbook needs `readme_title` (where `{slug}` is replaced), `documents` and `skeleton`. Datacron ships no runbook text. |
| `routing` | Capture kind to role: `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. A kind left out has no default role and a capture of it must name one. |

### Roles

A role is found by the pattern its file stem matches, not by its number: one
runbook files troubleshooting under `50-troubleshooting`, another under
`70-troubleshooting`, and both match `^\d{2}-troubleshooting$`.

| Key | Meaning |
|---|---|
| `stem` | File stem a creation uses, without `.md`. It must match its own `match`, and no other role's pattern, or the next capture could not find the file again. |
| `match` | Regular expression matched against the whole stem. A pattern with nested repetition is refused. |
| `title` | The `# ` title of a file this role creates. A role without one is never created, only written into when its file exists. |

## Capturing a fact: `runbook_capture`

The tool is registered only when the vault declares `runbooks`, the runbook vault
lies inside the served vault, the server is not read-only, and the tool profile is
`standard`. Otherwise the tool list is unchanged.

| Argument | Meaning |
|---|---|
| `subject` | The project the conversation is about: its name, a registered subject (tag, last segment or alias), or a runbook folder path such as `Runbooks/mail-runbook`. |
| `kind` | One of the eight kinds below. |
| `evidence` | `measured` (a command actually run, its real output), `said` (a dated meeting or a named person) or `unverified`. |
| `source` | One line saying where the fact comes from. It is kept in the operation journal, not in the runbook. |
| `fields` | The fields of the kind, below. An unknown field is refused. |
| `observed_on` | Date of the event, `YYYY-MM-DD`, not in the future. Default: today (UTC). |
| `target_role` | A role that overrides the routing. |
| `create_runbook` | `true` creates the runbook when the project has none yet (below). Default `false`. |
| `dry_run` | `true` returns the plan and writes nothing. |
| `request_id` | Makes a retry safe: an identical retry replays the first receipt. |

| Kind | Required fields | Optional fields | Written as |
|---|---|---|---|
| `error_fix` | `symptom`, `fix` | `cause` | An H2 `Symptom: <symptom>` with `Cause`, `Fix` and the provenance. A missing cause is written with the `not_captured` label. |
| `command` | `machine`, `heading`, `command`; `output` when measured | `syntax`, `phase` | The machine, the command in a fence, its real output or `not_captured`, under `heading`. `phase` names the role when `target_role` is absent. |
| `server`, `variable` | `placeholder` (`<UPPER_SNAKE>`), `meaning`, `value`, `heading` | | A row of the table under `heading`, keyed by its placeholder. |
| `contact` | `name`, `role`, `channel`, `heading` | | A list item under `heading`. |
| `decision` | `decision`, `reason` | `discarded` | A dated list item under the `decisions` label. |
| `fact` | `heading`, `fact` | | A dated observation box (`> **On site, <date>.**`) under `heading`. |
| `open_question` | `question`, `owner` | | A list item with its owner under the `open_questions` label. |

### Creating a runbook

The name of a runbook comes from the project the conversation is about. A subject
that names no existing runbook is refused, and the refusal says to repeat with
`create_runbook=true`; asking explicitly is what keeps a typo from starting a
runbook. With it, the capture creates `<slug>-runbook/` in the runbook vault, the
slug taken from the project name (lowercase letters, digits and hyphens) or from
the registered subject, and writes:

1. the captured file, with its role's title and the entry;
2. one file per other role that has a title, holding only its title and the
   `skeleton` label;
3. the README, titled with `readme_title`, listing every file under the `documents`
   label.

The captured fact is committed first. A scaffold file that cannot be written is
left out of the receipt's `scaffold` list, and the next capture on its role creates
it. `dry_run` shows the whole plan, `scaffold` included, without writing.

### Routing

Each step refuses rather than guesses, and the refusal lists the candidates:

1. **Runbook.** The subject names one runbook folder of the runbook vault, or a new
   one with `create_runbook=true`.
2. **Role.** `target_role`, else `phase` for a command, else the template routing.
3. **File.** The one file whose stem matches the role's pattern. None: the role's
   `stem` is created, if the role has a `title`. Several: refused.
4. **Section.** The H2 the kind names. It is matched without case or spacing
   differences, and created at the end of the file when absent.

### What a capture does to a file

- **Provenance** is one neutral mark per entry, `<measured|said|unverified> YYYY-MM-DD`,
  with no tool, assistant or session named.
- **Already present** (the same text in the same section, whatever its provenance
  date): nothing is written, the receipt says `duplicate_skipped`. Text present in
  another section of the file is not detected.
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
`scaffold`, `written`, and for a write `content_hash`, `operation_id` and `indexed`.

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
  how a project gets its runbook, the three provenance values and the refusal of
  guesses and secrets, for clients that ignore server instructions.
- **`session_context`.** With a `subject` that names one existing runbook, the result
  carries `runbook: {path, language}`. It is attached after the sources are fitted and
  dropped when it does not fit, so it never costs a source.

### Undoing a capture

Every write is journaled: `get_note_history` and `audit_query` list it. `revert_note`
does not reach runbooks, because it is bound to `DATACRON_WRITE_PATHS`. When a runbook is a
git working tree, `git diff` shows the capture and `git restore` undoes it.

## Write boundary

A runbook file is writable only when all of these hold, judged after the path is
resolved (a `..` segment is followed first):

1. it lies inside the runbook vault, itself inside the served vault;
2. its first folder under the runbook vault is named by `directory_naming`;
3. it is a Markdown file;
4. it is not under one of the `protected_paths`;
5. no folder on its path is `.git`, `confluence-source` or `confluence-updates`.

Rule 5 is fixed: a vault cannot declare it away. Those three generic names are the only
folder names Datacron knows in advance; every other name, protected ones included, comes
from your configuration. Rules 4 and 5 compare without case on every filesystem. The
write itself then goes through the ordinary writer, with its lock, its journal, its note
admission and its refusal of a path that crosses a symbolic link or a junction. The
generic write tools never treat the runbook vault as a write path.

Datacron never runs git in a runbook: it writes files, and reviewing the diff and
committing stay manual.
