# Follow-up storage and presentation

[Translation](../fr/follow-up-storage.md)

`prepare_follow_up` stores the original summary, source excerpt and identity basis as
JSON text, without presentation envelopes. The stored `text_format: raw` discriminator
distinguishes these records from historical wrapped entries. Secret-bearing plans are
refused; owner metadata keeps its existing control-token escaping.

`get_follow_up` presents the source excerpt inside one envelope labelled with `source_path`.
Summary and identity basis have no vault-source envelope. Retrieval still escapes control
tokens and applies secret redaction, so presentation is not a byte-exact export for hostile
or sensitive text. Ordinary text is preserved, including Unicode and whitespace.

Historical digests and revision chains are checked before presentation. Recognized old
envelopes are removed from the read projection only; persisted bytes remain unchanged and
matching old revisions can still be replayed. New raw records preserve literal envelope
text as data rather than interpreting it as an old storage format.

A bulk cleanup of existing entries is not performed. Any proposed migration must inventory
the affected records and preserve their revision/digest integrity before explicit approval.
