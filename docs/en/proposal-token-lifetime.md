# Proposal token lifetime

`contradiction_scan` tokens do not expire by elapsed time. The date in
`cs2:YYYY-MM-DD:<sha256>` is the proposal date used in the proposed update, not a TTL.
A token issued yesterday can be confirmed today if the proposal still recomputes identically.

Confirmation rescans the current candidates and checks the target against its current
indexed hash. An unknown or changed proposal is refused. The returned `expected_hash`
is the compare-and-set guard for the subsequent explicit write: a target change after
confirmation makes that write fail. Token age adds no freshness guarantee, and a returned
proposal remains a proposal until its writer succeeds.

[Translation](../fr/proposal-token-lifetime.md)
