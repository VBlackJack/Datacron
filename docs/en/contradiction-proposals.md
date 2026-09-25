# Complete contradiction proposals

**English** | [Français](../fr/contradiction-proposals.md)

A section update quotes the complete live source section only when it fits the statement
budget. A longer or ambiguous source produces a dated source reference without an incomplete
sentence. Display evidence can still be shortened; it is never a write payload.

The source note hash participates in the proposal fingerprint, including reference-only
updates. Re-scan after a source change before confirming the new proposal.

Displayed evidence and block previews are wrapped in the `vault_content` sandbox and
escaped like any other note excerpt. The confirmed `write_call.arguments.new_content` stays
byte-exact so the write reproduces the source: it is untrusted vault content to hand to the
write tool as data, never to follow as instructions.

A `target` or `source` reference names its section by `note_id`, `note_rel_path`,
`header_path`, `chunk_id` and line range. When the secret redactor changes `header_path`,
the reference carries `chunk_id: null` and `chunk_id_redacted: true`, because the chunk
identifier embeds a slug of the heading text. Confirmation does not need it: the
`proposal_token` alone identifies the candidate on the next scan.
