# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Incremental index reconciliation shared by the CLI and the MCP read-repair.

A single algorithm keeps the FTS index in sync with the live vault:

* notes whose filesystem mtime is unchanged are skipped (no read, no hash) when
  ``mtime_gate`` is enabled -- this is what turns a full re-hash of the vault on
  every search into a near-instant ``stat`` sweep;
* ``content_hash`` stays the authority: a note is only re-chunked when its hash
  differs, never on an mtime change alone;
* a note whose mtime moved but whose content is unchanged has its stored mtime
  refreshed via :meth:`record_mtime`, so the next pass can skip it (otherwise a
  touched-but-unchanged note would be re-read on every search forever);
* notes present in the index but absent from disk are deleted.

Both :func:`datacron.cli._run_index` and the MCP ``_repair_index_on_read``
delegate here so the two paths cannot drift.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from datacron.core.logger import get_logger
from datacron.core.protocols import ASTChunker, FTS5Store, VaultReader
from datacron.core.vault import DuplicateNoteIdentityError

__all__ = ["IndexProgress", "ReconcileStats", "reconcile"]

_LOGGER = get_logger(__name__)

IndexProgress = Callable[[int, int], "Awaitable[None] | None"]
"""Completed and total note counts.

A callback may be a coroutine function: the CLI reports progress by printing,
while the MCP apply reports it as a protocol notification, which has to be
awaited. Returning an awaitable is awaited before the sweep continues, so a
notification cannot pile up behind the work it describes.
"""


class ReconcileStats(TypedDict):
    """Outcome counts for a single reconcile pass."""

    checked_notes: int
    indexed_notes_before: int
    reindexed_notes: int
    deleted_notes: int
    skipped_notes: int


async def reconcile(
    store: FTS5Store,
    reader: VaultReader,
    chunker: ASTChunker,
    *,
    mtime_gate: bool,
    progress: IndexProgress | None = None,
    live: dict[str, tuple[Path, int]] | None = None,
) -> ReconcileStats:
    """Reconcile the FTS index in ``store`` with the live vault behind ``reader``.

    Args:
        store: The index to update.
        reader: The vault reader (enumeration + read).
        chunker: Produces chunks for notes that must be re-indexed.
        mtime_gate: When True, skip the read+hash of notes whose stored
            ``fs_mtime`` equals the on-disk ``st_mtime_ns``. When False, every
            note is read and compared by ``content_hash`` (full verification).
        progress: Optional callback receiving completed and total note counts. A note
            counts once its index state is settled, which for an unchanged note
            happens during the identity pre-pass.
        live: The walk this pass reconciles against, when the caller has already
            performed it. ``None`` walks here. The read repair passes its own, so
            one sweep never walks the vault twice.

    Returns:
        Per-pass counts. ``skipped_notes`` covers both mtime-gated skips and
        hash-matched no-ops.
    """
    indexed = await store.list_indexed_notes_with_mtime()
    if live is None:
        live = await reader.stat_notes()
    gated = frozenset(
        rel_path
        for rel_path, (_path, st_mtime_ns) in live.items()
        if _gate_holds(indexed.get(rel_path), st_mtime_ns, mtime_gate=mtime_gate)
    )
    # A note advances the counter once, when its index state is settled: during the
    # pre-pass when its content is unchanged, at commit when it is new or changed.
    total = len(live)
    completed = 0

    async def advance() -> None:
        nonlocal completed
        completed += 1
        await _report(progress, completed, total)

    await _report(progress, completed, total)
    # One pass is one scope for each of the two costs it used to pay per note.
    # Resolving the identity of a note with no frontmatter id rewrote the whole
    # sidecar, and every indexed note committed its own SQLite transaction, so both
    # the bytes written and the durable commits grew with the vault on every pass.
    # The identity scope covers the reads of both loops; the commit scope covers the
    # writes of the second.
    counts = _PassCounts()
    try:
        async with reader.defer_identity_writes():
            prepared, owners, unreadable, undecodable = await _prepare_live_identities(
                reader, live, indexed, gated=gated, on_settled=advance
            )
            async with store.bulk_writes():
                await _apply_pass(
                    store,
                    reader,
                    chunker,
                    # An undecodable note is treated as gone, so its rows are purged.
                    live={
                        rel_path: entry
                        for rel_path, entry in live.items()
                        if rel_path not in undecodable
                    },
                    indexed=indexed,
                    prepared=prepared,
                    gated=gated,
                    unreadable=unreadable,
                    owners=owners,
                    advance=advance,
                    counts=counts,
                )
    except Exception:
        # Every delete and every upsert commits as it goes, so a pass that fails
        # part-way has already published rows. The generation is what tells the
        # temporal cache its answer is stale, and that cache lives as long as the
        # server process: leaving it unmoved kept a note ranked from its pre-edit
        # metadata for the rest of the run, with no error on any later search. A
        # refusal that published nothing, such as a duplicate identity, leaves the
        # counter alone, which is why the pass reports what it wrote as it writes.
        if counts.published:
            await _advance_generation_after_failure(store)
        raise
    deleted, reindexed, skipped = counts.deleted, counts.reindexed, counts.skipped

    if unreadable:
        _LOGGER.warning(
            "reconcile skipped %d unreadable note(s); each one was logged with its path above",
            len(unreadable),
        )
    if undecodable:
        _LOGGER.warning(
            "reconcile dropped %d undecodable note(s) from the index; "
            "each one was logged with its path above",
            len(undecodable),
        )

    if reindexed or deleted:
        await store.increment_generation()

    stats = ReconcileStats(
        checked_notes=len(live),
        indexed_notes_before=len(indexed),
        reindexed_notes=reindexed,
        deleted_notes=deleted,
        skipped_notes=skipped,
    )
    _LOGGER.info(
        "reconcile complete (checked=%d reindexed=%d skipped=%d deleted=%d mtime_gate=%s)",
        stats["checked_notes"],
        reindexed,
        skipped,
        deleted,
        mtime_gate,
    )
    return stats


