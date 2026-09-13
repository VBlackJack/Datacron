# Read your notes without a connection

**English** | [Français](../fr/human-library.md)

Datacron can prepare a browsable Markdown library for Obsidian or another local
Markdown reader. The home page links to folder maps, people, procedures, open
checkboxes and historical references. Maps use readable note titles; optional
areas group professional and personal subjects. No community plugin, database,
model or Internet connection is needed to read the prepared library.

The source vault remains canonical. `preview/` is a dated review copy, not a
second editable source of truth. Open that folder as an Obsidian vault and then
open the configured home note. Referenced local attachments are copied when they
are available, admitted and within the configured size limits. External sites
are not downloaded. Check unresolved references before relying on offline access.
This export retains the selected local note contents, including private content;
keep it with the same access restrictions as the source vault.

For material retrieved from Confluence through Cortex, keep a short local summary
with the source link, document update date and retrieval date. Compare it with
recent project decisions before presenting an old procedure as current. A recent
index refresh does not prove that the source is still valid. External references
remain unavailable offline unless their relevant content is captured locally.

## Prepare navigation

Create an options JSON file outside your vault. Adapt paths and tags to its
existing organization policy; this example does not install a new taxonomy:

```json
{
  "scope": "_memory",
  "home": "_memory/accueil.md",
  "tags": ["memory/meta"],
  "language": "fr",
  "areas": {
    "Professionnel": "_memory/subjects/pro",
    "Personnel": "_memory/subjects/perso"
  },
  "max_note_chars": 24000,
  "max_section_chars": 8000
}
```

Run these commands with your own absolute paths:

```text
datacron library audit --vault VAULT --options OPTIONS.json
datacron library prepare --vault VAULT --options OPTIONS.json --output NEW_DIRECTORY
datacron library check --vault VAULT --output NEW_DIRECTORY
```

`audit` prints JSON and does not write a log, ID sidecar or index. `prepare`
requires a new directory outside the source vault. It produces:

- `preview/`: source notes, copied attachments and proposed navigation.
- `review.md` and `changes.diff`: findings, editorial reasons and exact changes.
- `audit.json`: sizes, possible duplicates and unresolved links, including anchors.
- `subject-template.md`: a concise current-state/action/decision/reference/history template.
- `snapshot.json`: source and preview hashes and explicit coverage.
- `manifest.json` and `payloads/`: a content-addressed organization bundle.

The home lists task **sources**, rather than copying checkboxes. The count covers
Markdown unchecked list items, not every commitment expressed in prose or tables.
`state_tags`, `people_tags` and `procedure_tags` in the options customize category
selection. Age alone never archives a note. A historical state needs an explicit
archive, invalidation or replacement signal. Missing verification information
places a reference under review, and a verification date never proves validity.

Limits are configurable: 10,000 notes, 2 MiB per note, 32 MiB per attachment and
256 MiB for the preview by default. The underlying manifest has its own limits,
including 512 operations; select smaller scopes when necessary. Folder pages use
stable filenames so existing links survive refreshes. Refresh refuses to replace
a handwritten page or a generated page whose body was edited. Edit its sources,
or move personal additions to a separate note before refreshing.

`folder_labels` maps exact folder paths to custom display labels.

## Consolidate, split and archive

`datacron library split --vault VAULT --options OPTIONS.json --source RELATIVE_NOTE.md`
prints a proposal JSON with one note per H2 section, including Setext headings.
Duplicate heading titles receive distinct numbered targets. It preserves the
original and every incoming link and heading anchor. Notes with open checkboxes
require manual editorial preparation to avoid duplicating authoritative actions.
Shared context above the first H2 remains in the linked source; review each
extracted section for sufficient context before approval.

For a synthesis, merge or rewritten reference, prepare an explicit recipe:

```json
{
  "notes": [{
    "target": "_memory/subjects/perso/example/example-guide.md",
    "title": "Example: practical guide",
    "body": "# Practical guide\n\nSource-supported synthesis to review.",
    "tags": ["memory/fact", "project/example"],
    "sources": [{"path": "_memory/subjects/perso/example/example.md", "sha256": "EXACT_SOURCE_SHA256"}],
    "rationale": "Consolidate scattered reference information.",
    "archive_sources": []
  }]
}
```

The source must carry its stable frontmatter identity. Supply its exact hash from
the audit, then pass `--recipe RECIPE.json` to `library prepare`. Several source
references support merging; several output notes support splitting. New notes
carry provenance and low confidence until reviewed. Rewrites create a new linked
reference; original paths, titles, aliases, heading anchors and bodies stay intact.
Physical moves and renames remain a separate organization-manifest operation.

`archive_sources` is an explicit proposal, never an age-based choice. Each listed
source must be one of the exact referenced revisions and must have no open
checkboxes. Archive preparation changes its metadata and records successor links;
it preserves the body bytes, including original line endings. No source is deleted.
Editorial output must link to existing actions instead of copying open checkboxes.
Content similarities are candidates for review, not proof of duplicate meaning.

## Review and apply

Read the sources, preview and diff. Confirm that decisions, uncertainties, owners,
dates and commitments retain their meaning. The checks prove byte freshness and
mechanical validity, not semantic truth. They do not automatically resolve
contradictions or infer that a commitment is complete.

`library check` rejects changed source sets, changed source or attachment bytes,
edited previews and invalid manifests. Source-preserving no-op replacements in
editorial bundles bind source revisions into the eventual transaction's CAS.
The navigation itself is a dated inventory and needs refreshing as notes change.

Apply through the existing `apply_organization_manifest`: validate, review the
returned hash/token, then apply the same bundle with that confirmation token.
Stop other writers and verify a separate backup first. Inspect the receipt and
index confirmation; reread the changed canonical notes. For interrupted commits,
retry the identical request/token as described in [organization](organization.md).
The operation history supports restoring replaced notes; a review copy is not a
verified vault-and-sidecar backup.

## Offline acceptance check

Disconnect networking, open `preview/` in Obsidian and use its home page to find a
procedure, a project's state and the source of a decision. Open local attachments
and historical references. Check both ambiguous note names and links to sections.
The automated tests verify files, links, source preservation and the MCP apply/
replay flow; they do not claim that the native Obsidian interface was tested.
