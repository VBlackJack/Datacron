# Datacron documentation

**English** | [Français](../fr/index.md)

Entry point for all documentation. Datacron is a local MCP server that queries and maintains
a Markdown vault from Claude, without sending the whole vault into the context.

## Get started

| Document | For what |
|---|---|
| [README](../../README.md) | Overview, capabilities, current measurements. |
| [Installation and configuration guide](setup.md) | Install, initialize a vault, wire up Claude Desktop / Claude Code, environment variables, enable writing. |
| [Use Datacron with Ollama](ollama.md) | Connect Ollama to Datacron's stdio MCP server through an explicit bridge with documented evidence limits. |
| [Windows installation (installer)](installation-windows.md) | The `Datacron-Setup.exe` installer: double-click, no Python, automatic client registration, reinstall, silent mode, uninstall. |
| [Frequently asked questions](faq.md) | Symptom-first fixes for vault selection, write access, clients, index freshness, reset, uninstall, and logs. |
| [User guide](user-guide.md) | Day-to-day use from Claude: search, read, write, supervise, example requests. |
| [Memory discipline](memory-discipline.md) | Common initialization, people enrichment, sourced follow-up and client diagnostics. |

## Understand how it works

| Document | For what |
|---|---|
| [Vault conventions (SPEC)](spec.md) | Vault contract: `.datacron/` sidecar, frontmatter, trust model, wikilinks, chunks, audit, versioning. |
| [Vault organization](organization.md) | The `organization` block in `VAULT.yaml`: tags, folders, naming templates, size ceilings, and `datacron reorganize`, which measures the gap read-only. |
| [Architecture and public surface](architecture.md) | Technical architecture and exposed surface. |
| [Freshness contract v1](freshness-contract-v1.md) | Index freshness guarantees. |

## Security, integrity, operations

| Document | For what |
|---|---|
| [Security boundary](security-boundary.md) | Read/write confinement, guarantees, local threat model. |
| [Integrity scrubber](integrity-scrubber.md) | Silent-corruption detection, canaries, scrub passes. |
| [Operational health and durability](operational-health.md) | Certified read-only mode, durability policy, `get_health`. |

## Work day to day

| Document | For what |
|---|---|
| [Daily workflows and measured validation](daily-workflows.md) | Session orientation, sourced follow-up, write progress and archive workflows, with the measured evidence behind each. |
| [Read and reorganize note sections](note-sections.md) | Select, move, rename and delete sections from a note's heading outline; configure the sections a session reads. |
| [Read your notes without a connection](human-library.md) | Prepare a browsable Markdown library of the vault for Obsidian or a file browser, review it, then apply its manifest. |

## Reference

| Document | For what |
|---|---|
| [Reliable writes and retrieval quality](improvements.md) | `request_id` replay of ordinary writes, indexing contracts and retrieval quality measurements. |
| [Complete contradiction proposals](contradiction-proposals.md) | How a contradiction proposal stays complete within its budget, with dated source references and sandboxed evidence. |
| [Follow-up storage and presentation](follow-up-storage.md) | What `prepare_follow_up` stores and how `get_follow_up` presents it. |
| [Frontmatter audit fields](frontmatter-audit.md) | The lifecycle fields a frontmatter write records in the operation journal. |
| [Proposal token lifetime](proposal-token-lifetime.md) | A `cs2:` proposal token carries a proposal date, not a time to live. |

## Release notes

| Document | For what |
|---|---|
| [Datacron 2026.0913.02: offline library](offline-library-release-notes.md) | The release that added the local Markdown library. |
| [Pending release: surface test fixes](surface-fixes-release-notes.md) | Heading trails with actual Markdown levels and the reindex they require. |
