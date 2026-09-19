# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases use **Calendar Versioning**: `YYYY.MMDD.XX` - UTC year, zero-padded month and day,
and a two-digit same-day build counter starting at `00` (e.g. `2026.0714.00`). Git tags are
prefixed with `v` (e.g. `v2026.0714.00`).

## [Unreleased]

### Added

- `apply_organization_manifest` reports the progress of its post-commit reindex as MCP
  progress notifications, at most one every half second. That reindex runs ungated over
  every note in the vault, and without progress a client cannot tell a slow apply from a
  hung one: an apply of about twenty moves outlasted the tool timeout and was reported as a
  failure even when it had committed, leaving the reconcile unfinished. A notification that
  cannot be delivered is logged and dropped rather than failing a committed write.

### Changed

- Finding a write request's receipt costs the journal's tail instead of its whole history.
  The lookup parsed and chain-verified every record ever written, on every keyed write. It
  now scans backwards and stops at the match, and a key the journal has never carried is
  answered from an in-memory set of the keys it holds, without a scan at all. Measured on a
  journal of 20000 operations, 13.5 MiB: a retry of the last write 836 ms to 1.3 ms, a first
  use of a key 656 ms to 1.7 ms, plus 90 ms once per process to build the set. The set is
  rebuilt from the journal and never written to disk, because a second durable structure is a
  second thing that can disagree with it; it advances by reading only the bytes appended since
  it was last brought up to date, from whichever process appended them, and it is only ever
  trusted to say that a key is absent.
- `apply_organization_manifest` validates the bundle once instead of twice. Before committing,
  it rebuilt the whole preview a second time and compared projected report hashes, which walks
  and hashes every note in the organization scope and reprojects the report. That second pass
  costs 608 ms on a 200-note scope, 1423 ms on 400 and 2275 ms on 800: it grows with the vault
  while the batch it protects stays the same size, which is the shape of cost this product
  exists to avoid. It also answered a question the transaction already answers, under the same
  mutation lock: `_validate_before_states` compares the scope inventory the preview captured
  against a live one, in both directions and by exact hash, then checks the before state of
  every path the batch touches, `VAULT.yaml` and the projected report included. What the second
  pass covered on its own was the manifest file changing on disk between the two, so the
  pre-commit step still reloads and re-authenticates the bundle; that read is bounded by the
  batch, not by the vault. A test drives a scope note being edited inside that exact window and
  pins the refusal.
- Refreshing the index after a committed organization batch costs the notes the batch moved
  or rewrote, instead of every note in the vault. That pass ran ungated, reading and hashing
  the whole vault on every apply, and it is the tail that pushed an apply of about twenty
  moves past the client timeout and left the index half refreshed, which is the failure the
  pass exists to prevent. Gating loses nothing a batch can do: a moved note arrives at a path
  the index has no row for, so the gate cannot hold and it is read, while its old path is
  gone from the enumeration and its row is dropped by identity; a note rewritten in place
  went through an atomic replace and carries a new mtime; a removed identity is deleted
  before the pass runs. What the gate does not see is a note edited outside Datacron whose
  mtime did not move, which is the exposure every other pass already accepts, including the
  read repair. The ungated pass was stricter here than anywhere else in the product and
  nothing recorded why; arbitrated by Julien on 2026-09-19.
- `contradiction_scan(mode='confirm')` refuses a confirmation larger than the caller's result
  budget instead of returning it. The confirmation carries the write call the caller is meant
  to execute, and that call carries the section's new content: the whole live section plus
  the block to append, byte for byte, with no excerpt limit. A long running journal section
  is measured in hundreds of kilobytes, and the transport serialises a result twice, once as
  text and once as structured content, so one confirmation could take the caller's whole
  context. Every other tool that returns vault bytes measures itself against
  `max_result_tokens`; this one did not. It refuses rather than truncating, because the
  payload is an exact write call and a truncated one would corrupt the note it is applied to,
  and the refusal says to target a lower-level heading.
- `datacron status` counts notes without reading them, and without writing to the vault. It
  listed every note, which reads the bytes, decodes them, parses the YAML, extracts tags and
  aliases and hashes the file, then discarded all of it but the length. Its reader was also
  writable, so on a vault whose notes have no frontmatter id and no sidecar entry the health
  check resolved an identity for each one and rewrote the whole sidecar every time.
- The vault-relative path of a file the vault walk produced is computed rather than resolved.
  The walk starts at an already-resolved root, so both sides were being resolved for an
  answer already known, twice per note, in three sweeps including the one `reconcile` keys
  its index by. Measured on 1200 notes, all three interleaved in one process: counting
  through `list_notes` takes 1726 ms, through `stat_notes` 601 ms, and through `stat_notes`
  with the path computed 85 ms. A test pins the computed path against the resolved one over
  nested, spaced, uppercase and accented names, because a different spelling there would not
  look like a bug: the index would quietly stop recognising notes it already holds.
- `get_follow_up` sizes a page by halving instead of by dropping one record at a time. Each
  measurement re-serialises the page it is measuring, so dropping one record at a time cost
  one serialisation of the whole remaining page per record dropped, and nothing bounds how
  many records a note holds: the note count is capped at eight, the records harvested from
  them are not. A canonical person or project note that accumulated a few hundred
  commitments over a couple of years turned a read into seconds, once per page, on the
  synchronous path. Measured on records of about a kilobyte, sizing one page of 27: 100
  records 33 ms, 400 records 459 ms, 1000 records 3.0 s, 2000 records 12.5 s, now 1.4, 3.4,
  6.2 and 12.1 ms. The page is the same one the loop arrived at, which a test pins across
  budgets that fit a handful of records, the default page, and the whole set.
- `search_regex` resolves a note once per search instead of once per line that matched in
  it, and applies the glob and the scope admission before resolving at all. `chunks_fts`
  declares `note_id` UNINDEXED, so listing a note's chunks scans the table and builds a model
  for every chunk in it; doing that per matching line made the cost the number of matches
  times the size of the notes they fell in, rather than the number of distinct notes. A
  match the caller had already excluded by glob or scope paid that scan too, for a result
  that was then discarded. Measured on 120 notes of 40 sections, a pattern matching every
  section, limit 20: 214 ms and 20 chunk-list fetches, now 53 ms and one. The caches live
  for one search.
- `get_note` answers an unknown ULID without reading the vault. It does not repair the index
  first, so it falls back to looking at live notes for an identity written since the last
  pass, and that fallback read, decoded, hashed and YAML-parsed every note in the vault, on
  the event loop, with the scope resolving each path on top. The identity that reaches it is
  precisely the one that resolves nowhere: a plausible but nonexistent ULID, which a caller
  can repeat, stalling every other tool call each time before answering that there is no such
  note. Only the notes the index has not seen in their current state are examined now, which
  on a vault nobody has edited outside Datacron is none of them. That is the gate `reconcile`
  already applies, and as there it decides whether to look, never what the content is: a note
  written since the last pass is still read and still found.
- Authorizing a path no longer resolves the vault root a second time. A scope resolves its
  root when it is built, and every authorization resolved it again before comparing, which is
  a realpath per call for an answer already held. This is on every path the product
  authorizes, not only on listings. Measured interleaved in one process over 2000 indexed
  paths: 1167 ms, now 825 ms. A test compares the two forms over live, missing, non-Markdown,
  hidden, traversing, escaping and empty paths and requires the same decision for each.
- `list_notes` admits its candidate paths off the event loop. Admission resolves and stats
  every indexed path, because the page has to be taken from the admitted ones, and it ran on
  the loop: about 410 microseconds per note, so nothing else the server was serving could
  progress for eight seconds on a vault of twenty thousand notes. It is not cheaper now, it
  is survivable, and a test pins that by requiring the loop to keep ticking through a sweep
  that would otherwise freeze it. Making it cheaper means paging admission alongside the SQL
  page, which changes what `total` counts, so it is not done here.
- `get_backlinks` reads four fields per candidate instead of whole chunks, and stops when it
  has a page. It listed every chunk in the vault carrying a wikilink, with its body, and
  turned each one into a validated model before examining the first candidate, so the scan's
  own break saved nothing and a call with `limit=10` still paid for the whole vault. The scan
  now streams identity, note and outgoing links, and the page it keeps is fetched whole in
  one statement afterwards, because the protection pass compares each returned chunk against
  the note as it is on disk right now. Measured on 4800 linking chunks: 202 ms and 17.8 MiB
  of peak allocation, now 2 ms and no measurable allocation when the page fills early. The
  worst case, a target with no backlinks, reads every candidate and measured between 46 and
  116 ms across runs against 202 ms. A chunk that disappears between the scan and the fetch
  is dropped from the page rather than failing the call, which is what the protection pass
  would do with it anyway.
- History retention defaults to 1278 days, forty-two months, instead of 30. Retention decides
  when the only stored copy of a previous version of a note is deleted, and a subject can be
  left untouched for months and then resumed, so a window measured in weeks silently discarded
  the history of everything paused. An existing vault that sets `history_retention_days` keeps
  its own value.
- A long retention window costs a read of that window on the write path, and the sweep is
  throttled to once every thirty seconds of sustained writing. A first attempt skipped the
  read whenever the journal's oldest record was still inside the window, reasoning that
  nothing could then be deleted. That reasoning was wrong and a test caught it: the sweep
  deletes for two independent reasons, and the second is a blob no record names at all, left
  behind when a write stores the previous bytes and then fails before appending its record.
  Telling one of those apart from a live blob needs exactly the set the window scan builds.
- Rebuilding the alias index costs the notes that moved instead of the whole vault. The
  index is dropped after every write, so in the ordinary loop of writing a note and then
  asking for its backlinks it was rebuilt once per write, and rebuilding it read, hashed and
  YAML-parsed every note in the vault to extract four fields from each. Three things
  changed, none of which moves the source of truth off the files: a record carries only the
  identity, title, filename stem and aliases the tiers consume, records are remembered
  between rebuilds and re-read only when a note's `(st_mtime_ns, st_size)` moved, and the
  relative path of a note produced by the vault walk is computed rather than resolved, which
  removed two filesystem round trips per note from the event loop. Measured on 1500 notes,
  interleaved against the previous build in the same process: 2236 ms, now 106 ms. Peak
  allocation during a rebuild fell from 10 MiB to 3 MiB, because whole notes are no longer
  held at once.
- Identity, title and aliases are still resolved by the same helpers a full note read uses,
  so the cheaper build cannot answer differently; a test pins the two against each other
  over a vault covering every tier and every shape of the two inputs that are not stored
  verbatim. The mtime gate is the one `reconcile` already applies. Unlike `reconcile` there
  is no stored hash behind it here, so an out-of-band edit that leaves both the nanosecond
  mtime and the size untouched is not seen until something else drops the records; writes
  through Datacron move the mtime.
