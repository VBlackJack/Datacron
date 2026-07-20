# Datacron documentation

**English** | [Français](../fr/index.md)

Entry point for all documentation. Datacron is a local MCP server that queries and maintains
a Markdown vault from Claude, without sending the whole vault into the context.

## Get started

| Document | For what |
|---|---|
| [README](../../README.en.md) | Overview, capabilities, current measurements. |
| [Installation and configuration guide](setup.md) | Install, initialize a vault, wire up Claude Desktop / Claude Code, environment variables, enable writing. |
| [Windows installation (installer)](installation-windows.md) | The `Datacron-Setup.exe` installer: double-click, no Python, automatic client registration, reinstall, silent mode, uninstall. |
| [Frequently asked questions](faq.md) | Symptom-first fixes for vault selection, write access, clients, index freshness, reset, uninstall, and logs. |
| [User guide](user-guide.md) | Day-to-day use from Claude: search, read, write, supervise, example requests. |

## Understand how it works

| Document | For what |
|---|---|
| [Vault conventions (SPEC)](spec.md) | Vault contract: `.datacron/` sidecar, frontmatter, trust model, wikilinks, chunks, audit, versioning. |
| [Architecture and public surface](architecture.md) | Technical architecture and exposed surface. |
| [Settled decisions v2.1](decisions-v2.1.md) | Locked design choices and their rationale. |
| [Freshness contract v1](freshness-contract-v1.md) | Index freshness guarantees. |

## Security, integrity, operations

| Document | For what |
|---|---|
| [Security boundary](security-boundary.md) | Read/write confinement, guarantees, local threat model. |
| [Integrity scrubber](integrity-scrubber.md) | Silent-corruption detection, canaries, scrub passes. |
| [Operational health and durability](operational-health.md) | Certified read-only mode, durability policy, `get_health`. |
