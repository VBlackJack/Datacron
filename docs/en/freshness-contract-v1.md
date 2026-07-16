# Freshness contract v1

**English** | [Français](../fr/freshness-contract-v1.md)

## `freshness-contract-v1`

This contract defines the hash of a Markdown note. The input is the exact ordered byte
sequence returned by a successful binary read of the file. The algorithm is SHA-256 and the
output is a 64-character lowercase ASCII hexadecimal string. There is no normalization: no
line-ending conversion, no BOM stripping, no text re-encoding, no adding or removing of the
trailing newline, no Unicode normalization. The normative boundary is `bytes -> hash`; the
Datacron reference implementation is `sha256_bytes(path.read_bytes())`.

`get_note` returns this digest under `note_content_hash` and keeps `content_hash` as a
compatible alias. It also returns `content_hash_contract` with the value
`freshness-contract-v1`. An autonomous consumer can apply this specification locally: Cortex
never calls Datacron at runtime to decide its own freshness.

The shared manifest `tests/fixtures/freshness-contract-v1.json` contains the Base64 vectors
for LF, CRLF, BOM, and Unicode NFD. The tests write them in binary before hashing.

## Derived chunk hash

For a read by `chunk_id`, `get_note` also returns `chunk_content_hash`: the already-indexed
`content_hash` of the chunk, computed from `chunk.content.encode("utf-8")`. The chunk is
derived from parsing; this digest is not that of a slice of source bytes.

## `sha256-path-content-hash-rollup-v1`

`vault_checksum` is a separate, unchanged contract. It computes a SHA-256 rollup of the
sorted relative paths and the already-computed note hashes: UTF-8 path, NUL, ASCII hash, then
LF. It serves as a point-in-time vault checksum, not as the hash of a single note.