- Indexing a vault commits in batches instead of once per note. `upsert_note` and
  `delete_note` each opened and committed their own transaction, which is the right
  boundary for a single tool call and the wrong one for a pass over the vault, so a cold
  index of N notes paid N durable commits and they dominated its wall clock. The store now
  offers a bulk scope that commits every 500 notes, and `reconcile` opens one per pass.
  Nothing weaker is promised than before: the index is derived from the vault, and a crash
  mid-pass leaves it missing the notes of the uncommitted batch, which the next pass indexes
  because their stored mtime is absent. The scope does hold the SQLite write lock for a batch
  rather than for a note, so a concurrent datacron process waits longer, which is why the
  batch is bounded rather than the whole pass.
- One pass writes the ULID sidecar once instead of once per note. Resolving the identity of a
  note with no frontmatter `id` reserialized the whole mapping and replaced the file, so a
  cold index wrote it once per resolved identity and its total bytes grew with the square of
  the vault. Deferring loses nothing: that identity is a pure function of the note's
  vault-relative path, so a pass that dies before the flush recomputes exactly the same
  identities next time. The flush also runs when the pass raises, because leaving a cache
  closer to the truth costs nothing.
- Measured on a cold index of 800 id-less notes: 25.5 s, now 11.1 s. The sidecar went from
  800 rewrites and 17.1 MiB written to one rewrite, and that part of the cost was growing
  quadratically, so it dominates at vault sizes the measurement did not reach. What remains
  is the note reads and the chunk inserts themselves.
- Opening the index for writing no longer rebuilds its derived data every time. Two repairs
  ran unconditionally on every writable open, which is every MCP server start and every
  writable CLI command: one deleted and reinserted every row of `note_frontmatter`, the other
  counted the chunks missing their search context, which on a contentful FTS5 table
  deserializes the body of every chunk. Both exist for rows an earlier release wrote, and on
  every open but the one after an upgrade there was nothing to repair. Measured on 2000 notes
  and 12000 chunks, a 55 MiB index: a writable open took 198 ms against 1 ms read-only, and
  now takes 5 ms.
- A `notes` row records which release wrote it, and the two repairs above run only when a row
  predates the current one. They deliberately carried no one-shot marker, because a release
  that predates the derived data cannot clear one and the next upgrade would skip the repair
  forever, leaving `search_text` and `list_notes` answering the same documented filter
  differently on the same vault. A per-row stamp is not that kind of marker: an earlier
  release does not name the column in its INSERT, so its rows take the column default and are
  visible as stale. A pair table that is empty while notes exist also triggers the repair, so
  a table lost independently of the rows is still rebuilt.
- `list_notes` answers a frontmatter filter through the `note_frontmatter` index instead of
  decoding every note's frontmatter in Python. The schema comment says the table exists so a
  frontmatter filter is an index lookup rather than a scan of every note; `search_text`
  honoured that and `list_notes` did not, so the same filter cost wildly different amounts
  through the two tools. Measured on 4000 notes, in the shape the MCP tool actually calls it,
  which asks for the total and then for every matching path because the scope admission filter
  runs in Python afterwards: 48 ms whatever the filter selected, now 29 ms for a filter
  matching 2666 notes and 20 ms for one matching 80. The cost now follows the answer rather
  than the vault. An index predating the pair table keeps the Python scan, and both branches
  are held to returning the same pages.
- The committed-write baseline is established from the tail of the operation journal when the
  note already has a record there, instead of parsing and hash-verifying the whole file.
  `_check_committed_baseline` calls `latest_record_for_path` on every write that carries no
  `expected_hash`, and that call read and SHA-256-hashed every line of `operations.jsonl`.
  Measured on a 5000 record, 2.2 MiB journal, for a note whose last record is near the tail:
  180 ms, now 1.8 ms, and no longer a function of the journal's length. Proving that a path has
  no record at all still reads back to the first line, because nothing short of that proves an
  absence, so a first write to a new note is unchanged at about 165 ms. The write path also
  still carries a full parse in `_check_request_replay`, which is a separate finding and is not
  addressed here.
- The history retention sweep reads back to its cutoff instead of reading the whole journal.
  It runs after every committed write, at most once every thirty seconds of sustained writing,
  and it needs the hashes named by records inside the window only. Since records are appended
  in time order, those records are a suffix of the journal, so the scan stops at the first
  record older than the cutoff. Measured with the window holding about 410 records while the
  journal grows behind it: 92 ms at 2500 records, 220 ms at 5000, 340 ms at 10000, now 11, 14
  and 12 ms. The cost follows the retention window and no longer the accumulated history,
  which is the whole point: a vault kept for years stops paying for its own age on every write.
  A journal that fits entirely inside the window still reads in full, at parity with before.
- `append_record` refuses a record whose timestamp precedes the journal tail. The retention
  sweep stops at the first record older than its cutoff, which is only the right answer while
  position and time agree; a record out of order would sit behind that stopping point while
  still being inside the window, and its history blob, the only stored copy of that version of
  the note, would be deleted. Equal timestamps stay legal, because two records sharing an
  instant are either both inside the window or both outside it. The sweep additionally checks
  the order over the records it reads and falls back to reading the whole journal if it is ever
  contradicted, and it verifies the hash chain over what it reads, so a journal it cannot trust
  makes it keep everything rather than delete anything.
- A failing history retention sweep no longer fails the write that triggered it. By the time it
  runs, the note, the journal record and the pending cleanup are all durable. Raising there
  reported a committed write as failed, and because the journal records the sweep as done only
  once it finishes, the next write repeated it and failed again, with no way out but repair.
- The write path refuses two corrupt journals it used to accept. A journal whose last line has
  no trailing newline was parsed happily whenever the tail state was already cached, and the
  next append then concatenated two records onto one line. A journal whose head has been cut
  away now fails the oldest line the scan reaches, which must declare itself the chain root.
- The operation journal chains over the canonical rendering of each record everywhere, as
  section 7 of the specification defines and as `read_records` already did. Loading the tail
  state hashed the bytes as they sat on the line instead, so a journal repaired by hand kept a
  meaning every reader agreed on while the next append recorded a `prev_hash` no reader would
  ever recompute, breaking the chain permanently from that point on.
- `get_note_history` and `audit_query` decide read admission once per note instead of once
  per journal record. The scope filter resolved the candidate path and every allowed root
  on each record, on the event loop, so a caller asking about one note blocked the server
  for every record in the vault's history first. Measured over 10000 records spanning 50
  notes: 20000 path resolutions and 5.09 seconds, now 100 and 0.03 seconds. The same
  per-record pattern applied to the recovery inspection paths. The admission decision is
  unchanged; the cache lives for one call.
- One organization apply walks the vault seven times instead of thirteen, and computes the
  case-canonicalization inventory once per validation pass instead of twice. The five stage
  validators run back to back against a vault none of them touches, so each whole-vault
  inventory is now computed once and shared for the duration of that pass. The cache is
  scoped to the pass rather than held on the transaction, which is built once per writer and
  would otherwise answer the next apply from a stale vault; nesting a pass raises, because
  the classifier's loop over several pending batches rolls them forward and moves the vault
  underneath a cached inventory. What remains is `_stage_error` running three times per
  apply and the separate scope walk that streams a SHA-256 of every note in scope.

- One organization apply hashes the organization scope once instead of twice. That sweep
  streams a SHA-256 of every note in scope and is the most expensive step of the preflight;
  it ran from the before-state validation and again from the classifier, against a vault
  that had not changed in between. Measured on one clean apply: two scope walks to one, and
  three full vault walks to two. Recovery still re-classifies every batch it rolls forward,
  because its own loop moves the vault underneath the next one.

- The history retention sweep no longer revalidates every blob from the vault root. It ran
  on every committed write, at most once every thirty seconds of sustained writing and
  unconditionally on the first write after a server start, and it validated each entry with
  a root-to-leaf walk that lstats every path component, twice for a blob it deleted.
  Measured over 2000 blobs: 38020 stat syscalls and 3.14 seconds, now 20 syscalls and 0.41
  seconds, most of it the unlinking. The cost was paid in full even when the sweep deleted
  nothing. A link or reparse point inside the history directory still raises.

### Fixed

- A write request's receipt no longer suppresses a write the note no longer holds. Any journal
  record carrying the request key made the call a replay, and the caller got the old receipt
  with `committed: true` while nothing was written. Revert a note and reissue the same call,
  which is what `prepare_follow_up` does by design because it derives the request id from the
  plan, and the entry was dropped with no trace: a write the caller was told had landed and
  which is nowhere. The receipt is now read against the note as it is. The note still holds
  what the record produced, so it is the ordinary retry after a timeout, a crash or a
  concurrent duplicate, and it replays. The note has moved and the call carries
  `expected_hash`, so the write proceeds and CAS judges it: a reverted note is written again,
  a stale retry raises a conflict. The note has moved and no `expected_hash` was supplied, so
  it refuses and says to re-read and retry with an exact hash, rather than fabricating a
  receipt for bytes that are no longer anywhere or appending the entry twice. This covers all
  nine ordinary write tools, not only follow-up plans.
- `get_note_history` says whether each operation is still a restore point. It returned
  `history_stored`, which records what was stored when the write committed and says nothing
  about now: retention deletes a version once it falls out of the window, so the tool offered
  reverts that could only fail. Each operation now also carries `restore_available`, the
  presence of the bytes its `before_hash` names. A listing checks presence rather than reading
  and rehashing every blob, because a page holds up to `max_result_count` records and
  verifying each would read that many whole note versions to render metadata; `revert_note`
  still verifies the bytes it restores.
- Switching a vault's `history_mode` from `full` to `redacted` no longer destroys the versions
  the `full` period stored. `redacted` means this vault stores no new prior bytes; it never
  meant deleting the ones already on disk. The retention sweep is skipped while history is
  disabled, so the retained set was empty and every blob counted as unreferenced: editing one
  key in `.datacron/VAULT.yaml` and making one unrelated write deleted every earlier version of
  every note, silently, with no confirmation and no way back. The sweep now does nothing at all
  while history is disabled. Removing those bytes is a deliberate act, not a side effect of the
  next write.
- An unterminated `<!--` no longer hides every heading below it, which made a patch on the
  section above replace the rest of the note. Headings inside a closed HTML comment are still
  skipped, which is what that rule exists for; a comment that is never closed now masks
  nothing. An opener with no closing marker is a typo, not an instruction to comment out the
  rest of a note, and hiding gave up nothing in exchange: the damage a closed comment can
  suffer is a marker relocated or orphaned by an edit, and there is no closing marker to
  relocate. Found by the patch property test, which had never generated a bare `<!--` before.

- `search_regex` no longer hangs when it finds enough results before ripgrep has finished
  writing. The search stops reading as soon as it has `limit` matches and kills the child,
  but `Process.wait` returns only once the child has exited *and* every pipe transport it
  owns has closed, and a child killed with output still queued on stdout leaves that
  transport open. The call then waited forever on output nobody would ever read, with the
  server thread held. This is the ordinary path for any pattern with many matches, not an
  error path: reproduced with a pattern matching every note of a 300 note vault, where the
  tool ran for over ten minutes without returning, while a pattern matching nothing
  resolvable on the same vault answered in 1.3 seconds. The remaining output is now read
  before the wait, which is bounded because the child is already dead.

