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

## Measured local migration

On 2026-09-10, the local Windows candidate `2026.0910.01` rebuilt a vault containing
2,431 indexed notes and 95,094 chunks in 260.197 seconds of wall time (4 min 20 s).
The reindex command reported 258.558 seconds and advanced generation 2873 to 2874.
Comparison with the saved index found 22,364 replaced chunk IDs across 641 notes.

Before any acceptance-test writes, the Markdown checksum was identical before and
after migration, as was the separate SHA256 of `VAULT.yaml`. The new index reported
no stale entries, hash divergences, identity mismatches, or frontmatter parse errors.
This is one measured local migration, not a duration guarantee for other vaults.

No real proposal token was retained before this live reindex, so rejection of an
actual pre-migration token was not tested on this vault. A synthetic token dated
2020 verified the absence of a TTL, and an unknown token verified the error path;
neither verifies that historical migration scenario. `test_proposal_reindex.py`
covers rejection when chunk IDs change and preservation when they remain unchanged.
Retain a real proposal token before the next controlled reindex to exercise this
case in a live migration.

The local candidate remains `2026.0910.01`; the intended public version is
`2026.0910.02`. A vault already rebuilt with `.01` does not need another reindex
solely for this version increment.

## Known limitation: session context budget

With a token budget too small to contain the memory contract, `session_context`
returns a budget refusal that does not match its declared output schema. Strict
clients can reject this response before exposing `required_tokens`. This behavior
predates these fixes and remains unchanged in this release. Retry with a larger
budget (6,000 tokens worked in the measured case), or use
`get_note(id_or_path="_memory/INIT.md")` when that bootstrap note exists. A separate
follow-up will cover the refusal schema, preservation of `required_tokens`, and
whether other tools share the same error-path exposure.
