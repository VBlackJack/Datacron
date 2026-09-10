# Proposal token lifetime

`contradiction_scan` tokens do not expire by elapsed time. The date in
`cs2:YYYY-MM-DD:<sha256>` is the proposal date used in the proposed update, not a TTL.
A token issued yesterday can be confirmed today if the proposal still recomputes identically.

Confirmation rescans the current candidates and checks the target against its current
indexed hash. An unknown or changed proposal is refused. The returned `expected_hash`
is the compare-and-set guard for the subsequent explicit write: a target change after
confirmation makes that write fail. Token age adds no freshness guarantee, and a returned
proposal remains a proposal until its writer succeeds.

No time expiry does not guarantee validity across index migrations. The proposal
identity includes target and source chunk IDs. A reindex that changes either ID can
make an old token unresolvable even when note bytes are unchanged. The D1 migration
reports `proposal_token_stale_or_unknown` and asks for a new scan and review, with no
write call. Existing `cs2` tokens have no index generation, so this error cannot
distinguish a migration from another candidate change or an unknown token. A rebuild
that preserves the proposal identity does not invalidate it merely by advancing the
index generation.

[Translation](../fr/proposal-token-lifetime.md)
