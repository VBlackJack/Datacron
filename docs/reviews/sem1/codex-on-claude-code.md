# Cross-review — Codex on claude-code/phase0 [Sem 1]

**Reviewed commits**: 5e5eb17..c5d0a0a (5 commits)
**Date**: 2026-05-17
**Verdict**: Approve

## Verification
- ruff: ✓ `ruff check .`
- ruff format: ✓ `ruff format --check .`
- mypy --strict src tests: ✓
- pytest: ✓ (105 passed with cacheprovider disabled)

Note: the exact cache-enabled `pytest` command hit a workspace-local Windows ACL error on
`.pytest_cache`, not a test failure. I reran `pytest -p no:cacheprovider`; it collected and
passed the Claude Code Sem 1 suite: 105 tests.

## Answers to Claude Code's 4 cross-review requests
1. note.content stripped of frontmatter (welcome.md + code-snippets.md sanity check):
   Confirmed. On a temporary copy of `tests/fixtures/demo-vault`, `welcome.content` starts
   with `"# Welcome\n\n"` and `code-snippets.content` starts with `"# Code Snippets\n\n"`;
   both keep frontmatter data in `note.frontmatter`. This follows `frontmatter.parse()`,
   which returns `post.content` after parsing the YAML block.
2. resolve_alias resolution order (Reflection alias on nested-thoughts.md):
   The demo sanity case passes: `resolve_alias("Reflection", vault_root)` resolves to
   `subfolder/nested-thoughts.md`. However, the implementation does not enforce the
   contract's global priority order when an exact title collides with another note's alias.
   A temporary two-note vault with title `Target` on `a.md` and alias `Target` on `b.md`
   returns `None` instead of the title match. This is a blocker because `01-contracts.md`
   §2.6 says title, then filename, then aliases.
3. note.tags deduplication order:
   Confirmed for the fixtures and implementation. `welcome.md` returns
   `["intro", "onboarding", "welcome", "datacron/demo"]`: frontmatter tags first, then
   inline tags, lowercased and first-seen deduplicated. `code-snippets.md` returns
   `["code", "reference"]`.
4. JsonIdStore → ulid_paths Sem 2 migration plan:
   I agree with Julien's plan. In `SQLiteFTS5Store.open(db_path)`, after creating the
   `ulid_paths` table, derive the sidecar path from the expected layout:
   `db_path.parent.parent / "ulids.json"`. If it exists, read the JSON object, validate
   string `rel_path -> note_id` entries, and `INSERT OR IGNORE` them into `ulid_paths`
   in the same schema/open transaction. After a successful commit, rename the file to
   `ulids.json.migrated`. This is idempotent, non-destructive, and lets Sem 2 make the
   SQLite side-table authoritative without losing the Sem 1 bootstrap IDs.

## Inline comments
- `src/datacron/core/vault.py:327` — Request change: `_build_alias_index()` indexes
  title, filename stem, and aliases in one flat pass, so a lower-priority alias can make
  a higher-priority title unresolved. The contract requires resolution by priority:
  exact frontmatter title first, then filename stem, then aliases. Build three maps, or
  run three passes over the notes, and only treat ambiguity within the same priority tier
  as unresolved.
- `src/datacron/core/vault.py:275` — Request change: `_assert_matches_vault_root()` logs a
  mismatch but does not reject it. A reader bound to vault A returns vault A notes even
  when `list_notes(vault_b)` is called. The class docstring says mismatches are rejected,
  and silently reading the wrong root is a path-confinement footgun. Raise `ValueError`
  here.
- `.github/workflows/ci.yml:75` — Suggestion: remove `continue-on-error: true` from the
  ShellCheck job before scripts land. The standards say ShellCheck must pass clean; this
  setting would make future shell failures non-blocking.
- `tests/conftest.py:172` — Suggestion: `chunk_factory` builds `chunk_id` from the
  human-readable `header_path`. The actual contract uses the slugged header path inside
  `chunk_id`. This is only a test helper today, but it can train downstream tests to
  accept impossible chunk IDs once Codex's FTS5 tests start using it.

## Other observations
- I could not find a materialized PR description at `docs/prs/sem1-claude-code.md`.
- `VaultReader.read_note()` correctly keeps `raw_content` as full file content and
  `content` as frontmatter-stripped body.
- `JsonIdStore` is a reasonable Week 1 bootstrap as long as the Sem 2 migration is
  implemented before `ulid_paths` becomes authoritative.
- The CLI Sem 1 surface is appropriately narrow: `init` and `status` are implemented,
  later commands are registered as explicit stubs.

## Approval condition (if Request changes)
Approval is unblocked by:

1. Fixing `resolve_alias()` so title, filename stem, and aliases are resolved in the
   frozen contract order, with tests for title-vs-alias and filename-vs-alias collisions.
2. Making `VaultReader` reject mismatched `vault_root` arguments with `ValueError`, with
   a regression test for `list_notes()` and/or `resolve_alias()`.

## Resolution
Spot-checked follow-up commits on `claude-code/phase0`:

- `5c0a92a refactor(core): VaultReader drops vault_root method args; resolve_alias strict global priority`
- `41bc97f docs(review): author response to codex blockers`

Both blockers are resolved:

1. `resolve_alias()` now uses a strict three-tier alias index: title, then filename stem,
   then aliases. Higher-priority tiers shadow lower-priority tiers globally, and ambiguity
   within the same tier returns `None`. The new tests
   `test_title_wins_over_alias_global_priority` and `test_ambiguous_titles_return_none`
   exercise the contract correctly. `_alias_candidates` has been removed.
2. `list_notes()` and `resolve_alias()` no longer accept per-call `vault_root` arguments.
   `_assert_matches_vault_root` is gone. `read_note(path)` still raises `ValueError` for
   paths outside the bound `vault_root`, matching amendment `fe5dbc6`.

Verification after the fixes:

- `ruff check .`: passed
- `ruff format --check .`: passed
- `mypy --strict src tests`: passed
- `pytest`: collected and ran all 107 tests, then failed only while writing a
  workspace-local `.pytest_cache` path with a Windows ACL error.
- `pytest -p no:cacheprovider`: 107 passed

`41bc97f` documents the author response and confirms the fixes. Verdict updated to
Approve.
