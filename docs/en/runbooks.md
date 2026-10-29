# Runbooks

**English** | [Français](../fr/runbooks.md)

A runbook is one folder per subject, one Markdown file per numbered theme
(`00-architecture`, `50-troubleshooting`, `90-variables`, and so on) and a
`quickref/` folder of per-service survival sheets. A vault can declare where its
runbooks live and what they look like, so that Datacron can file a durable fact in
the right runbook and the right section.

This page describes what is available today: the `runbooks` block of
`.datacron/VAULT.yaml`, the dedicated write setting and the write boundary they
define. No tool writes to a runbook yet.

## The `runbooks` block

The block is optional. A vault without it behaves exactly as before. Unknown keys
are refused at startup, so a misspelled key fails loudly instead of being dropped.

```yaml
runbooks:
  roots:
    - folder: CPDC                       # vault-relative
      directory_naming: "{slug}-runbook" # default
      template: infra
  templates:
    infra:
      language: en
      readme: README
      observation_label: On Mets
      skeleton_line: "State: skeleton. Nothing in this file is measured yet."
      roles:
        architecture:    {stem: 00-architecture,    match: '^\d{2}-architecture$'}
        troubleshooting: {stem: 50-troubleshooting, match: '^\d{2}-troubleshooting$', entry: symptom}
        escalation:      {stem: 60-escalation,      match: '^\d{2}-.*escalation$',   entry: contact}
        variables:       {stem: 90-variables,       match: '^\d{2}-variables$',      entry: table_row}
        measured:        {stem: MEASURED,           match: '^MEASURED$', optional: true}
      quickref: {folder: quickref, language: fr}
      labels: {symptom: Symptom, cause: Cause, fix: Fix}
      routing:
        error_fix: troubleshooting
        contact: escalation
        server: variables
```

### Roots

| Key | Meaning |
|---|---|
| `folder` | Vault-relative folder that holds runbooks. It must not lie in `knowledge/`, and no segment may be an excluded directory (see below). Two roots must not overlap. |
| `directory_naming` | Name of a runbook folder under the root. It holds `{slug}` exactly once plus a literal part, so not every child folder of the root is a runbook. Default `{slug}-runbook`. |
| `template` | Name of a declared template. |

### Templates

| Key | Meaning |
|---|---|
| `language` | Language tag of the numbered files (`en`, `fr`). |
| `readme` | Stem of the index file, without `.md`. |
| `roles` | One entry per file role (lowercase identifier). |
| `quickref` | Optional: `folder`, its own `language`, and `naming` with `{service}` exactly once (default `{service}`). |
| `observation_label` | Label of the dated observation boxes, for example `On Mets`. |
| `labels` | Every word Datacron will render, in the template language. Datacron ships no runbook text. |
| `skeleton_line` | The single line an empty file carries. |
| `routing` | Capture kind to role: `error_fix`, `command`, `server`, `variable`, `contact`, `decision`, `fact`, `open_question`. A kind left out has no default role. |

### Roles

A role is found by the pattern its file stem matches, not by its number: the WSUS
runbook files troubleshooting under `50-troubleshooting`, the AD runbook under
`70-troubleshooting`, and both match `^\d{2}-troubleshooting$`.

| Key | Meaning |
|---|---|
| `stem` | File stem a creation uses, without `.md`. It must match its own `match`, and no other role's pattern, or the next capture could not find the file again. |
| `match` | Regular expression matched against the whole stem. A pattern with nested repetition is refused. |
| `entry` | Optional entry form: `symptom`, `contact` or `table_row`. |
| `optional` | `true` when the file may be absent, `false` by default. |

## Write boundary

Declaring a root does not make it writable. Runbook writes have their own setting:

| Variable | Default | Role |
|---|---|---|
| `DATACRON_RUNBOOK_WRITE_PATHS` | empty | Runbook write allowlist, separated by the OS path separator. **Empty = no runbook is writable.** Only the runbook capture reads it; the generic write tools keep using `DATACRON_WRITE_PATHS` and never read this one. |

A runbook file is writable only when all of these hold, judged after the path is
resolved (a `..` segment or a link is followed first):

1. it lies inside `DATACRON_RUNBOOK_WRITE_PATHS` and inside the vault;
2. it lies inside a folder of a declared root that is named by `directory_naming`;
3. it is a Markdown file;
4. it is not under `knowledge/` at the top of the vault;
5. no folder on its path is `.git`, `confluence-source` or `confluence-updates`.

Rules 4 and 5 are fixed: a vault cannot declare them away, and they are compared
without case on every filesystem. They are the only folder names Datacron knows in
advance; every other name comes from the vault.

Datacron never runs git in a runbook: it writes files, and reviewing the diff and
committing stay manual.
