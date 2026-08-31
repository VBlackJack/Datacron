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

The block holds two keys, and only two.

| Key | Type | Purpose |
|---|---|---|
| `scope` | string | The vault subtree the measurement covers. |
| `rules` | list | Placement rules, in priority order. |

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

## What is measured, and what is not

Three gaps are reported, and nothing else.

| Kind | Meaning |
|---|---|
| `WRONG_FOLDER` | The note is not in the folder its rule declares. |
| `NAMING` | The stem does not satisfy its rule's template. |
| `OVER_SIZE` | The note exceeds its rule's `max_kb`. |

**A note no rule claims is not a deviation.** It is counted in `unmatched`, and Datacron
never invents a placement for it. This is a property of the model, not a tolerance: a vault
may hold as many ungoverned notes as it likes.

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
```

| Option | Purpose |
|---|---|
| `--vault`, `-v` | Vault root. Fallback: `DATACRON_VAULT_ROOT`, then the current directory when it holds a `VAULT.yaml` under `.datacron`. |
| `--dry-run` | **Required.** No other mode exists, and the flag must never become implicit. |
| `--json` | Stable machine-readable report instead of text. |
| `--kind` | Restrict the report to one kind: `WRONG_FOLDER`, `NAMING`, or `OVER_SIZE`. |

`--dry-run` must be passed explicitly. Omitted, the command refuses to run. An unknown
`--kind` value lists the expected values.

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
  WRONG_FOLDER   0
  NAMING         0
  OVER_SIZE      0
No deviation found.
```

## The JSON contract

`--json` emits a document whose schema is identified by `organization-plan-v1`. The
serialization is deterministic: two-space indentation, sorted keys, non-ASCII characters
preserved as-is.

| Field | Content |
|---|---|
| `schema` | `organization-plan-v1` |
| `vault_root` | The measured root |
| `scope` | The declared scope |
| `scanned` | Notes admitted within the scope |
| `governed` | Notes a rule claims |
| `unmatched` | Admitted notes no rule claims |
| `counts` | Deviation count per kind |
| `deviations` | Gap list: `rel_path`, `kind`, `tag`, `detail`, `expected` |
| `skipped` | Unreadable notes: `rel_path`, `reason` |

The identity `scanned = governed + unmatched` always holds.

```json
{
  "counts": {
    "NAMING": 0,
    "OVER_SIZE": 0,
    "WRONG_FOLDER": 0
  },
  "deviations": [],
  "governed": 391,
  "scanned": 392,
  "schema": "organization-plan-v1",
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