## [2026.0918.00] - 2026-09-18

### Security

- A vault-relative path is now refused while it is still a string, before it is joined to
  the vault root or resolved. Path confinement resolved its argument first, and on Windows
  that resolution is a `CreateFileW` call, so a UNC argument such as
  `get_note(id_or_path="//host/share/x.md")` opened an SMB session against a
  caller-named host, with the automatic NTLM response that implies, before the path was
  refused. The same argument also stalled the stdio server for the network timeout, since
  the check runs synchronously inside the async tool bodies. Reachable from `get_note`,
  `list_notes`, `search_text`, `session_context`, `get_follow_up` and every write tool. A
  drive letter, an absolute path and directory traversal are refused by the same screen,
  which keeps raising `PathConfinementError`.
- `contradiction_scan` no longer offers a candidate for automatic correction when the
  source note carries a secret outside the cited section. The gate read the source only
  through the indexed chunks of one header path, which exclude frontmatter, headings, code
  fences and subsections, so a key one heading below left the candidate addressable. The
  scan displayed the provenance block as `[REDACTED]` while `mode="confirm"` returned the
  same text unredacted and proposed writing it into the target note, against a
  `proposal_token` computed over content the operator was never shown. Both notes are now
  checked in full.
- `contradiction_scan` no longer exposes a `chunk_id` whose heading slug carries a secret:
  when the redactor changes a section's `header_path`, its `target` or `source` reference
  carries `chunk_id: null` and `chunk_id_redacted: true`. Confirmation is unaffected, the
  `proposal_token` alone identifies the candidate.

### Added

- `publish-pypi` refuses to build a distribution when the pushed tag is not `v` followed by
  `datacron.__version__`, read through the CalVer normalizer that `check_invariants.py`
  uses. `scripts/release_preflight.py` gains a `tagged` phase with the same check, and its
  `committed` phase applies it before the tag is pushed. PyPI is immutable; the Windows leg
  of `release.yml` was the only place that compared the two.
- A documentation link guard checks every internal anchor of the English and French pages
  and of both READMEs against the headings GitHub derives (accented letters kept, a colon
  between two spaces becomes a double hyphen), and every page under `docs/en` and `docs/fr`
  must be listed by its language index.

### Changed

- The index reconcile pre-pass keeps only the identity and content hash of each note it
  reads, then reads a changed note again when it commits it, so `datacron index --full` and
  `apply_organization_manifest` no longer hold every note of the vault in memory (a full pass
  over 2000 synthetic notes peaks at 2.4 MiB of traced allocations instead of 14.6 MiB, for
  about six percent more time). The progress counter now also advances during that
  pre-pass, for every note whose content is unchanged; a new or changed note still
  counts when it is committed.
- `create_note_ai` enforces the tag policy that the server read at startup, with the rest
  of `VAULT.yaml`, instead of parsing the file again on every creation. Editing the
  policy now takes effect after a server restart, like the excluded folders and the
  query expansion already did.
- One first-level heading pattern: `core.vault.H1_PATTERN` is the only definition, and
  the batch transaction and the organization manifest import it. The manifest previously
  kept its own pattern without the CommonMark indentation tolerance, so a title derived
  from an H1 indented by one to three spaces now matches the reader, the planner and the
  offline library instead of falling back to the filename.
- The test suite strips every `Settings` variable from the environment before each test.
  The list is derived from the model fields and the `DATACRON_` prefix, and a guard test
  fails when a field has no isolated variable; the former manual list ignored eight fields,
  so a host `DATACRON_SESSION_CONTEXT_SECTIONS` changed a session-sections test result.
- The documentation indexes list the daily workflows, note sections, offline library,
  reliability, contradiction, follow-up, frontmatter audit, proposal token and release notes
  pages; four French anchors point at the slugs GitHub derives; the architecture layout tree
  is regenerated from the repository and its footer keeps the date only. The sdist ships
  `docs/en/spec.md` next to `docs/fr/spec.md`.
- Error-path tests cover the vault glob segment semantics and refusals, the reference
  bounds, duplicate, budget, vanished-target and conflict paths of `get_write_progress`,
  and the source-side selector and `rel_path` refusals of `move_note_section`.

- The offline library resolves links through casefolded lookup tables built once per
  audit, parses each note body once for the audit and the navigation, and `prepare`
  checks the bundle it just wrote against the notes it just read. An audit of 2000 notes
  with 24 links each drops from minutes to seconds; findings are unchanged.
- One wikilink parser: the organization planner and the offline library extract wikilink
  targets through the indexing parser. The planner no longer counts a wikilink inside inline
  code, an escaped `\[[...]]` or a tilde fence as a link, and the library normalizes the
  whitespace inside `[[ target ]]` the way the index does. Two more audit findings follow
  the parser: a block reference (`[[Note#^block]]`) is not a heading and is no longer
  reported as an unverified anchor, and a bash condition written in prose (`[[ -f x ]]`) is
  no longer read as a link. A same-note anchor (`[[#Heading]]`) is still audited and reports
  `ANCHOR_UNVERIFIED` when the heading does not exist.
- `datacron library` options carry help text, `--vault` falls back to `DATACRON_VAULT_ROOT`
  and then to a current directory that holds `.datacron/VAULT.yaml`, like the other vault
  commands, and each command documents its exit codes in `--help`. An explicit vault binds
  the read and write scope of the command like every other vault command.
- `session_context` selects no orientation section by default: the two French headings of
  one particular `INIT.md` that were built into the package are gone. An orientation note
  without configured sections returns its opening text and reports `section_selection.mode`
  as `full` with the reason `no_sections_configured`, so every orientation source now
  carries `section_selection`. Select sections through `DATACRON_SESSION_CONTEXT_SECTIONS`;
  the note-sections page documents the JSON format with an example.
- The archive tags and the state-note namespace are declared once, in the core
  configuration. A vault overrides the archive tags through `organization.tags.archive_tags`
  in `VAULT.yaml`; the index, the offline library and the planner follow the same source.
  The planner names its expectation for a missing state note as `one note carrying any
  kind/* tag`, which is what it checks.
- The library command module lives outside the domain package (`datacron.cli_library`);
  the `datacron library` commands and their help are unchanged.

### Fixed

- `append_journal`, `patch_note_section`, `rename_note_section`, `delete_note_section` and
  `patch_note_preamble` no longer delete the content above a leading `---` block that is
  not frontmatter. The exact body span was computed from the two delimiter lines alone,
  while `python-frontmatter` reported no metadata, so a note opening with a thematic break
  or with a leading block holding a YAML list or scalar had everything above the second
  delimiter dropped on the next write, and the tool reported success. The parser now keeps
  the whole document as the body when the block is not a mapping, and the five tools refuse
  an ambiguous block outright. A block that parses to an empty mapping is still
  frontmatter.
- A heading inside an HTML comment is no longer treated as a live section. mistletoe parses
  no HTML block, so `move_note_section` accepted a commented-out draft, carried the closing
  `-->` away with it and buried the section that followed, while reporting that every byte
  was preserved; `delete_note_section` orphaned the opener and swallowed the rest of the
  note. Fenced code is respected, so a `<!--` inside a fence opens nothing.
- One undecodable byte in the vault no longer breaks every index-backed tool. The reader
  already skips an unreadable note and logs its path, but reconcile propagated the
  `UnicodeDecodeError` out of the read repair and through `search_text`, `search_regex`,
  `get_note`, `list_notes` and the advisory tools, with a message naming the byte offset
  and not the file. `reindex` went through the same path, so the documented repair could
  not run either. Reconcile now applies the reader's policy, names each skipped note and
  reports the count; existing index rows for a note it could not read are kept.
- `contradiction_scan` passes its own structured-output validation when redaction is
  active. The tool declared `chunk_id` as a required non-nullable string while the
  redaction branch emits `null`, and redaction is on by default, so a vault holding one
  heading that trips a default pattern turned the whole tool into a transport-level
  failure. `chunk_id_redacted` is now declared as well, having been emitted and then
  stripped from `structured_content`.
- A vault-relative path whose directory component ends in a dot or a space no longer wedges
  every subsequent write. Win32 strips a trailing space from the final component only, so
  `create_note_ai(rel_path="_memory/facts /note.md")` created a directory named `facts`,
  wrote a pending journal record naming `facts `, then failed to open its temporary file.
  The record survived, and recovery, which runs before every write, could no longer resolve
  it: `create_note_ai`, `patch_note_section`, `append_journal`, `set_frontmatter`,
  `revert_note`, `repair_recovery` and `datacron ops` all raised, and only deleting the
  pending file by hand restored writes. Such a path is now refused up front; a note write
  that fails synchronously discards the pending record it created, unless the target
  already holds the prepared bytes; and recovery tolerates the extended-length form that
  Windows returns for a partially existing path.
- The frontmatter pair table is indexed by `note_id`, so every index write removes a
  note's pairs through an index lookup instead of a full table scan. A full reindex of a
  large vault was quadratic in the number of stored pairs.
- Operation journal appends are constant-time again: a mutating tool appends its chained
  record in place, fsyncs it and reads only the journal tail. Full hash chain verification
  stays with the readers (`audit_query`, `get_note_history`, `revert_note` and recovery).
- `get_write_progress` refuses a reference that is not an admitted live note, or that
  escapes the vault, with the typed `note_not_admitted` error instead of an internal error,
  reports a target that vanished before it was read as `target_unavailable`, and accepts a
  note ULID in `note` like `get_note_history` and `revert_note`.
- `contradiction_scan` evidence and block previews pass through the vault content sandbox;
  the confirmed `new_content` write payload stays byte-exact.
- `datacron library prepare` resolves the title of an archived note without a frontmatter
  `title` from its first heading or filename, like the vault reader, and reports a missing
  note field with exit code 2 instead of a traceback.
- `move_note_section` errors carry the failing `selector` (`source` or `destination`), and a
  destination message names `destination_level` and `destination_occurrence`.
- `get_follow_up` returns `follow_up_offset_invalid`, with a next action, for a negative
  offset or one beyond `total`.
- A `session_context` fallback report no longer shares one entry between
  `unavailable_sections` and `omitted_sections`.

## [2026.0913.02] - 2026-09-13

### Added

- Offline library commands (`audit`, `prepare`, `check`, `split`) build a readable
  Markdown preview with navigation, local attachments and English or French labels.
  Sourced consolidation recipes preserve originals and prepare a reviewable organization
  manifest with source hashes, diffs and archive safeguards.
- Prioritized session context with adaptive budgets and an optional cached contract hash.
- Read-only `get_write_progress` reports durable receipts, conflicts and indexing status.
- Fictional conversation acceptance cases, trace evaluation, concurrent session benchmarks,
  and an opt-in Windows runtime validation workflow.

