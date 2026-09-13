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

from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from datacron.core.logger import get_logger
from datacron.core.models import Note
from datacron.core.protocols import ASTChunker, FTS5Store, VaultReader
from datacron.core.vault import DuplicateNoteIdentityError

__all__ = ["IndexProgress", "ReconcileStats", "reconcile"]

_LOGGER = get_logger(__name__)

IndexProgress = Callable[[int, int], None]


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
) -> ReconcileStats:
    """Reconcile the FTS index in ``store`` with the live vault behind ``reader``.

    Args:
        store: The index to update.
        reader: The vault reader (enumeration + read).
        chunker: Produces chunks for notes that must be re-indexed.
        mtime_gate: When True, skip the read+hash of notes whose stored
            ``fs_mtime`` equals the on-disk ``st_mtime_ns``. When False, every
            note is read and compared by ``content_hash`` (full verification).
        progress: Optional callback receiving completed and total note counts.

    Returns:
        Per-pass counts. ``skipped_notes`` covers both mtime-gated skips and
        hash-matched no-ops.
    """
    indexed = await store.list_indexed_notes_with_mtime()
    live = await reader.stat_notes()
    prepared = await _prepare_live_notes(reader, live, indexed, mtime_gate=mtime_gate)

    reindexed = 0
    deleted = 0
    skipped = 0
    completed = 0
    if progress is not None:
        progress(completed, len(live))

    # Purge vanished paths before processing live notes. If a note was moved
    # while keeping a stable frontmatter id, delete_note() clears the old row by
    # note_id and the live loop below reinserts it at the new path.
    for rel_path, (note_id, _content_hash, _fs_mtime) in indexed.items():
        if rel_path not in live or (rel_path in prepared and prepared[rel_path].id != note_id):
            await store.delete_note(note_id)
            deleted += 1

    for rel_path, (_path, st_mtime_ns) in live.items():
        entry = indexed.get(rel_path)

        # Cheap path: mtime unchanged -> trust the index, do not read or hash.
        if entry is not None and mtime_gate and entry[2] is not None and entry[2] == st_mtime_ns:
            skipped += 1
            completed += 1
            if progress is not None:
                progress(completed, len(live))
            continue

        note = prepared[rel_path]

        if entry is not None and entry[0] == note.id and entry[1] == note.content_hash:
            # Content unchanged. If only the mtime moved, refresh the stored
            # mtime so the next pass can skip this note via the gate above.
            if entry[2] != st_mtime_ns:
                await store.record_mtime(note.id, st_mtime_ns)
            skipped += 1
            completed += 1
            if progress is not None:
                progress(completed, len(live))
            continue

        # New note, or content actually changed.
        await store.upsert_note(note, chunker.chunk(note), fs_mtime_ns=st_mtime_ns)
        reindexed += 1
        completed += 1
        if progress is not None:
            progress(completed, len(live))

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


async def _prepare_live_notes(
    reader: VaultReader,
    live: dict[str, tuple[Path, int]],
    indexed: dict[str, tuple[str, str, int | None]],
    *,
    mtime_gate: bool,
) -> dict[str, Note]:
    """Validate projected identities before deleting or replacing any index rows.

    Unchanged notes retain the existing mtime fast path. Changed notes are read
    once and reused by the commit loop, including when two paths exchange IDs.
    """
    prepared: dict[str, Note] = {}
    owners: dict[str, str] = {}
    for rel_path, (path, mtime) in live.items():
        entry = indexed.get(rel_path)
        if entry is not None and mtime_gate and entry[2] is not None and entry[2] == mtime:
            note_id = entry[0]
        else:
            note = await reader.read_note(path)
            prepared[rel_path] = note
            note_id = note.id
        if note_id in owners:
            raise DuplicateNoteIdentityError(note_id, owners[note_id], rel_path)
        owners[note_id] = rel_path
    return prepared
