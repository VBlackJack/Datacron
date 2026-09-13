# Datacron 2026.0913.02

**English** | [Français](../fr/offline-library-release-notes.md)

This version adds a local Markdown library for finding and reading notes in
Obsidian without an Internet connection. Start with the
[offline library guide](human-library.md): prepare a preview, review its sources
and links, then apply the exact organization manifest during maintenance.

## What changes

- Home and folder pages link to subjects, people, procedures, actions and history.
- Consolidation recipes prepare sourced summaries and section splits, preserve
  originals and protect notes with open checkboxes from automatic archival.
- Referenced local attachments are included within configurable limits. External
  websites remain external; useful source summaries must be stored locally.
- Session context supports priorities, adaptive budgets and cached contract hashes.
- Write progress and recovery guidance distinguish committed notes from indexing failures.
- Context redaction, duplicate identities and stale identity sidecars receive stronger checks.
- Concurrent index writes reserve the SQLite writer before checking identity,
  avoiding conflicting read-to-write lock upgrades across independent clients.

## Operation

The library is an explicit maintenance workflow. It does not schedule reviews or
archive notes on its own. Existing vault configuration and original note bodies
are retained. Review source freshness before applying a prepared bundle, stop
other Datacron clients and keep a verified backup.

The runtime validation workflow exercises silent install and reinstall on a
disposable Windows runner, with the runtime PATH restricted to System32. This
does not replace an interactive installer or clean consumer Windows assessment.

See the [daily workflows](daily-workflows.md) for routine use.
