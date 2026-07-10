# Datacron local security boundary

Datacron currently serves one local Markdown vault over MCP stdio. The server
security claim is deliberately narrower than prompt-injection resistance for an
agent or model.

## Responsibility boundary

The server guarantees that:

- note filesystem access is mediated by one `VaultScope`;
- reads stay inside the configured vault and writes also require
  `DATACRON_WRITE_PATHS`;
- vault text is returned inside a data-only envelope;
- registered tools do not evaluate note content or turn note content into a
  filesystem, arbitrary process, or network command;
- write attribution comes from MCP transport context, never from note content;
- likely secret values are redacted at configured output boundaries.

The MCP consumer remains responsible for deciding whether to call another tool.
A model can still be influenced by hostile prose and can copy that prose into a
new explicit tool call. The server-side envelope and escaping do not prove model
compliance.

## Caller identity

The supported transport is local stdio. `StdioCallerIdentityProvider` is the only
caller-attribution point. The local OS process connection is the trust boundary;
MCP client name, version, and client ID are self-asserted attribution metadata,
not cryptographically verified credentials. Vault content cannot set the actor in
the durable operation journal.

A remote transport must replace the identity provider with one that validates
credentials before constructing an actor. Remote authentication, SSO, tenant
namespaces, and cross-tenant ACLs are not implemented.

## Vault scope

`SingleTenantVaultScope` currently permits reads throughout one configured vault.
Writes must also fall within an explicit `DATACRON_WRITE_PATHS` root. Scoped reader
and writer adapters mediate filesystem operations, while index results, chunk
resolution, backlinks, resources, audit metadata, and the fixed ripgrep search root
are checked against the same scope dependency.

The underlying reader and durable writer retain their own path-containment checks.
`VaultScope` is the replacement seam for a future ACL or namespace policy; the
current implementation is not a multi-tenant isolation mechanism.

## Secret redaction

Secrets should not be stored in a Markdown vault. Use a secret manager and enable
volume encryption for the vault and `.datacron` sidecar at rest.

`DATACRON_REDACT_SECRETS` accepts:

- `off`: no optional FileLogger or retrieval redaction;
- `log`: FileLogger redaction only;
- `retrieval`: MCP retrieval redaction only;
- `all`: both boundaries, and the conservative default.

The durable operation journal always redacts detected values regardless of this
optional policy. This prevents an audit setting from making clear credentials
durable. Exact note history is not scrubbed because it is the reversible source
material, not an output log.

The default detector covers labelled passwords, tokens, keys and fingerprints,
Bearer credentials, common token prefixes, AWS access keys, PEM private keys, and
secret-bearing heading slugs. Additional regular expressions can be supplied as a
JSON string list in `DATACRON_SECRET_REDACTION_PATTERNS`. A custom expression may
define a named group called `secret` to preserve the surrounding match; otherwise
the complete match is replaced.

Example:

```powershell
$env:DATACRON_REDACT_SECRETS = "all"
$env:DATACRON_SECRET_REDACTION_PATTERNS = '["INTERNAL-[0-9]{8}"]'
```

Redaction is deterministic loss prevention, not credential validation or vault
scrubbing. False positives are possible under the conservative default.

## Content-hash output

`get_note` also returns the byte-exact note hash under both `content_hash` and
`note_content_hash`, plus the `content_hash_contract` identifier. Chunk reads
return `chunk_content_hash`, the SHA-256 of the indexed derived chunk content.
These fields are fixed-length lowercase hexadecimal digests, not raw note or
chunk content. They do not bypass the existing retrieval redaction, sandboxing,
or vault-scope checks; the only content-bearing fields remain subject to those
boundaries.

## Audited tool capabilities

The closed manifest is `datacron.mcp.security_manifest.MCP_TOOL_CAPABILITIES`.
The blocking injection-surface property compares it with the live FastMCP registry.
The only process-backed capability is `search_regex`, which starts the configured
ripgrep executable with explicit caller-provided pattern and glob arguments. No MCP
tool provides network access, arbitrary process execution, eval, or dynamic tool
dispatch.