### Changed

- Explicit archive lifecycle metadata influences retrieval ranking and is writable through
  `set_frontmatter` with concurrency checks and durable replay.
- Health and tool errors include actionable recovery guidance.
- English and French guides cover daily workflows and offline note organization.
- Public examples use fictional identities and neutral local paths.

### Fixed

- Concurrent index upserts reserve the SQLite write transaction before checking note
  identity, preventing read-to-write lock conflicts between independent clients.
- Contextual redaction covers note titles, headings and ancestor metadata while returning
  opaque chunk aliases without changing stored identities.
- Duplicate note identities are rejected before index reconciliation mutates state.
- Missing entries in a stale identity sidecar trigger live discovery rather than false absence.

## [2026.0913.01] - 2026-09-13

### Added

- `move_note_section` previews and commits exact heading subtree moves within one note,
  with mandatory CAS, explicit confirmation, durable replay and index reconciliation.
- `get_note` reads an exact heading ancestry and its subtree with section-relative
  pagination, source coordinates and the original note hash.
- `set_frontmatter` accepts a monotone `last_id` backlog counter with mandatory CAS.
  Existing requests without this field retain their durable replay fingerprints.
- Configurable section excerpts in `session_context` make orientation headings available
  within the existing note and response budgets, with explicit continuation and fallback.
- Missing-heading write errors provide bounded, sanitized suggestions without selecting
  or changing a section automatically.

### Fixed

- An unknown chunk identifier now returns an explicit error instead of the whole parent note.
- Release preflight validates effective and committed identities against the configured
  GitHub noreply email, replacing the obsolete empty-email requirement.

## [2026.0913.00] - 2026-09-13

### Added

- Three organization deviations, measured by `datacron reorganize` and projected by
  `apply_organization_manifest` from the bundle's payload bytes, so the report signed at
  validation equals the report measured after apply. `NO_STATE_NOTE`: a subject folder that
  holds at least `organization.state_note_min_notes` governed notes and none tagged `kind/*`;
  the deviation names the folder. `UNLINKED`: a note of a subject folder dated on or after
  `organization.linking_since` that carries no wikilink to a state note of its folder, by
  stem, frontmatter title or alias, case-insensitively; state notes and `-history-` stems
  are exempt, and a wikilink inside a fenced block does not count. `UNBALANCED_FENCE`: a
  governed note whose body has an odd number of lines starting with three backticks after at
  most three spaces of indentation; it needs no key. Both new keys default to absent, which
  means not measured, so an existing sidecar measures exactly what it measured before. They
  are read strictly (a boolean is not a count, a number is not a date) and require a tag
  policy with a `subject_namespace`, the namespace of the subject rules whose folders they
  measure; either key declared without it is a load-time error that names the key. An
  executable older than this version refuses a `VAULT.yaml` that declares them, as it does
  for the `tags` block: upgrade every installation first.
- `datacron reorganize --freshness-days N` lists, after the counters, every governed note
  tagged `kind/*` whose `last_verified` is missing or older than `N` days relative to the
  UTC run date, sorted by path, as a `freshness` field in JSON and a `Freshness` block in
  text. The list is informative: the exit code is unchanged, and the manifest projection,
  which has no run date, never carries it.
- The planner snapshot derives, once per note and without keeping any prose, the frontmatter
  title and aliases (strings only), the wikilink targets outside fenced blocks, the fence
  line count and the rendered `last_verified` day; both snapshot builders share one
  derivation, which folds CRLF and lone CR to LF first, so a CRLF note measures the same
  from the filesystem scan and from a manifest payload.

### Changed

- The organization report schema is `organization-plan-v2`: `counts` carries one key per
  kind, nine in all (sorted alphabetically in the serialized JSON, in declared order in the
  text report), `--kind` accepts the three new names, and the text report aligns its columns
  on the longest kind. A vault without rules still answers `--freshness-days` with an empty
  list. A bundle validated under version 1 is not replayable across the upgrade, which is
  acceptable: a validate token never survives any vault write either.
- The report contract states its counter identity as `scanned = governed + unmatched +
  skipped`: a note the planner cannot read is scanned, then skipped, and is neither
  governed nor unmatched. Earlier wording omitted the `skipped` term; the counters
  themselves are unchanged.
- The `MISSING_KIND` gap sketched in the organization inventory is not implemented. With the
  state note recognised by its `kind/*` tag rather than by its stem, "a state note without a
  kind" is not measurable; `NO_STATE_NOTE` covers the same need at folder level.
- The organization pages, in English and French, describe the nine kinds, the two keys, the
  state note and link conventions, `--freshness-days`, the text and JSON samples and the
  schema version.

### Upgrade

- No reindex and no protocol install: the index layout and the memory protocol are
  unchanged. Upgrade every installation before declaring `state_note_min_notes` or
  `linking_since` in `VAULT.yaml`; an older executable refuses the keys and stops its server.

## [2026.0912.03] - 2026-09-12

### Added

- The `prepare_follow_up` tool description ends with the record schema rendered as prose,
  one clause per property: required fields, extra fields refused, identifier, ULID and hash
  patterns, enumerations, text bounds, nullable alternatives, defaults, and the runtime limit
  on records per call. Some MCP clients present the input schema without its definitions or
  patterns; the description carries the same constraints for them. The JSON schema itself is
  unchanged. The renderer refuses any schema keyword, type, format, variant or name it cannot
  express unambiguously, so a constraint added to the model can never be silently missing
  from the description, and a test compares every rendered clause with an expectation rebuilt
  from the listed schema.
- `datacron protocol install --client claude-desktop` prints the one-line session start
  instruction to paste into the Claude preferences, the first sentence of the memory
  contract; `datacron protocol uninstall` tells the user to remove it.

### Changed

- `datacron protocol status` reports `claude-desktop` as `manual` at user scope, like Cursor,
  instead of `unverified`. The Claude Desktop chat presents the tool schemas intact but does not
  present the MCP server instructions to the model, so the memory contract reaches that client
  only through the user's Claude preferences; `session_context` returns the full contract text
  in `contract.instructions`. Earlier entries of this changelog describing Claude Desktop as
  receiving the instructions during MCP initialization recorded an assumption that this
  measurement refutes. The README, installation, setup and memory discipline pages, in English
  and French, describe the manual step.
- Upgrading from `2026.0912.02` changes no chunk ID, index format, note bytes or protocol block:
  the chunker, the index store and the memory contract bytes are untouched, so no
  `datacron reindex` and no `datacron protocol install` are required for this increment.
  Reconnect the MCP server so the client reloads the `prepare_follow_up` description.

## [2026.0912.02] - 2026-09-12

### Added

- Identity adoption through the organization manifest: a `replace_exact` whose source note has
  no frontmatter id is accepted when the manifest spells the target exactly as the file exists
  on disk, the manifest's expected id is the identity the ULID sidecar already maps to that exact
  path (the key the reader resolves) and the payload carries the same id. The replacement writes
  the id into the note; the index, backlinks and history keep the identity they always used.
  Batch commit and recovery check the same sidecar baseline. Move sources without a frontmatter
  id stay unsupported, and a frontmatter id that is present but not a string is still refused.

### Changed

- Upgrading from `2026.0912.01` changes no chunk ID, index format, note bytes or protocol block:
  the chunker, the index store and the memory contract are untouched, so no `datacron reindex`
  and no `datacron protocol install` are required for this increment.

## [2026.0912.01] - 2026-09-12

### Added

- Subject placement rules: with an `organization.tags` policy declared, a placement rule may
  be keyed by a registered subject tag (`project/heimdall` to `_memory/subjects/perso/heimdall`)
  next to the rules keyed by placement tags. The winning rule still decides the folder, the
  naming template and the size ceiling; the policy still requires exactly one placement tag
  per note and does not count the subject rule as one. Markers and exempt tags must name
  placement rules, and at least one placement rule must remain.

### Changed

- Configuration validation refuses a rule tag that is neither in the placement namespace nor
  a declared subject with a message naming both conditions; before this version every rule
  tag had to live in the placement namespace as soon as the policy was declared. A marker or
  exempt tag that names a subject rule is refused with a new message; one that names no rule
  at all keeps the previous message.
- Upgrading from `2026.0912.00` changes no chunk ID, index format, note bytes or protocol block:
  the chunker, the index store and the memory contract are untouched, so no `datacron reindex`
  and no `datacron protocol install` are required for this increment.

## [2026.0912.00] - 2026-09-12

### Added

- Vault-declared tag policy: an optional `organization.tags` block in `.datacron/VAULT.yaml`
  declares the placement namespace, the transversal markers, the subject namespace, a closed
  subject registry with aliases, the placement tags exempt from the single-subject rule, and
  the other admitted namespaces. Datacron still ships no taxonomy.
- `create_note_ai` refuses, with the typed error code `tag_policy_violation`, a note inside the
  organization scope whose effective tags (frontmatter plus inline `#tags`) break the declared
  policy; nothing is written.
- `apply_organization_manifest` refuses at validation, with the same code, a bundle whose result
  notes break the policy of the target configuration; untouched notes are not judged.
- `datacron reorganize` reports three new deviation kinds when a policy is declared:
  `UNGOVERNED`, `UNKNOWN_TAG` and `TAG_CARDINALITY`; `--kind` accepts them. The identity
  `scanned = governed + unmatched` is unchanged.

### Changed

- Memory protocol contract `1.1.0`: the shared discipline states the declared tag policy in one
  sentence and trims a few phrasings to stay under the client size ceiling. Reinstall the client
  blocks with `datacron protocol install` after upgrading.
- The `reorganize` JSON report now lists six `counts` keys on every vault, the three policy
  counters staying at zero without a policy, and `--kind` accepts the three new names; every
  other field of the report is unchanged. Vaults without an `organization.tags` block are not
  judged.
- Deployment constraint: an executable that predates this version refuses a `VAULT.yaml` that
  declares `organization.tags` (`extra_forbidden`), so every Datacron installation sharing a
  vault must be upgraded before the block is declared. Only `create_note_ai` and the
  organization manifest are gated; body mutations are measured by `reorganize`, not refused.
- Upgrading from `2026.0911.00` changes no chunk ID, index format or note bytes: the chunker
  and the index store are untouched, so no `datacron reindex` is required for this increment.
  Vaults still on `2026.0910.00` or earlier keep the full reindex requirement documented for
  `2026.0910.02`.

## [2026.0911.00] - 2026-09-11

### Fixed

- Return the `session_context` budget refusal as a typed tool error (`isError=true`, `code`
  `context_budget_too_small`, `type` `ContextBudgetError`) instead of a result outside the
  declared `SessionContextOutput` schema, and carry `required_tokens` on both refusal paths so
  a schema-validating client can retry with a sufficient budget.

### Changed

