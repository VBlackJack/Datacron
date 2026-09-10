# Pending release: surface test fixes

[Translation](../fr/surface-fixes-release-notes.md)

## Heading hierarchy

Heading trails now use actual Markdown levels. H2 siblings no longer become children
of the first H2 when the body has no H1; skipped levels also preserve sibling boundaries.
The frontmatter title remains a separate search signal, not a virtual heading root.

This changes affected chunk IDs. Run a full `datacron reindex` after installing the
updated version, then retrieve fresh chunk references. Note IDs and note bytes do not
change. An index built with the previous chunker is not migrated by ordinary reads of
unchanged notes.

Contradiction proposal tokens are one consumer of chunk IDs: their identity includes
the target and source chunk IDs. Tokens for affected candidates cannot be confirmed
after this migration, even if no note bytes changed. Confirmation returns
`proposal_token_stale_or_unknown`, mentions reindexing, and asks for a new
`contradiction_scan(mode="scan")` followed by review. No write call is returned.
The existing `cs2` format carries no index generation, so the server cannot identify
reindexing as the definite cause of an unresolved token. An unchanged proposal remains
confirmable after a rebuild that preserves its chunk IDs. No elapsed-time TTL is added.
