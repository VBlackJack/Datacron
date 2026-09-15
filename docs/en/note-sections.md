# Read and reorganize note sections

[Version française](../fr/note-sections.md)

Use a note's heading outline to select the part you need. Section operations use the
same Markdown parser as the outline: inline formatting is removed from heading names,
Setext headings are supported, and headings inside fenced code are ignored.

## Read a section

Call `get_note` with `format: "map"` to inspect the heading hierarchy. Then pass the
rendered heading names from the root to the target as `heading_path`:

```json
{
  "id_or_path": "backlog.md",
  "heading_path": ["Backlog", "Active", "Service", "BL-0123"],
  "format": "full"
}
```

The result includes the selected heading and its complete subtree, within the normal
response limit. `offset`, `limit`, `total_chars`, and `next_offset` refer to the returned
section's redacted text. Keep the same heading path when requesting the next page.
The section metadata identifies the source lines and selected occurrence;
`content_hash` and `note_content_hash` still describe the complete original note.

If the same complete path appears more than once, supply `heading_occurrence`, starting
at one. A missing or ambiguous selector returns an error. Section selection requires
a note path or note ID and `format: "full"`; it cannot be combined with a chunk ID.
An unknown chunk ID also returns an error, instead of falling back to the whole note.
Read the outline again and use a chunk ID actually returned by the server.

## Move a section within a note

`move_note_section` moves a heading and its entire subtree beneath an existing
destination heading in the same note. This supports archiving a completed backlog
item while keeping its identifier and history together. The tool does not decide
whether an item is complete: select the item after checking its latest state.

First preview the move using the current note hash:

```json
{
  "rel_path": "backlog.md",
  "heading": "BL-0123",
  "heading_level": 4,
  "destination_heading": "Archived service items",
  "destination_level": 3,
  "expected_hash": "<current note content_hash>",
  "confirm": false
}
```

Review the source and destination coordinates and `projected_hash`. Move coordinates
are one-based lines within the Markdown body, excluding frontmatter. To apply the
move, call the same operation with `confirm: true` and a stable `request_id`.
Previewing changes neither note bytes nor the index. Application requires the
same current hash, records the previous bytes, and reconciles the index.

The destination must already exist. Heading levels are preserved; the source must
have a greater level number than its destination and fit as its final direct child.
Duplicate titles require the appropriate level and one-based occurrence selector
(`heading_occurrence` or `destination_occurrence`). The tool refuses self-moves,
descendant destinations, unchanged placement, mixed line endings, and moves that
would change Markdown heading interpretation or require inserting a newline.

No conversion of legacy archive tables occurs. Create an appropriate archive heading
with the existing section writers, then move selected items. Cross-note moves remain
organization-manifest operations.

## Update a backlog counter

`set_frontmatter` accepts `last_id`, with a required `expected_hash`. Its value is
`BL-` followed by at least four ASCII decimal digits. An existing valid counter
cannot decrease; malformed existing values are refused. The body is preserved.

Moving a counter from prose into frontmatter is a separate migration: read the
authoritative current value, set it with CAS, verify the receipt, then replace the
old prose counter with a pointer to frontmatter. Never leave two competing counters.
Use a stable `request_id` for each mutation and reread the note after each write.

Counter updates preserve unrelated YAML text as well as the body. They refuse
anchors, aliases, duplicate or merged keys, complex keys, flow mappings, and combined
field removal rather than risk changing unrelated metadata. Ordinary updates without
`last_id` retain their existing serialization behavior.

## Start with useful orientation excerpts

`session_context` can select configured heading paths from orientation notes instead
of returning only their opening text. `DATACRON_SESSION_CONTEXT_SECTIONS` accepts a JSON
object mapping note paths to lists of heading paths. It applies to notes that the
session already loads through `DATACRON_SESSION_CONTEXT_PATHS` or another selection.
Use an empty object to keep opening-page behavior for every note.

The defaults select the locations and writing sections of the supplied INIT template.
Each selected source exposes an `excerpts` array with individually wrapped content,
the original note hash, and `next_read` pointers for incomplete sections. The existing
per-note allowance is shared between excerpts, and the overall serialized response
budget still applies. There is no single offset spanning discontiguous excerpts.

Missing or ambiguous headings produce an explicit `full_fallback` result with a normal
opening page. Redacted selectors are not offered as usable continuation requests.
No index repair or vault mutation occurs during orientation.

## Correct a missing heading selector

Write errors retain the `heading_not_found` code and provide bounded suggestions
using exact rendered titles, levels and occurrences. Suggestions are sanitized and
never select a section automatically. A truncated or redacted suggestion is marked
as unsuitable for direct selection; reread the outline before choosing a target.
`move_note_section` errors also carry `selector`: `source` when the heading to move was
not selected, `destination` when the destination heading is missing, ambiguous or
selected with an invalid occurrence. An ambiguous selection returns the `heading_ambiguous`
code with a `next_action`; a destination message names `destination_level` and
`destination_occurrence`.