- Upgrading from `2026.0910.02` changes only the `session_context` refusal path: no chunk
  ID, index format or note bytes change, so no `datacron reindex` is required for this
  increment. Vaults still on `2026.0910.00` or earlier keep the full reindex requirement
  documented for `2026.0910.02`.

## [2026.0910.02] - 2026-09-10

### Fixed

- Preserve Markdown heading hierarchy, remove retrieval envelopes from stored follow-up
  payloads, and resolve regex globs relative to the vault with explicit error reporting.
- Build contradiction proposals from complete source sections, return typed section
  errors, and audit only changed frontmatter fields.

### Changed

- Document proposal tokens without a time-based expiry and report unresolved tokens as
  `proposal_token_stale_or_unknown`, without claiming a definite cause.
- Upgrades from `2026.0910.00` or earlier require a full `datacron reindex` and refreshed
  chunk references. The local `.01` candidate rebuilt 2,431 notes in 4 min 20 s
  (260.197 seconds), replacing 22,364 chunk IDs across 641 notes. This is one Windows
  vault measurement, not a duration guarantee. See the
  [upgrade notes](docs/en/surface-fixes-release-notes.md) and
  [French translation](docs/fr/surface-fixes-release-notes.md), including validation limits.
- Reserve `.01` for the local migration candidate; `.02` is the intended public version.

### Known issues

- With an insufficient budget, `session_context` can return a refusal outside its
  declared output schema, hiding `required_tokens` from strict clients. This
  pre-existing issue remains unchanged. Retry with a larger budget or read
  `_memory/INIT.md` through `get_note` when available; see the upgrade notes.

## [2026.0910.01] - 2026-09-10

### Fixed

- Preserve sibling heading boundaries when a note has no H1 or skips heading levels.
- Store follow-up payload text without retrieval envelopes while retaining legacy compatibility.
- Resolve regex globs relative to the vault and report invalid or unmatched globs explicitly.
- Build contradiction proposals from the complete source section instead of a truncated excerpt.
- Return typed section-selection errors and record only changed frontmatter fields in audit receipts.

### Changed

- Document proposal tokens without a time-based expiry and return
  `proposal_token_stale_or_unknown` when a proposal can no longer be resolved.
- Require a full `datacron reindex` after installation because affected chunk IDs change.
  Retrieve fresh chunk references and rescan unresolved contradiction proposals.
  This version is a local migration candidate. Its local vault migration rebuilt
  2,431 notes in 260.197 seconds; this measurement is not a duration guarantee.
  See the [upgrade notes](docs/en/surface-fixes-release-notes.md)
  and their [French translation](docs/fr/surface-fixes-release-notes.md).

## [2026.0910.00] - 2026-09-10

### Fixed

- `search_regex` no longer fails on every query when ripgrep is absent. The indexed
  fallback used to build a list of every chunk in the vault inside its own deadline,
  and to run the admission predicate once per chunk while doing so. Admission resolves
  and stats a path, measured at 552 to 902 microseconds per call, so on a 94589-chunk
  vault the 2.0s budget was gone after 2432 chunks and the scan never reached the
  matching step. The glob filter, the `limit` early exit and the catastrophic-pattern
  guard all sat downstream of that point and were therefore unreachable, which is why
  a trivial literal failed exactly like a pathological one. The fallback now streams
  the index, applies the cheap lexical glob first, matches in bounded worker batches,
  and admits only the chunks whose body already matched, once per distinct note. A
  query with matches stops at the first `limit` of them instead of paying for the whole
  index.
- A catastrophic regex is refused before the index is read, so the refusal is reported
  as such instead of surfacing as a timeout on a large vault.
- An explicit `rg_path` argument now outranks `DATACRON_RIPGREP_PATH`, matching the
  precedence everywhere else in the settings, and a blank environment value falls
  through to the default instead of blanking the command.
- A ripgrep binary that exists but cannot be launched, such as a path that is not a
  valid executable, now routes to the indexed fallback like an absent one. Only
  `FileNotFoundError` was caught before.

### Added

- `datacron status` names the regex backend in use, `session_context` reports a
  `regex_search_ripgrep` capability, and the server logs a warning at startup when
  ripgrep cannot be resolved. A missing prerequisite was previously invisible until a
  query failed, and the failure named a timeout rather than the absent binary. An MCP
  client does not hand its own PATH to the server it starts, so a shell that finds `rg`
  proves nothing about the server.
- `DATACRON_REGEX_FALLBACK_TIMEOUT_SECONDS` and
  `DATACRON_REGEX_FALLBACK_MAX_PATTERN_LENGTH` are documented. The fallback budget
  default moves from 2.0s to 10.0s: a complete scan that matches nothing measures 1.7s
  over 94589 chunks, and the headroom covers a cold cache and vault growth.
- Regression coverage at scale. Every previous fallback test ran on three chunks, which
  is how a glob and a limit that were unreachable in production stayed green in CI.

### Changed

- The documented security boundary now states that `search_regex` starts no process
  when ripgrep is unavailable, and instead compiles and evaluates the caller's pattern
  in-process against indexed chunk bodies.

## [2026.0909.00] - 2026-09-09

### Added

- `search_text` accepts `folder`, `tags`, and `frontmatter` scope filters with the
  `list_notes` semantics, and echoes the filters it applied. The OR fallback for multi-term
  queries honours the same scope.
- `search_text` accepts `group_by_note`: the best-ranked chunk of each note survives and
  carries `note_matches`, the number of that note's matching chunks, asked of the index rather
  than counted off the bounded result window; the response reports `grouped_by_note`. On the
  retrieval corpus the returned tokens drop from 26382 to 15929 for the same 44 questions.
- The index keeps a `note_frontmatter` table of casefolded top-level pairs, filled on write
  and rebuilt on every writable open, so a `frontmatter` filter is an index lookup instead of
  a scan of every note. A read-only legacy index keeps the scan.
- The retrieval corpus grows from 32 to 44 questions with hard cases: title against passing
  mentions, heading-only matches, duplicate section titles, bilingual queries, backlog and
  archive distractors, and an `invalid_at` note behind its replacement. The integration gate
  requires note recall@5 of 0.99, MRR of 0.94 and nDCG@10 of 0.95.
- A documentation guard refuses a hyphen glued to a following conjunction, the damage an em
  dash removal leaves behind, and requires every public page to link to its translation.

### Changed

- `session_context` scopes its subject search to the domain tag, so project, people and
  meeting candidates are no longer crowded out by notes the domain filter discards.
- Search excerpts fall back to the note title and heading trail when only that context
  matched, instead of showing the unrelated start of the chunk body. Which column matched is
  decided on private markers, so a body containing Markdown bold no longer counts as a match.
- The chunk context holds the note title once. The heading trail starts at the H1 a title is
  usually resolved from, so joining them naively applied an undeclared weight multiplier to
  the notes whose H1 restates their title, and to no others.
- The context-column migration logs the number of migrated chunks and its duration.
- `pytest-xdist` joins the development dependencies; `pytest -n auto` runs the suite in
  parallel with one temporary directory per worker.
- The FTS index carries a `context` column holding the note title and heading trail of each
  chunk, weighted three times the chunk body in BM25 scoring. A writable open migrates a
  legacy index in place, preserving chunk identities and hashes; a certified read-only open
  keeps searching a legacy index with unweighted scoring until it is rebuilt.
- On the enriched retrieval corpus, MRR moves from 0.922 to 0.950 and nDCG@10 from 0.944 to
  0.964, with note recall@5, empty-answer accuracy and forbidden-path violations unchanged at
  1.0, 1.0 and 0.

### Fixed

- A secret carried by a note title or a section heading no longer reaches the client in clear
  text. An excerpt taken from the context column now supplies its own undecorated redaction
  source; the existing guard compares against the chunk body and could not see it.
- `search_text` and `list_notes` answer a `frontmatter` filter identically. Filter pairs are
  derived from the serialized metadata both tools compare against, so an unquoted YAML
  timestamp is no longer indexed with a space where the other tool expects a `T`.
- An index written by an older release is repaired on the next writable open instead of being
  skipped forever by a one-shot marker: frontmatter pairs are rebuilt and chunks left without
  a context are refilled.
- Reindexing a note under a new identity at a stable path no longer leaves its former
  frontmatter pairs behind.
- `datacron status` opens the index read-only. A writable open runs the schema migrations, so
  reporting a note count would rename and refill the chunk table under a write lock that a
  running `datacron mcp serve` contends with. It also stops naming `datacron reindex` for a
  failure a rebuild cannot fix, and distinguishes a corrupt index from one it cannot open.
- Follow-up owner metadata is sandboxed during preparation and retrieval, including older
  revisions, while historical hashes and replay compatibility remain intact.
- Current follow-up reads expose snapshot-bound pagination and an actionable error when
  one record exceeds the response budget, preventing unreachable trailing commitments.
- Session context passes plain search text to the index without adding a spurious `OR` term.
- Public structured tool errors respect the configured retrieval secret-redaction policy.

## [2026.0905.01] - 2026-09-05

### Added

- A shared, versioned memory discipline for server and client instructions, with read-only
  `protocol status` diagnostics that distinguish distribution from observed behavior.
- Bounded `session_context`, sourced `prepare_follow_up` plans using existing journal writers,
  and integrity-checked `get_follow_up` projections for current commitments and people history.
- Synthetic daily-follow-up scenarios covering meetings, projects, professional objectives,
  conversation preparation, waiting-for replies, interruption, weekly reviews and decisions.

### Fixed

- Retrieval checks secret spans in the complete parent note before exposing split chunks
  or search excerpts. Fragments crossing a secret boundary are concealed; raw hashes stay intact.
- Search budgets account for serialized result metadata, escaping and sandbox envelopes.
  Oversized excerpts retain the matching region and report truncation.
- Regex search reads large JSON frames progressively, with an explicit configurable byte
  ceiling and actionable `regex_frame_too_large` errors instead of a hidden stream limit.
- Regex result limits count resolved, admitted matches; frontmatter and excluded matches
  no longer hide valid body results.

## [2026.0905.00] - 2026-09-05

### Added

- Optional `request_id` on all eight ordinary note writers binds exact arguments to a
  durable operation receipt. Identical retries after restart return the historical receipt;
  conflicting reuse is refused under the existing cross-process mutation lock.
- `get_note_history` accepts `request_id` to locate a receipt without repeating a mutation.
- A versioned 32-question bilingual retrieval corpus covers ambiguity, superseded notes,
  excluded paths and absent answers. Eval reports separate empty-query accuracy and gate
  its regression alongside freshness. `scripts/benchmark_writes.py` measures disposable vaults.

### Changed

- Ordinary writes index only the committed target; global read repair and health remain
  responsible for vault-wide consistency. Unrelated malformed files do not break write receipts.
- Maps and section mutations share AST heading identities. Setext and closing ATX headings
  are supported, code-block headings ignored, and ambiguous journal headings refused.
- Publication reuses the complete six-platform/Python CI matrix, dependency audit and
  ShellCheck; an aggregate Quality gate fails if any required job fails or is skipped.