@dataclass
class _PassCounts:
    """What one pass has published so far.

    Owned by the caller rather than returned, because the question it answers is
    only interesting when the pass did not finish: every delete and every upsert
    commits as it goes, so a pass that raises has already changed rows, and the
    generation has to move for exactly those rows and not for a refusal that
    changed nothing.
    """

    deleted: int = 0
    reindexed: int = 0
    skipped: int = 0

    @property
    def published(self) -> bool:
        return bool(self.deleted or self.reindexed)


async def _advance_generation_after_failure(store: FTS5Store) -> None:
    """Move the generation after a failed pass, without masking why it failed."""
    try:
        await store.increment_generation()
    except Exception as exc:
        _LOGGER.error("Could not advance the index generation after a failed pass: %s", exc)


async def _apply_pass(
    store: FTS5Store,
    reader: VaultReader,
    chunker: ASTChunker,
    *,
    live: dict[str, tuple[Path, int]],
    indexed: dict[str, tuple[str, str, int | None]],
    prepared: dict[str, tuple[str, str]],
    gated: frozenset[str],
    unreadable: frozenset[str],
    owners: dict[str, str],
    advance: Callable[[], Awaitable[None]],
    counts: _PassCounts,
) -> None:
    """Apply one settled pass to the index, recording what it publishes as it goes."""
    counts.deleted = await _purge_vanished_notes(store, indexed, live, prepared)

    for rel_path, (path, st_mtime_ns) in live.items():
        entry = indexed.get(rel_path)

        # Cheap path: mtime unchanged -> trust the index, do not read or hash.
        if rel_path in gated:
            counts.skipped += 1
            await advance()
            continue

        if rel_path in unreadable:
            # Already reported as settled by the pre-pass; its index rows, if any,
            # are left as they were rather than dropped on a transient read error.
            counts.skipped += 1
            continue

        note_id, content_hash = prepared[rel_path]

        if entry is not None and entry[0] == note_id and entry[1] == content_hash:
            # Content unchanged. If only the mtime moved, refresh the stored
            # mtime so the next pass can skip this note via the gate above.
            if entry[2] != st_mtime_ns:
                await store.record_mtime(note_id, st_mtime_ns)
            counts.skipped += 1  # Already reported as settled by the pre-pass.
            continue

        # New note, or content actually changed: read it again now. The pre-pass kept
        # only its identity and hash so the whole vault is never held in memory.
        try:
            note = await reader.read_note(path)
        except (OSError, ValueError) as exc:
            _LOGGER.warning("Skipping unreadable note %s: %s", path, exc)
            counts.skipped += 1
            await advance()
            continue
        if note.id != note_id and owners.get(note.id, rel_path) != rel_path:
            raise DuplicateNoteIdentityError(note.id, owners[note.id], rel_path)
        owners[note.id] = rel_path
        await store.upsert_note(note, chunker.chunk(note), fs_mtime_ns=st_mtime_ns)
        counts.reindexed += 1
        await advance()


