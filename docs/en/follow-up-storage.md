# Follow-up storage and presentation

[Translation](../fr/follow-up-storage.md)

`prepare_follow_up` stores the original summary, source excerpt and identity basis as
JSON text, without presentation envelopes. The stored `text_format: raw` discriminator
distinguishes these records from historical wrapped entries. When retrieval redaction is on
(`DATACRON_REDACT_SECRETS` is `retrieval` or `all`), a plan whose persisted fields hold
secret-shaped text is refused with code `follow_up_sensitive_content`, and the message names
the field; the rest of the source note is not examined. Owner metadata keeps its existing
control-token escaping.

`get_follow_up` presents `source_excerpt`, `summary` and `identity_basis` each inside one
envelope. The excerpt is labelled with `source_path`, the other two with the note the record
was read from. Older entries stored with an envelope come back with exactly one. Retrieval
still escapes control tokens and, under the same redaction policy, redacts secrets, so
presentation is not a byte-exact export for hostile or sensitive text. Ordinary text is
preserved inside the envelope, including Unicode and whitespace.

Historical digests and revision chains are checked before presentation. Recognized old
envelopes are removed from the read projection only; persisted bytes remain unchanged and
matching old revisions can still be replayed. New raw records preserve literal envelope
text as data rather than interpreting it as an old storage format.

A bulk cleanup of existing entries is not performed. Any proposed migration must inventory
the affected records and preserve their revision/digest integrity before explicit approval.