### Fixed

- Search detects secrets on undecorated source text before returning highlighted excerpts.
  Sensitive excerpts use the masked source without highlighting, including FTS, ripgrep and
  the indexed fallback.
- Chunk line coordinates remain aligned with physical LF/CRLF files and UTF-8 BOM notes.
- Indexed ULID lookups validate the live identity and resolve moved notes instead of returning
  an unrelated replacement at the old path.
- Ordinary note writes return `error.code=committed_index_incomplete`, the committed
  `content_hash`, `committed=true` and `indexed=false` when post-commit reconciliation fails.
  Callers must re-read and repair the index rather than repeat the mutation.

## [2026.0831.00] - 2026-08-31

### Fixed

- Reading a note now absorbs the transient Windows sharing violation raised while another
  writer atomically replaces that same path, instead of surfacing it as an internal error.
  Two concurrent appends to one note could make the post-write index reconciliation fail its
  re-read and report failure for a mutation that had already been committed durably. Only the
  transient codes are retried, with bounded exponential backoff; a durable permission failure
  and every non-Windows platform still fail on the first attempt.

## [2026.0830.00] - 2026-08-30

### Added

- A new `apply_organization_manifest` MCP tool validates a local content-addressed bundle without
  writing, returns a confirmation token bound to the exact admitted vault state, and applies the
  same note, organization-configuration, and derived identity-sidecar changes only after that
  token is presented.
- Organization batches keep durable pending and committed receipts so an interrupted application
  can be recovered or replayed deterministically without silently repeating completed mutations.

### Changed

- Organization planning and application now share strict path, scope, admission, naming, and
  identity checks, including case-insensitive collision handling on Windows and exact before/after
  hashes for every declared or derived member.
- Operational guidance now requires a single-writer maintenance window and a verified byte-exact
  backup outside the vault before applying an organization manifest.

### Fixed

- Ordinary note writers now fail closed while an organization batch is pending instead of
  observing or extending a partially applied global reorganization.

## [2026.0829.01] - 2026-08-29

### Added

- Organization naming rules can use `{iso_date}` exactly once at the start of a template to
  require a valid ASCII `YYYY-MM-DD` calendar date without comparing it with frontmatter
  lifecycle fields. The existing `{date}` token keeps its exact `created`-then-`updated`
  semantics.

## [2026.0829.00] - 2026-08-29

### Added

- An optional `organization` block in `.datacron/VAULT.yaml` declares where notes carrying a
  given tag belong, how they should be named, and an optional size ceiling. Rule order is
  priority order: the first rule whose tag is present on a note wins, which is the tie-break
  for notes carrying several tags at once. A vault without the block is unaffected.
- `datacron reorganize --dry-run` reports the gap between a vault and the organization it
  declares, as text or as stable JSON, optionally narrowed with `--kind`. The command is
  read-only: it never moves, renames or rewrites a note. It exits 0 when the report is empty,
  1 when it is not, and 2 on a configuration error, so a non-empty report stays detectable in
  CI without being an error. `--dry-run` is mandatory and deliberately never implicit.
- Active organization rules now declare an explicit, confined vault scope. Planning shares the
  canonical note-admission policy, reports only that scope, and matches `{date}` to frontmatter
  `created` (falling back to `updated`) instead of accepting any date-shaped filename.

### Changed

- The Windows release command now verifies a clean, synchronized `main`, empty Git identity
  emails, absent release tags, exact version-only changes, and the final commit and annotated tag
  before pushing the branch and tag atomically.
- The bilingual architecture and Ollama guides now record the implemented structured-read
  contracts and distinguish the completed BL-0019 campaign from the parked BL-0107 follow-up.

### Fixed

- Invalid YAML types in organization rules now fail with exit code 2 instead of being coerced
  into text or silently disabling organization.
- A manual `publish-pypi` workflow dispatch from a branch can no longer reach the PyPI
  publication job; publishing now requires a `v*` tag reference.

## [2026.0828.01] - 2026-08-28

### Added

- An opt-in `DATACRON_TOOL_DESCRIPTION_PROFILE=compact` mode strengthens the `search_text`
  usage trigger for compact language models without changing tool schemas or handlers.

### Changed

- Operational documentation and every public `--vault` help surface now describe the measured
  admission, durability, reindex, identity-repair, and vault-root fallback contracts without
  treating read exclusions as write ACLs or best-effort flushes as confirmed durability.
- The typed `get_health` output now includes the bounded `recovery` block already emitted by the
  runtime, including blocked operation identifiers and content-free hash evidence.

### Fixed

- Certified read-only readers now keep SQLite locking and change detection enabled. A running
  reader can follow committed index updates from another process instead of retaining an
  `immutable=1` snapshot that eventually reports a live database as malformed.
- A complete `datacron reindex` now publishes and advances the generation even when the vault is
  empty, instead of rejecting a valid zero-note replacement as an incomplete rebuild.
- `datacron ops repair-id` now rechecks the note hash while holding the identity and note locks
  through sidecar/index realignment. Its inspection recommendation also falls back to another safe
  source or reports why no action passes collision and migrated-sidecar preflight, instead of
  proposing an action the repair would immediately refuse.

## [2026.0828.00] - 2026-08-28

### Changed

- `get_health` now judges broken wikilinks by classification instead of by count. A link whose
  target does not exist anywhere is editorial backlog and no longer prevents `healthy`; a link
  whose target exists under another title or alias is a misdirection and still reports
  `degraded`. Vaults that mark a note yet to be written with an unresolved link kept `status`
  pinned to `degraded`, which made the field unusable as an alert.

### Added

- The `integrity` payload exposes `broken_wikilinks_misdirected` next to `broken_wikilinks`, so
  a reader can tell the blocking subset from the total.
- `datacron ops inspect-id` lists every note whose identity diverges between the frontmatter, the
  ULID sidecar, and the index, with the three recorded values, the classification, the exact
  content hash to copy, and the preferred action that passes its collision and migrated-sidecar
  preflight, or the reason no action can be suggested. It changes no durable state.
- `datacron ops repair-id` repairs one such divergence under `--rel-path`, `--action`,
  `--expected-hash`, and a `--confirm` repeating the path. `adopt-index` writes the canonical ID
  into the frontmatter through the ordinary atomic, journaled write path, preserving the body
  byte for byte for uniform-EOL notes while re-serializing the frontmatter in canonical key order
  through this structured identity-repair path; `adopt-frontmatter`
  realigns the sidecar and the index instead, and is refused when the frontmatter ID is not a
  canonical 26-character Crockford ULID, so a malformed identity can never be propagated. The
  command generates no ID, accepts none by hand, reports duplicate IDs instead of guessing at
  them, refuses to adopt an ID another note already carries, and realigns the live index
  without an offline `datacron reindex`. No write tool on the
  MCP surface could reach the `id` field, which left a single divergent note pinning `get_health`
  to `degraded` with no sanctioned way out.

### Fixed

- Markdown notes with a leading UTF-8 BOM now expose their YAML frontmatter during reads and
  indexing instead of being treated as notes with empty metadata. The original BOM remains part
  of `content_hash`, and a note without frontmatter keeps its body unchanged.
- An unexpected system error now returns a stable `code` and a `correlation_id` alongside the
  opaque `internal error` message. The nine sites that flatten such failures disclose exactly as
  much about the host as before -- no `errno`, no `winerror`, no path, no `strerror` -- which is
  the arbitrated boundary and has not moved. What changed is that the payload can now be joined
  to the local log line that does carry the detail: the same `correlation_id` appears in both.
  Before, `internal error` left the caller with nothing to go on and nothing to quote.
- `datacron reindex` now refuses up front when another process holds the live index, instead of
  indexing every note and then failing at publication with a bare `PermissionError [WinError 5]`
  from `os.replace`. A running `datacron mcp serve` holds the index open for as long as it
  serves, which is knowable before the rebuild starts and costs one file handle to test; the old
  behaviour threw away minutes of work and named neither the cause nor the remedy. The
  publication path reports the same actionable message, since a server can still open the index
  while a rebuild runs.
- `datacron scrub` and `datacron scrub-init` run under the same maintenance write scope as
  `datacron ops`, so they can write their own checkpoint and canaries under `.datacron/`. The
  content write scope exists to bound agent writes to note folders and deliberately excludes
  Datacron's own state, so with a real `DATACRON_WRITE_PATHS` both commands failed with
  `PathConfinementError` before reading a single note. No bit-rot detection ran at all, while
  `get_health` kept reporting the last successful pass's `anomalies_count: 0` and its healthy
  canaries, with only `status: stale` and a frozen `index_generation` to say the measurement had
  stopped -- an absence of evidence that reads like evidence of absence unless `last_scrub` is
  checked. The
  widening now lives in one helper the three commands share, since each building it separately
  is how `scrub` came to be the one that never got it.
- The reliability scan now honours `excluded_folders` and `excluded_files` from `VAULT.yaml`,
  as the reader, the index and the MCP surface already did. It was the only component that did
  not, so it reported defects on notes nothing else treats as part of the vault -- notes
  `get_note` refuses with `note_not_admitted`. One of them pinned `status` to `degraded` even
  though admitted reads and indexing deliberately ignored it. `integrity.notes_count` now matches
  `index.notes_count` for the first time. `vault_checksum` is the deliberate exception and stays
  exhaustive: it is a byte-integrity claim over the folder, and narrowing it would silently
  change what an earlier trusted value means, so it now reports its own count and says plainly
  that it covers folders the vault excludes from what it serves.
- The write tools no longer eat a note's last byte. `parse()` strips the body it returns, so
  `set_frontmatter`, `rename_note_section`, and `delete_note_section` silently removed the
  trailing newline of every note they touched, along with the blank line after the closing
  frontmatter delimiter; a metadata-only write therefore rewrote the body and moved the note's
  `content_hash`. `append_journal` and `patch_note_section` shared the same parser and were only
  masking the loss, because the fragment they insert ends in a newline. All five now use the
  exact-body parser `patch_note_preamble` already used, which is what the published freshness
  contract promises: uniform-EOL suffix bytes stay exact. Creating an absent section through
  `append_journal` still leaves exactly one blank line before the new heading. Existing notes
  are not rewritten; the fix applies to writes from now on.

## [2026.0827.01] - 2026-08-27

### Fixed

- The PyPI trusted-publishing action now supports Core Metadata 2.5 distributions produced by
  the current build toolchain.

## [2026.0827.00] - 2026-08-27

### Fixed

- Every MCP read payload now uses the same canonical vault sandbox delimiters, including
  retrieval, search, contradiction, and resource responses.
- Production read paths no longer create persistent path-to-ULID mappings or restore
  `ulids.json` from the migrated sidecar. Explicit indexing and write workflows keep their
  existing identity-persistence contract.
- Every note lookup route now enforces the same live Markdown admission boundary. Missing,
  excluded, non-Markdown, and escaping targets fail closed with the stable
  `note_not_admitted` code instead of being returned through a stale index, sidecar, alias, or
  chunk reference.