async def _report(progress: IndexProgress | None, completed: int, total: int) -> None:
    """Deliver one progress report, awaiting it when the callback is a coroutine."""
    if progress is None:
        return
    outcome = progress(completed, total)
    if inspect.isawaitable(outcome):
        await outcome


async def _purge_vanished_notes(
    store: FTS5Store,
    indexed: dict[str, tuple[str, str, int | None]],
    live: dict[str, tuple[Path, int]],
    prepared: dict[str, tuple[str, str]],
) -> int:
    """Drop index rows for paths that are gone, before any live note is processed.

    A note moved while keeping a stable frontmatter id has its old row cleared by
    ``note_id`` here, and the live loop reinserts it at the new path. A note that
    is still on disk but could not be read is absent from ``prepared`` and present
    in ``live``, so its rows are deliberately left alone rather than dropped on a
    read error.
    """
    deleted = 0
    for rel_path, (note_id, _content_hash, _fs_mtime) in indexed.items():
        if rel_path not in live or (rel_path in prepared and prepared[rel_path][0] != note_id):
            await store.delete_note(note_id)
            deleted += 1
    return deleted


def _gate_holds(
    entry: tuple[str, str, int | None] | None, st_mtime_ns: int, *, mtime_gate: bool
) -> bool:
    """True when the stored mtime lets the pass trust the index without reading the note."""
    return entry is not None and mtime_gate and entry[2] is not None and entry[2] == st_mtime_ns


async def _prepare_live_identities(
    reader: VaultReader,
    live: dict[str, tuple[Path, int]],
    indexed: dict[str, tuple[str, str, int | None]],
    *,
    gated: frozenset[str],
    on_settled: Callable[[], Awaitable[None]],
) -> tuple[dict[str, tuple[str, str]], dict[str, str], frozenset[str], frozenset[str]]:
    """Validate projected identities before deleting or replacing any index rows.

    Gated notes keep the existing mtime fast path and are never read. Every other
    note is read once here, but only its ``(note_id, content_hash)`` pair is kept:
    the commit loop reads a changed note again instead of holding every ``Note`` of
    a large vault at once. ``on_settled`` reports each note whose content matches the
    index, so the progress callback advances during a long pre-pass.
    Returns the pairs by path, the owner path of every live identity, the notes
    that could not be read, and the notes whose bytes could not be decoded.

    The last two are kept apart because they call for opposite index states. A
    read error is transient (a Windows share lock, an antivirus scan), so the
    rows of the last good read are kept. A decoding error is a property of the
    bytes on disk, and keeping the rows kept serving content the file no longer
    holds: every later list or search then re-read that note for redaction or
    paging, hit the same error and failed as a whole, and no pass ever healed it.
    """
    prepared: dict[str, tuple[str, str]] = {}
    owners: dict[str, str] = {}
    unreadable: set[str] = set()
    undecodable: set[str] = set()
    for rel_path, (path, _mtime) in live.items():
        if rel_path in gated:
            note_id = indexed[rel_path][0]
        else:
            try:
                note = await reader.read_note(path)
            except OSError as exc:
                _LOGGER.warning("Skipping unreadable note %s: %s", path, exc)
                unreadable.add(rel_path)
                await on_settled()
                continue
            except ValueError as exc:
                _LOGGER.warning("Dropping undecodable note %s from the index: %s", path, exc)
                undecodable.add(rel_path)
                await on_settled()
                continue
            prepared[rel_path] = (note.id, note.content_hash)
            note_id = note.id
            entry = indexed.get(rel_path)
            settled = entry is not None and entry[:2] == (note.id, note.content_hash)
            del note
            if settled:
                await on_settled()
        if note_id in owners:
            raise DuplicateNoteIdentityError(note_id, owners[note_id], rel_path)
        owners[note_id] = rel_path
    return prepared, owners, frozenset(unreadable), frozenset(undecodable)
