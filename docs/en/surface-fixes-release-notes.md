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