### Dependencies

- The development extra now declares and locks `httpx`, which is required by the BL-0019
  experiment harness and its regression tests.

## [2026.0811.00] - 2026-08-11

### Added

- Three confined write tools bring the MCP surface from 14 to 17 tools, and the write surface
  from five to eight. `patch_note_preamble` replaces or removes the content placed strictly
  before the first recognized ATX heading; it always requires `expected_hash`, an empty or
  whitespace-only `new_content` removes the preamble, and a note without a recognized ATX
  heading is refused instead of being rewritten whole. `rename_note_section` changes only the
  title of an ATX H2-H6 section, without touching its level, its content, or its subtree, and
  refuses a colliding title. `delete_note_section` removes an ATX H2-H6 section and its
  subtree. All three refuse level-1 headings, are annotated as destructive, store the exact
  prior bytes in history, write atomically, and disappear from the surface under
  `DATACRON_READ_ONLY=true`.
- `patch_note_section`, `rename_note_section`, and `delete_note_section` accept a 1-based
  `heading_occurrence` selector for notes that repeat the same section title. The ordinal
  follows document order and requires both `heading_level` and the exact `expected_hash`.
  Without it an ambiguous heading is still refused rather than guessed, and the refusal
  message now names the selector to pass.
- `get_health` accepts `detail` (`summary` by default, or `full`) and `limit`. In `full` mode
  `integrity.findings` returns bounded, sanitized reliability findings in deterministic
  severity order with `total`, `returned`, `limit_applied`, and `truncated`; `limit <= 0`
  selects the server ceiling and a positive limit is capped by the configured maximum result
  count. Findings carry no line numbers, and `integrity.detail` always reports the mode used.
- `get_health` reports a `recovery` block with `required` and `blocked_operations`, plus the
  bounded blocked operations themselves in `full` mode. Any blocked operation makes the
  reported status `degraded`.
- `datacron ops inspect` lists blocked operation manifests with their reason, expected hashes,
  on-disk hash, and available repair actions, without changing durable state.
  `datacron ops repair` then repairs exactly one of them under
  `--action restore-before|adopt-disk`, the exact `--expected-disk-hash` copied from the
  inspection, and a `--confirm` repeat of the operation ID. The repair is itself journaled.
- A verified guide for driving Datacron from Ollama through an explicit MCP bridge, with the
  evidence level stated per path: `mcpo` was tested end to end against the local server (17
  routes discovered, then real `get_health`, `search_text`, and `get_note` calls), while
  `ollmcp` and Open WebUI are verified against official documentation only.

### Changed

- The stdio transport now runs on the Python MCP SDK v2. Datacron requires `mcp>=2,<3` (locked
  at 2.0.0) and can no longer be installed into an environment pinned to `mcp` 1.x. Nothing
  else changes for a client: the same server answers the final `2026-07-28` protocol revision
  and stays compatible with the legacy `2025-11-25` revision without a separate flag, no new
  environment variable or configuration entry is required, no HTTP listener is opened, and the
  supported Python range is unchanged (3.11, 3.12, 3.13).
- Push elicitation is used only on a legacy `2025-11-25` session that offers a back-channel.
  On a `2026-07-28` session `contradiction_scan` no longer prompts: it returns its normal scan
  result and leaves confirmation to a later explicit call with the proposal token.
- Structured tool errors can carry a machine-readable `code` beside `type` and `message` when
  the underlying error defines one; `recovery_required` is the first such code.
- Error mapping is unchanged on the wire and is now stated in the specification: an unknown
  tool and an absent resource return JSON-RPC `-32602`, an internal resource failure is
  sanitized to `-32603`, a validation error on a known tool stays a tool result flagged as an
  error, and a Datacron business error keeps its stable `{"error": {...}}` text payload.
- The English and French user guide, specification, architecture document, operational-health
  page, and security boundary were re-verified against the running server on 2026-08-11 and
  now state the exact tested environment.

### Fixed

- A note modified outside Datacron since its last committed operation is no longer
  overwritten. When `expected_hash` is omitted, a write now compares the current bytes with
  the last committed operation recorded for that exact path and refuses with a write conflict
  asking the caller to re-read and retry with an exact `expected_hash`. Callers that relied on
  hash-free writes must pass `expected_hash` or handle the refusal. Notes that Datacron has
  never written are unaffected.
- `patch_note_section` refuses a level-1 heading whose span contains subsections instead of
  replacing them; patch a lower-level heading instead.
- Unresolvable operation evidence now fails closed instead of raising an opaque error. Every
  mutation is refused with a `recovery_required` error until the blocked operations are
  repaired with `datacron ops repair`, history purge is held back while any operation stays
  blocked, and startup no longer aborts: tools still register and reads stay available while
  the blocked count is logged and reported by `get_health`.
- Windows installer compilation now fails closed when `dist/datacron.exe` is missing or its
  reported version differs from `AppVersion`; local and CI builds share the same validated
  wrapper, and direct ISCC compilation is rejected.
- The architecture documentation no longer claims that Cowork is remote-MCP-only; ADR-009 is
  superseded by the 2026-07-21 production validation of local stdio MCP in Cowork desktop.

### Dependencies

- `mcp` moves from `>=1.2` to `>=2,<3`, locked at 2.0.0. The resolved transitive set changes
  with it: `httpx2` and `httpcore2` replace `httpx`, `httpcore`, `httpx-sse`, and `certifi`,
  while `mcp-types` and `opentelemetry-api` are added. Datacron declares no HTTP client of its
  own and the server still opens no network connection.
- The locked `cryptography` moves to 50.0.0 so the dependency-audit gate passes again.

## [2026.0721.01] - 2026-07-21

### Added

- `datacron setup` now detects LM Studio from its real `~/.lmstudio` profile and merges the
  Datacron server into the user-only `~/.lmstudio/mcp.json`, preserving every other entry.
  The documentation includes an official `lmstudio://add_mcp` deeplink, while protocol
  installation deliberately excludes LM Studio because it has no documented global
  instruction file.

### Changed

- The installer write page and matching interactive setup prompts were reworded for
  non-technical users, explaining who may write, the three dedicated subfolders, what
  remains protected, and how to enable the permission later.
- The public specification was rewritten as v2.0 to match the current implementation.

### Removed

- The historical decision record was moved out of the public documentation; Git history
  remains the archive.

## [2026.0721.00] - 2026-07-21

### Added

- Interactive `datacron setup` now explains every prompted choice, its safe default, and
  the concrete effect of opting in before asking for an answer; `--yes` and explicitly
  supplied options remain quiet and script-compatible.
- Strictly matched English and French FAQs now cover vault-selection recovery, write-tool
  opt-in, Antigravity scopes, installed-version mismatches, index freshness, reset, silent
  installer switches, uninstall boundaries, and log diagnosis from current behavior.
- The Windows installer wizard now offers a **Write tools** page with two fail-safe
  opt-ins (unchecked by default): enable the confined write tools and apply the write
  allowlist to the user environment, mapped to `setup --enable-write` and
  `--machine-wide-write`. Silent installs get the matching `/ENABLEWRITE` and
  `/MACHINEWIDEWRITE` switches.
- `datacron setup` now detects Google Antigravity from its live profile and merges the
  Datacron server into `~/.gemini/config/mcp_config.json` and
  `<project>/.agents/mcp_config.json`, accepting an empty user config and preserving all
  other entries. Its memory protocol is supported in the workspace `GEMINI.md` only.

### Fixed

- `datacron setup --yes` no longer adopts the current directory as the vault silently:
  non-interactive runs require `--vault`, `DATACRON_VAULT_ROOT`, or an existing
  `.datacron/VAULT.yaml` in the current directory, and the user profile root is always
  refused as a vault target, even when passed explicitly.

## [2026.0720.00] - 2026-07-20

### Added

- `datacron setup` can optionally apply the write allowlist machine-wide through the user
  environment (`HKCU` registry value plus settings broadcast on Windows; printed `export`
  line on Unix), defaulting to the `_memory`, `_drafts`, and `_journal` folders, so every
  MCP client on the machine inherits it.
- Memory notes can record up to 16 structured rejected options in the optional `rejected`
  frontmatter list through `create_note_ai` and `set_frontmatter`.
- `list_notes` now accepts up to eight case-insensitive top-level frontmatter key/value
  filters with AND semantics and list-element matching.

## [2026.0719.01] - 2026-07-19

### Added

