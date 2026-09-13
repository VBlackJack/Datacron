# Vault organization

**English** | [Français](../fr/organization.md)

A vault can declare where its notes belong, how they should be named, and what size they
must not exceed. Datacron measures the gap between that declaration and the vault's actual
state. The measurement is read-only: it never moves, renames, or rewrites a note.

Datacron knows the *shape* of a rule and nothing else. Folder names and tag names come from
the vault's own sidecar, never from this package. No taxonomy is shipped, suggested, or
expected: two vaults with opposite conventions are served identically.

A vault with no `organization` block is unaffected. The feature is entirely optional.

## The model in one minute

A rule binds **one tag** to **one folder**, with an optional naming template and an optional
size ceiling. A note is governed by the first declared rule whose tag it carries. A note no
rule claims is not at fault: it is out of scope.

```text
note tags  ->  first declared rule that matches  ->  expected folder + name
                                                 ->  gap measured, never corrected
```

## The `organization` block in `.datacron/VAULT.yaml`

The block holds five keys, and only five.

| Key | Type | Purpose |
|---|---|---|
| `scope` | string | The vault subtree the measurement covers. |
| `rules` | list | Placement rules, in priority order. |
| `tags` | mapping | The optional [tag policy](#the-tags-block-a-declared-tag-policy). |
| `state_note_min_notes` | integer, at least 1 | From how many notes a subject folder must carry a state note. Absent: `NO_STATE_NOTE` is not measured. |
| `linking_since` | date | From which calendar date a new note of a subject folder must link to its state note. Absent: `UNLINKED` is not measured. |

`state_note_min_notes` and `linking_since` are read strictly: a boolean is not a count, a
number is not a date, and either key declared without a `tags` policy carrying a
`subject_namespace` is a load-time error that names the key, because the folder
measurements exist only over subject rules. **Upgrade every Datacron installation before
declaring them**: an executable older than this version refuses the whole `VAULT.yaml` with
an `extra_forbidden` validation error, which stops its server, exactly as it does for the
`tags` block.

`scope` is **required as soon as at least one rule is declared**. A rule list without a
scope is a configuration error, not an implicit scope covering the whole vault.

`scope` must be vault-relative. An absolute path, a `:`, or a `..` or `.` segment are
rejected. The scope must exist and be a directory.

An unknown key in the block is a loud failure at load time. This is deliberate: a
misspelled key accepted in silence would leave a configuration that looks active and
measures nothing.

## A rule

| Key | Required | Default | Purpose |
|---|---|---|---|
| `tag` | yes | - | The tag that triggers the rule. |
| `folder` | yes | - | The expected folder, vault-relative. |
| `naming` | no | `{slug}` | Filename template. |
| `max_kb` | no | no ceiling | Maximum size, strictly positive integer. |

**`max_kb` counts in kibibytes of 1024 bytes.** `max_kb: 80` therefore allows 81920 bytes.

`folder` is **vault-relative**, not scope-relative, but it must still resolve inside the
scope. With `scope: knowledge`, a `folder: knowledge/meetings` is valid and a
`folder: archive` is rejected. Backslashes are normalized to forward slashes; an absolute
path, a `:`, or directory traversal are rejected.

Two rules cannot declare the same `tag`.

As with the block, an unknown key inside a rule is rejected at load time. The risk avoided
is precise: a rule with a misspelled key would match without constraining anything.

### Order is priority

Declaration order is normative. The first rule whose tag is present on the note wins, and
the search stops there.

This is the only tie-break for a note carrying several governed tags at once, and it is one
the vault owner controls by reordering the list, without reading code.

## Naming templates

Three tokens exist, and only three: `{slug}`, `{date}`, and `{iso_date}`. An unknown token
is a load-time error whose message lists the allowed tokens. An empty `naming` is rejected.

The template is evaluated against the **stem**, that is the filename without its `.md`
extension. Literal text between tokens is escaped: a template stays a template and never
becomes an accidental regular expression.

| Token | Matches | Relation to frontmatter |
|---|---|---|
| `{slug}` | `[^/\\]+`, anything but a path separator | none |
| `{date}` | the note's calendar date | `created`, falling back to `updated` |
| `{iso_date}` | a valid ASCII `YYYY-MM-DD` calendar date | **none** |

`{slug}` is deliberately permissive: no slugification, no case constraint. A template
reduced to `{slug}` alone therefore constrains nothing. What surrounds it does the
constraining.

The distinction between the two date tokens is the only subtlety in this model, and it
matters:

- `{date}` is **compared with the frontmatter**. The name must carry the `created` date, or
  the `updated` one when `created` is missing or unreadable. A file dated another day is a
  naming deviation.
- `{iso_date}` is **structural**. It requires a real date but compares it with nothing:
  not today, not `created`, not `updated`. Any valid date passes.

Template constraint on `{iso_date}`: a template holds at most one, and when it holds one,
the template must start with it.

## The `tags` block: a declared tag policy

Placement rules say where a governed note belongs. They say nothing about a note that
carries no governed tag, two of them, a subject name spelled three different ways, or a
namespace invented by one client. The optional `tags` block inside `organization` closes
that gap, and it is enforced where it matters: at write time, not only in a report.

| Key | Required | Purpose |
|---|---|---|
| `placement_namespace` | yes | The namespace of the placement tags (`memory` when rules are `memory/fact`, `memory/project`, ...). Every rule tag lives in it, or names a declared subject (see [subject rules](#subject-rules)). |
| `markers` | no | Rule tags that may accompany the placement tag as a transversal marker (`memory/decision` on a fact that settles something). |
| `subject_namespace` | no | The namespace that names the subject a note belongs to (`project`). |
| `subjects` | no | The closed registry of subject tags, each with optional `aliases`: spellings that must be reported instead of silently accepted. A plain string is a subject without aliases. |
| `subject_exempt_tags` | no | Placement tags whose notes may carry several subjects (a person record relates to many projects). |
| `allowed_namespaces` | no | Other namespaces admitted next to the placement and subject namespaces (`org`, `meta`). Anything else with a `/` is refused. |

The policy reads as follows. A note carries **exactly one placement tag** among the rule
tags, optionally plus one marker; **at most one subject tag** from the registry, unless its
placement tag is exempt; **no alias** of a registered subject, whether bare (`heimdall`) or
namespaced (`projet/heimdall`); **no undeclared namespace**, and no `memory/*` tag that is
not a rule tag. Tags without a `/` that are not aliases are free descriptors and always
pass. Comparison is case-insensitive, and inline `#tag` occurrences in the body count, so a
compliant frontmatter cannot be undone by prose.

Three surfaces apply the same evaluation:

- `create_note_ai` refuses a note inside the scope whose effective tags break the policy,
  with a typed error whose `code` is `tag_policy_violation` and whose message names every
  violation and what was expected. Nothing is written.
- `apply_organization_manifest` refuses, at validation, a bundle whose **result** notes break
  the policy of the target configuration, with the same code. Notes the bundle does not
  touch are not judged, so an incremental cleanup stays possible.
- `datacron reorganize` reports the three policy kinds described below.

A vault without the block is not judged; none of this exists until the vault declares it.
The policy requires at least one placement rule, every marker and exempt tag must be a
placement rule tag, rule tags must be lowercase (the rule resolver compares them exactly,
the policy compares them lowercased), an alias may not collide with a rule tag, and an
unknown key is a loud failure at load time, like everywhere else in the block.

### Subject rules

Once a policy is declared, a rule may be keyed by a **registered subject tag** instead of a
placement tag: `project/heimdall` to `_memory/subjects/perso/heimdall`. The rule behaves like
any other, first declared match wins, and carries the folder, the naming template and the
ceiling for the notes it governs. What changes is the reading of the note: the folder says
which subject the note belongs to, the placement tag still says what the note is.

- The policy is unchanged. A note governed by a subject rule still carries **exactly one
  placement tag** and at most one marker; the subject rule tag is judged as a subject, never
  counted as a placement tag. A note with a subject tag and no placement tag is
  `UNGOVERNED`, even when a subject rule decides its folder.
- A rule tag that is neither in the placement namespace nor a declared subject is refused at
  load time, and so is an alias used as a rule tag. Markers and exempt tags must name
  placement rules, and at least one placement rule must remain.
- Declaration order does the rest. Put the rules of the types that must stay together
  (`memory/contact`, `memory/preference`, ...) **before** the subject rules, and the subject
  rules **before** the placement rules that serve as fallback for notes without a subject.
  A subject rule declared first would draw every person record into the subject folder.
- The winning rule carries everything, so declare `max_kb` on each subject rule: a `memory/fact`
  ceiling does not apply to a fact governed by its subject.

### What the policy does not cover

- **Only creation and manifests are gated.** `append_journal`, `patch_note_section`,
  `patch_note_preamble`, `rename_note_section`, `delete_note_section`, `move_note_section` and `revert_note`
  change a body without re-judging its tags: an entry that writes `#topic/x` in prose, or a
  restored historical version, can drift. `datacron reorganize` measures that drift; it is
  the control to run after such writes, not a reason to write inline tags.
- **Inline tags follow the extractor's heuristics.** A `#tag` inside a single-backtick span
  or a symmetric fenced block is ignored; one inside an indented code block, or a fence
  closed with a different marker length, still counts. An inline tag starts with an ASCII
  letter or `_` and continues with ASCII letters, digits, `_`, `-` and `/`; an accented or
  dotted tag is truncated at the first other character. Write tags in the frontmatter,
  never in prose.
- **Every Datacron installation must be upgraded before the block is declared.** An
  executable that predates the policy refuses the whole `VAULT.yaml` with an
  `extra_forbidden` validation error, which stops its server; the key is not ignored.
- **The JSON report lists nine counters even without a policy.** The three policy counters
  are then always zero, and `--kind` accepts their names; the identity
  `scanned = governed + unmatched + skipped` and every other field are unchanged.

```yaml
organization:
  scope: _memory
  rules:
    - tag: memory/contact
      folder: _memory/people
    - tag: project/heimdall
      folder: _memory/subjects/perso/heimdall
      max_kb: 121
    - tag: memory/fact
      folder: _memory/facts
      naming: "{iso_date}-{slug}"
    - tag: memory/decision
      folder: _memory/decisions
      naming: "{iso_date}-{slug}"
  tags:
    placement_namespace: memory
    markers: [memory/decision]
    subject_namespace: project
    subjects:
      - tag: project/heimdall
        aliases: [heimdall, projet/heimdall]
      - project/datacron
    subject_exempt_tags: [memory/contact]
    allowed_namespaces: [org, meta]
```

## What is measured, and what is not

Nine gaps are reported, and nothing else. The first three measure a governed note against
its rule; the next three exist only when the vault declares a `tags` policy; the last
three measure the subject folders and the code fences. Every one of them compares the
vault with a declared intent: the planner never invents a placement, a link or a note.

| Kind | Meaning |
|---|---|
| `WRONG_FOLDER` | The note is not in the folder its rule declares. |
| `NAMING` | The stem does not satisfy its rule's template. |
| `OVER_SIZE` | The note exceeds its rule's `max_kb`. |
| `UNGOVERNED` | The note carries no placement tag (policy declared only). |
| `UNKNOWN_TAG` | A tag is an alias of a registered subject, an unregistered subject, an undeclared namespace, or a `memory/*` tag that is not a rule tag (policy declared only). |
| `TAG_CARDINALITY` | Several placement tags, or several subject tags on a note whose placement tag is not exempt (policy declared only). |
| `NO_STATE_NOTE` | A subject folder holds at least `state_note_min_notes` notes and none of them carries a `kind/*` tag (key declared only). |
| `UNLINKED` | A note of a subject folder dated on or after `linking_since` carries no wikilink to the folder's state note (key declared only). |
| `UNBALANCED_FENCE` | The body of a governed note has an odd number of code fence lines. |

### State notes and links

A **subject folder** is the folder of a [subject rule](#subject-rules): a rule whose tag
lives in the policy's `subject_namespace`. The two keys therefore require a `tags` policy
with a `subject_namespace`; declaring either without it is refused at load time rather
than measured as nothing. Only the governed notes that actually sit in the rule folder
count; a misplaced note is already `WRONG_FOLDER`.

The **state note** of a folder is recognised by a tag of the `kind` namespace
(`kind/platform`, `kind/development`, `kind/mission`), never by its stem. A folder may
hold several; any of them satisfies a link. `NO_STATE_NOTE` is reported once per folder:
its `rel_path` is the folder, its `detail` the note count, and it sorts with the note
deviations by path.

`UNLINKED` applies to the notes the convention asks to link, and only to them: a note
whose calendar date (`created`, then `updated`, the same date as `{date}`) is on or after
`linking_since`, that is not a state note itself, and whose stem does not contain
`-history-` (a split history note is named `<subject>-history-<period>`). The stock before
that date is not retrofitted. A note is linked when one of its `[[targets]]` names a state
note of its folder by **stem**, by frontmatter **title**, or by one of its **aliases**,
case-insensitively; `[[target#anchor]]` and `[[target|label]]` count for `target`, a
wikilink inside a fenced block does not. When the folder has no state note, no `UNLINKED`
is reported: the folder is either below the threshold or already `NO_STATE_NOTE`.

`UNBALANCED_FENCE` needs no key. A fence line is a body line that starts with three
backticks after at most three spaces of indentation; opening and closing fences follow
the same rule, so a balanced body has an even count. A tilde fence (`~~~`) is out of
scope. The gap matters because a note split on a line inside a fenced block leaves the
rest of the note unparsed: the section selector no longer sees the headings that follow,
while the index stays healthy.

The rule is a line heuristic, and its limits are measured, not guaranteed. A fence opened
with four or more backticks that contains a three-backtick line, and a closing fence
followed by text on the same line, are both counted by the same rule, so such a body may
be reported as unbalanced or pass as balanced. A body whose very first line is a fence
indented four spaces is measured as flush, because the frontmatter parser strips the
body's leading whitespace. The wikilink exclusion follows the same parity, so a link inside
such a block may still count as a link. Line endings are folded to LF before counting, so
a CRLF note measures the same from the filesystem and from a manifest payload.

**Without a `tags` policy, a note no rule claims is not a deviation.** It is counted in
`unmatched`, and Datacron never invents a placement for it. This is a property of the
model, not a tolerance: a vault may hold as many ungoverned notes as it likes. With a policy
declared, such a note is still counted in `unmatched` and additionally reported as
`UNGOVERNED`, so the identity below keeps holding.

A note the planner cannot read is reported in `skipped` with its reason. It never
interrupts the scan.

Deviations are sorted by path then by kind, never by filesystem traversal order: two runs
over an unchanged vault produce the same report.

### Which tags count

A note's effective tags aggregate two sources: the frontmatter `tags` key **and** the
inline `#tag` occurrences in the body. They are lowercased, deduplicated, and keep
first-seen order.

Prose tags shaped exactly like a hexadecimal color are dropped. Frontmatter tags are never
filtered.

A consequence worth knowing: a `#tag` written in the body takes part in resolution. It can
therefore change the winning rule, and with it the expected placement, depending on the
other tags present and the rule order.

## Measuring: `datacron reorganize`

```text
datacron reorganize --vault /path/to/vault --dry-run
datacron reorganize --vault /path/to/vault --dry-run --json
datacron reorganize --vault /path/to/vault --dry-run --kind NAMING
datacron reorganize --vault /path/to/vault --dry-run --freshness-days 60
```

| Option | Purpose |
|---|---|
| `--vault`, `-v` | Vault root. Fallback: `DATACRON_VAULT_ROOT`, then the current directory when it holds a `VAULT.yaml` under `.datacron`. |
| `--dry-run` | **Required.** No other mode exists, and the flag must never become implicit. |
| `--json` | Stable machine-readable report instead of text. |
| `--kind` | Restrict the report to one kind: `WRONG_FOLDER`, `NAMING`, `OVER_SIZE`, `UNGOVERNED`, `UNKNOWN_TAG`, `TAG_CARDINALITY`, `NO_STATE_NOTE`, `UNLINKED` or `UNBALANCED_FENCE`. |
| `--freshness-days N` | Also list the state notes whose `last_verified` is missing or older than `N` days (positive integer). Informative: the exit code is unchanged. |

`--dry-run` must be passed explicitly. Omitted, the command refuses to run. An unknown
`--kind` value lists the expected values.

### Freshness of the state notes

A session that verifies a state note sets its `last_verified` frontmatter key. With
`--freshness-days N`, the report lists every governed note tagged `kind/*` whose
`last_verified` is missing, or older than `N` days relative to the run date (UTC calendar
date). A note verified exactly `N` days ago is not listed. The list is sorted by path and
never changes the exit code: it is a reading aid, not a deviation.

With no rule declared, the command does not report an error: it states there is nothing to
measure.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No deviation. |
| `1` | The report is not empty. |
| `2` | The vault or its configuration could not be read. |

**`1` is not an error.** The split between `1` and `2` exists so a non-empty report stays
detectable in continuous integration without failing the job for the wrong reason. An
invalid configuration, a missing scope, or an unreadable vault yield `2`.

### Text output

```text
Organization report for /path/to/vault
  scanned 392 notes, 391 governed, 1 out of scope
  WRONG_FOLDER     0
  NAMING           0
  OVER_SIZE        0
  UNGOVERNED       0
  UNKNOWN_TAG      0
  TAG_CARDINALITY  0
  NO_STATE_NOTE    0
  UNLINKED         0
  UNBALANCED_FENCE 0
No deviation found.
```

With `--freshness-days 60`, a block follows the counters, one line per stale state note:

```text
Freshness (older than 60 days): 2
  knowledge/subjects/alpha/alpha.md (project/alpha, 74 days)
  knowledge/subjects/beta/beta.md (project/beta, never verified)
```

## The JSON contract

`--json` emits a document whose schema is identified by `organization-plan-v2`. The
serialization is deterministic: two-space indentation, sorted keys, non-ASCII characters
preserved as-is. Version 2 adds the three folder and fence counters and the optional
`freshness` field; every field of version 1 keeps its meaning.

| Field | Content |
|---|---|
| `schema` | `organization-plan-v2` |
| `vault_root` | The measured root |
| `scope` | The declared scope |
| `scanned` | Notes admitted within the scope |
| `governed` | Notes a rule claims |
| `unmatched` | Admitted notes no rule claims |
| `counts` | Deviation count per kind, one key per kind; keys are sorted alphabetically in the serialized document, and the declared kind order applies to the text report |
| `deviations` | Gap list: `rel_path`, `kind`, `tag`, `detail`, `expected` |
| `skipped` | Unreadable notes: `rel_path`, `reason` |
| `freshness` | Present only with `--freshness-days`: `rel_path`, `tag`, `last_verified`, `age_days` (`null` when the date is missing) |

The identity `scanned = governed + unmatched + skipped` always holds: a note the planner
cannot read is scanned, then skipped, and is neither governed nor unmatched. A folder
deviation (`NO_STATE_NOTE`) has the folder as `rel_path`; it changes none of the counters.

```json
{
  "counts": {
    "NAMING": 0,
    "NO_STATE_NOTE": 0,
    "OVER_SIZE": 0,
    "TAG_CARDINALITY": 0,
    "UNBALANCED_FENCE": 0,
    "UNGOVERNED": 0,
    "UNKNOWN_TAG": 0,
    "UNLINKED": 0,
    "WRONG_FOLDER": 0
  },
  "deviations": [],
  "governed": 391,
  "scanned": 392,
  "schema": "organization-plan-v2",
  "scope": "knowledge",
  "skipped": [],
  "unmatched": 1,
  "vault_root": "/path/to/vault"
}
```

## Measure, then apply

The two halves of the feature are separate, and the order is the sensible one:

- `datacron reorganize` **measures** the gap. It is read-only, and proposes neither an
  action plan nor a command to run.
- The `apply_organization_manifest` MCP tool **applies** a content-addressed batch, in two
  steps: `mode="validate"` returns a token bound to the exact admitted state, then
  `mode="apply"` acts only when that exact token is presented.

The projected report a validation signs is the same nine-kind report `reorganize` renders,
computed from the bundle's payload bytes, so a payload that introduces an unbalanced fence
or removes a link is visible in `projected_report_sha256` before anything is written. The
projection has no run date, so it never carries the `freshness` field; the final report
after apply is computed without it too, and the two hashes match.

See the [user guide](user-guide.md) for using the tool, and
[operational health](operational-health.md) for the maintenance window an application
requires.

## A complete example

This example is an **illustration**, not a default and not a recommendation. The tags and
folders below are in no way provided by Datacron: they come entirely from the vault that
declares them.

```yaml
organization:
  scope: knowledge
  rules:
    - tag: kind/meeting
      folder: knowledge/meetings
      naming: "{iso_date}-{slug}"
      max_kb: 64
    - tag: kind/journal
      folder: knowledge/journal
      naming: "{date}-{slug}"
    - tag: kind/reference
      folder: knowledge/reference
      naming: "{slug}"
```

Reading this example:

- a note carrying `kind/meeting` must live in `knowledge/meetings`, be named
  `2026-08-31-quarterly-review.md` for instance, and weigh at most 65536 bytes;
- a note carrying `kind/journal` must carry its own frontmatter date, which is a stronger
  constraint than the previous one;
- a note carrying `kind/reference` is constrained on its folder only;
- a note carrying both `kind/meeting` and `kind/reference` is governed by `kind/meeting`,
  because that rule is declared first;
- a note carrying none of these three tags is out of scope, and appears only in
  `unmatched`.

## Further reading

- [Vault conventions (SPEC)](spec.md): the `.datacron/` sidecar and frontmatter contract.
- [User guide](user-guide.md): day-to-day use from Claude.
- [Operational health](operational-health.md): durability, `get_health`, maintenance windows.