- CI now auto-publishes `server.json` to the MCP Registry via GitHub OIDC after the PyPI
  publish succeeds, with a bounded PyPI-propagation wait and an idempotent registry pre-check
  (#27).

### Changed

- The memory protocol now routes contradiction and refinement detection to the
  `contradiction_scan` tool, kept distinct from consolidation (#26).
- The memory protocol ships index-freshness guidance - trust writes returning `indexed: true`,
  use `get_health` only on suspicion, and run `datacron reindex` if the index is inconsistent -
  replacing the prior `get_health` line (#28).

### Fixed

- `contradiction_scan` statements no longer truncate mid-word and now mark truncation visibly
  (#25).
- Atomic writes retry `os.replace` on transient Windows sharing errors (WinError 5/32/33),
  fixing an intermittent concurrent-write failure on Windows (#29).

## [2026.0719.00] - 2026-07-19

### Added

- The memory protocol now covers every MCP client supported by Datacron. Windsurf receives
  an always-on block in its global rules file, and VS Code receives a dedicated user-profile
  `datacron.instructions.md` rule with `applyTo: "**"`. Claude Code, Gemini CLI, Codex,
  Cursor, and Claude Desktop keep their existing native or MCP-initialization behavior.

### Changed

- The Windows installer now installs the Datacron memory protocol after registering detected
  AI clients and removes Datacron-managed instruction blocks during uninstall.

## [2026.0718.04] - 2026-07-18

### Added

- `get_health` now reports `write_paths_configured` and `effective_writes_enabled` under
  `durability`, distinguishing the write-policy gate from whether a write can actually land.
  An effective write requires both an enabling policy and at least one configured write path.
- Release binaries now ship a `SHA256SUMS` manifest and a build-provenance attestation.

### Changed

- The `datacron://policy/active` MCP resource now reports the real write policy, including
  mode, `write_tools_enabled`, and configured write paths, instead of a static read-only
  placeholder.
- `bump_version` now updates `server.json` together with `__init__.py`, and a blocking
  invariant fails CI if the package version and `server.json` drift.
- All GitHub Actions are pinned to commit SHAs, and the PyPI publish and release workflows
  are gated on the invariant suite before building or publishing.
- The regex search fallback, used only when ripgrep is unavailable, is documented as
  best-effort with an advisory timeout and rejects a broader set of catastrophic patterns.
- History purge now runs at most once per configurable interval, off the hot write path, and
  temporal retrieval metadata is cached by index generation.
- Contradiction provenance labels are sourced from configuration instead of being hardcoded.
- CI enforces a minimum coverage floor.

### Dependencies

- `pydantic` is constrained below 3.0 to guard against the known breaking major.

## [2026.0718.03] - 2026-07-18

### Changed

- Updated the MCP registry namespace marker to match the GitHub account casing
  (`io.github.VBlackJack/datacron`).

## [2026.0718.02] - 2026-07-18

### Added

- PyPI releases can be published through a dedicated Trusted Publishing workflow with a
  separate build job and a manually approved `pypi` environment. The publish job receives
  only the short-lived OIDC permission and uses no persistent PyPI credential.
- Tested and supported on Python 3.13, which is now included in the CI matrix.

### Changed

- The PyPI project description now uses the English README and carries the MCP ownership
  marker for the future `io.github.vblackjack/datacron` registry entry.
- Distribution versions use PEP 440 normalization: Git tag `v2026.0718.01` and source
  version `2026.0718.01` map to PyPI/registry version `2026.718.1`. PEP 440 removes leading
  zeroes from numeric release segments while preserving version ordering.

## [2026.0718.01] - 2026-07-18

### Added

- Cursor project rules are now a first-class protocol target. `datacron protocol
  install` and `datacron protocol uninstall` accept `--scope user|project|both`
  and `--project PATH` (defaulting to the current directory). At project scope
  for Cursor, Datacron writes a dedicated, canonically owned
  `<project>/.cursor/rules/datacron.mdc` rule (MDC frontmatter with
  `alwaysApply: true`; body is the shared protocol block). Re-installs are
  idempotent, a `datacron.mdc` without Datacron markers is refused rather than
  overwritten, and uninstall removes only a Datacron-owned rule. `setup` installs
  the project rule at the code-project root when project scope is selected,
  without conflating it with the vault root.

## [2026.0718.00] - 2026-07-18

### Fixed

- `datacron setup --client <name>` now installs the requested non-Claude MCP client
  (Gemini CLI, Cursor, Codex CLI, Windsurf, VS Code). The setup dispatcher previously
  handled only `all`, `claude-desktop`, and `claude-code`; other client identifiers fell
  through without writing a configuration or emitting a warning. Specific clients now use
  the shared detected-client installer with an explicit include filter and keep the
  requested scope; an unknown or undetected client produces an explicit warning.
- Protocol installation no longer writes an unsupported Cursor user-global rule file
  (`~/.cursor/rules/datacron.mdc`). Cursor global rules are configured through the Cursor
  UI (Settings > Rules), so Datacron returns copyable manual instructions instead and
  safely migrates Datacron-marked blocks out of the two obsolete home paths without
  removing unrelated user content.

### Changed

- The release workflow runs on Node 24 runtimes: `actions/upload-artifact@v6`,
  `actions/download-artifact@v7`, and `softprops/action-gh-release@v3`.

## [2026.0717.03] - 2026-07-17

### Added

- `contradiction_scan` accepts a `detail` parameter (`summary` by default, or `full`).
  Summary responses drop the pre-rendered blocks from alternative mutations and shorten
  evidence excerpts; `full` preserves the previous verbose payload for debugging.
  Confirmation is unaffected: the exact write-tool call, including its block, is still
  recomputed from the proposal token.
- A per-note-pair candidate cap (`contradiction_max_per_note_pair`, default 2), applied
  after deterministic ranking and before the overall candidate limit, so a single pair of
  notes can no longer dominate scan results.
- A configurable summary evidence length (`contradiction_summary_evidence_chars`,
  default 160).

### Changed

- Scan results default to the compact `summary` payload. On a real vault this roughly
  halves the response size while preserving candidate identity and ordering.
- A suggested `CONTRADICTION` between notes with disjoint `project/` tags and no explicit
  temporal ordering is downgraded to an open question. All classification options remain
  available at confirmation, so a human can still choose contradiction.

## [2026.0717.02] - 2026-07-17

### Fixed

- `contradiction_scan` results no longer fail structured-output validation: optional
  output keys are nullable, matching how absent keys are serialized before the
  protocol-level schema check. Scan calls now return their payload on every client.
  An end-to-end regression test exercises the tool through the full validation layer.

## [2026.0717.01] - 2026-07-17

### Added

- A live contradiction scan (`contradiction_scan`, `schema_version: 2`): deterministic,
  bounded, index-driven detection of potentially conflicting sections across notes, with
  per-candidate classification (contradiction, refinement, or open question), evidence
  excerpts from both sides, and conservative defaults. The scan is strictly read-only.
- Stateless, content-addressed mutation proposals with idempotent confirmation:
  confirming a proposal returns the exact write-tool call to execute (expected hash
  included). The client performs the mutation through the existing write tools, and a
  replayed write is rejected by the compare-and-swap check.
- Typed elicitation for classification and scope on clients that support it, with a
  token-based confirmation fallback on clients that do not.

### Changed

- Ambiguous or duplicate section headings are refused as mutation targets: the candidate
  is reported as non-addressable instead of guessing which section to change.

### Removed

- The frozen cache-only contradiction advisory and its packaged replay artifacts. The
  `contradiction_scan` tool name is unchanged; its output now reports `schema_version: 2`.

### Fixed

- Logging shutdown restores log propagation, so test runs no longer depend on execution
  order.

## [2026.0717.00] - 2026-07-17

### Added

- A memory protocol for MCP clients: server instructions covering session start,
  search-first answering, and conditional proactive persistence, plus trigger-led
  descriptions on the MCP tools, with structural test coverage.
- `datacron protocol install` and `datacron protocol uninstall` manage an idempotent,
  tagged Datacron block in supported agent instruction files; clients that honor the
  server instructions natively are skipped.
- Bi-temporal note metadata: `valid_from`, `invalid_at`, and `invalidated_by`
  frontmatter keys. Notes past `invalid_at` are demoted in search results the same way
  superseded notes are. Fully backward compatible, no vault migration required.
- Evaluation harness v2: deduplicated note- and chunk-level metrics, MRR, nDCG@10,
  forbidden-path checks for knowledge-update questions, latency percentiles, measured
  payload token counts, per-stage timing, pipeline and transport selection
  (`--pipeline store|tool`, `--transport impl|e2e`), and a versioned baseline with
  `--compare` that fails on regression.
- MCP tool annotations (read-only and destructive hints), typed output schemas, and
  structured tool errors that preserve the JSON error payload.
- `--version` on the CLI and progress reporting while indexing.

### Changed

- Note creation resolves ULIDs through the index and sidecar authorities instead of
  walking the whole vault, cutting creation cost from proportional-to-vault-size to
  roughly 16 ms on an 1800-note vault. Unreadable files are skipped with a warning.
- The write tools share a common execution and error-reporting path.
- Repair-on-read is throttled (`DATACRON_REPAIR_MIN_INTERVAL_SECONDS`, default 30
  seconds; `0` restores the previous always-repair behavior). `get_health` remains
  exhaustive. The freshness contract documents this amendment.
- Personal defaults moved out of the code base; folder exclusions are read from the
  vault configuration.
- CI workflows track current major versions of the checkout and setup-python actions.

### Fixed

- Temporal re-ranking no longer merges non-comparable BM25 scores from the strict
  (AND) and fallback (OR) search passes; result tiers are preserved through
  re-ranking. Tool-pipeline recall@5 is restored to parity with the store pipeline.

## [2026.0716.00] - 2026-07-16

### Added

- A per-user Windows installer (`Datacron-Setup.exe`) with optional release signing and
  automatic shutdown of a running Datacron process before replacement.
- `datacron unregister` for removing Datacron from supported MCP client configurations.
- `datacron setup --reset` for a guarded, targeted reset of Datacron-managed state.

### Changed

- Frozen executables are registered directly in MCP client configurations instead of relying
  on a Python launcher.
- Guided setup registers MCP clients before indexing and reports an indexing failure without
  discarding a successful client registration.
- Frozen packaging uses the operating system certificate store through `truststore`.
- Installer guidance is available in English and French and linked from the main READMEs.

### Fixed

- Windows setup uses a portable reparse-point constant while retaining cross-platform mypy
  compatibility.
- Repository hygiene excludes local installer output from version control.

## [2026.0714.00] - 2026-07-14

### Added

- `datacron setup`, a guided end-to-end wizard that initializes the sidecar, builds the index,
  and wires an MCP client in one command, with location and option choices (interactive by
  default, `--yes` for unattended runs). Vault initialization is now shared through
  `datacron.bootstrap` between `init` and `setup`. Supports `--client claude-code`, which
  prints a ready-to-paste stdio MCP config snippet.
- Multi-client auto-detection and registration (`datacron setup --client all`, the default):
  detects installed AI clients - Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI,
  Windsurf, VS Code - and merges the Datacron MCP server into each config (JSON `mcpServers`,
  VS Code `servers` with `type`, or Codex TOML `mcp_servers`), at user and/or project scope
  (`--scope`), preserving existing entries. New `datacron.installers.mcp_clients` module.
- Standalone single-file executable build (PyInstaller) behind the optional `[build]` extra,
  with `scripts/build_installer.ps1` and `scripts/build_installer.sh`. Ships Datacron to users
  without Python (ADR-017, revising the PyPI/pipx-only distribution decision).
- `release` GitHub Actions workflow: on a `v*` tag it builds the standalone executable on
  Windows, macOS, and Linux, smoke-tests each binary, and attaches them to the GitHub Release.
- `scripts/bump_version.py`: computes the next CalVer (`YYYY.MMDD.XX`, UTC date + same-day
  counter) from `__init__.py`, so cutting a release never requires choosing a version number.
  `scripts/release.bat` wraps it for a one-click Windows release (bump, commit, tag, push,
  with a confirmation prompt).
- Fourteen MCP tools covering vault reads, lexical and regex search, confined writes,
  operational health, note history, operation-audit queries, and a cache-only
  `contradiction_scan` advisory (experimental, non-blocking).
- Content-addressed note history, durable operation evidence, integrity scrubbing, and
  byte-exact freshness contracts.
- A locked uv environment for the complete runtime and development dependency set.
- Import-purity regression coverage for core write-path and MCP modules.

### Changed

- CI installs frozen dependencies and validates Python 3.11 and 3.12 on Ubuntu and Windows.
- CI adds a `dependency-audit` job that scans locked dependencies for known CVEs (`pip-audit`).
- Vault encoding/line-ending defaults and the `datacron_version` stamp key are centralized in
  `core.config` (single source of truth shared by the writer and the `vault/info` resource).
- Durable-write primitives live in `datacron.core.durability`, removing the former
  `vault_writer` and `operation_log` import cycle.
- Logging configuration is explicit at CLI and MCP server entrypoints; importing Datacron
  modules no longer parses environment settings, creates log directories, or starts threads.

### Fixed

- Platform-specific durability and locking code now passes strict mypy checks on Linux and
  Windows.
- CLI logging teardown tolerates already-closed captured streams without suppressing unrelated
  errors.
