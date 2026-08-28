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
"""Offline full-index rebuild with validation and one atomic publication swap."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, TypedDict
from uuid import uuid4

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

from datacron.core.config import Settings, VaultConfig
from datacron.core.durability import WritePolicy, probe_directory_durability
from datacron.core.logger import get_logger
from datacron.core.paths import sidecar_index_db
from datacron.core.vault import build_configured_reader
from datacron.core.vault_writer import durable_flush_directory
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import IndexProgress, reconcile

__all__ = [
    "REBUILD_FAULT_POINTS",
    "IndexRebuildError",
    "RebuildStats",
    "rebuild_index_atomic",
]

_LOGGER = get_logger(__name__)
# ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION only. ERROR_ACCESS_DENIED (5) is
# deliberately absent: it is what MoveFileEx reports for a denied ACL or a read-only
# attribute, neither of which is another process holding the file, and treating it as
# one refuses rebuilds that would have succeeded.
_WINDOWS_SHARING_ERRORS: Final[frozenset[int]] = frozenset({32, 33})
_INDEX_HELD_MESSAGE: Final[str] = (
    "the live index at {db_path} is held open by another process, so it cannot be "
    "replaced. A running `datacron mcp serve` holds it open for as long as it serves. "
    "Stop every MCP client and server on this vault, then run the rebuild again. "
    "Nothing was published and the existing index is untouched."
)

FaultInjector = Callable[[str], None]
REBUILD_FAULT_POINTS: Final[tuple[str, ...]] = (
    "after_temp_build",
    "after_validation",
    "before_swap",
    "after_swap",
)


class IndexRebuildError(RuntimeError):
    """Raised when an offline rebuild cannot be validated or safely published."""


class RebuildStats(TypedDict):
    """Published rebuild evidence."""

    checked_notes: int
    reindexed_notes: int
    chunk_count: int
    generation: int
    db_path: str


async def rebuild_index_atomic(
    vault_root: Path,
    settings: Settings,
    vault_config: VaultConfig,
    *,
    fault_injector: FaultInjector | None = None,
    progress: IndexProgress | None = None,
) -> RebuildStats:
    """Build a complete temp index, validate it, then atomically replace live DB."""
    root = vault_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"vault root does not exist: {root}")
    durability = probe_directory_durability(root)
    WritePolicy(settings, durability).ensure_writable()

    db_path = sidecar_index_db(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_sqlite_sidecars(db_path)
    _assert_index_replaceable(db_path)
    previous_generation = await _read_generation(db_path)
    temp_path = db_path.with_name(f".{db_path.name}.{uuid4().hex}.rebuild")
    temp_store = SQLiteFTS5Store(term_map=vault_config.query_expansion)
    reader = build_configured_reader(root, read_only=True)
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    published = False

    try:
        await temp_store.open(temp_path, sidecar_writeback=False)
        await temp_store.set_generation(previous_generation)
        reconcile_stats = await reconcile(
            temp_store,
            reader,
            chunker,
            mtime_gate=False,
            progress=progress,
        )
        stats = await temp_store.stats()
        indexed = await temp_store.list_indexed_notes()
        live_notes = await reader.list_notes()
        live = {note.rel_path: (note.id, note.content_hash) for note in live_notes}
        if indexed != live:
            divergent = sorted(
                rel_path
                for rel_path in indexed.keys() | live.keys()
                if indexed.get(rel_path) != live.get(rel_path)
            )
            raise IndexRebuildError(
                f"temp index differs from live byte-exact notes at {len(divergent)} paths"
            )
        expected_generation = previous_generation + 1
        if stats.generation != expected_generation:
            raise IndexRebuildError(
                "temp index generation mismatch: "
                f"expected {expected_generation}, found {stats.generation}"
            )
        if stats.note_count != len(live):
            raise IndexRebuildError(
                f"temp index note count mismatch: {stats.note_count} != {len(live)}"
            )
        await temp_store.close()
        await asyncio.to_thread(_validate_sqlite_integrity, temp_path)
        await asyncio.to_thread(_fsync_file, temp_path)
        _inject(fault_injector, "after_temp_build")
        _inject(fault_injector, "after_validation")
        _assert_no_sqlite_sidecars(db_path)
        _inject(fault_injector, "before_swap")
        _publish_index(temp_path, db_path)
        published = True
        _inject(fault_injector, "after_swap")
        durable_flush_directory(db_path.parent)
        _LOGGER.info(
            "Atomic index rebuild published (vault=%s notes=%d chunks=%d generation=%d)",
            root,
            stats.note_count,
            stats.chunk_count,
            stats.generation,
        )
        return {
            "checked_notes": reconcile_stats["checked_notes"],
            "reindexed_notes": reconcile_stats["reindexed_notes"],
            "chunk_count": stats.chunk_count,
            "generation": stats.generation,
            "db_path": str(db_path),
        }
    finally:
        await temp_store.close()
        if not published:
            _cleanup_sqlite_family(temp_path)


async def _read_generation(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    store = SQLiteFTS5Store()
    await store.open(db_path, read_only=True)
    try:
        return await store.get_generation()
    finally:
        await store.close()


def _publish_index(temp_path: Path, db_path: Path) -> None:
    """Swap the validated rebuild over the live index, or say why it could not.

    The up-front probe cannot close the race: a server may open the index while the
    rebuild runs. Reporting the same actionable message here beats surfacing a bare
    `WinError 5` from a call site the reader has to go and look up.
    """
    try:
        os.replace(temp_path, db_path)
    except PermissionError as exc:
        if sys.platform != "win32" or exc.winerror not in _WINDOWS_SHARING_ERRORS:
            # POSIX replaces open files, and a denied ACL is not a held file. Naming a
            # cause the platform cannot have would send the operator hunting servers
            # that are not the problem.
            raise
        raise IndexRebuildError(_INDEX_HELD_MESSAGE.format(db_path=db_path)) from exc


def _assert_index_replaceable(db_path: Path) -> None:
    """Refuse before the rebuild when the live index cannot be replaced.

    Windows refuses to replace a file another process holds open, and a running
    `datacron mcp serve` holds the index open for as long as it serves. The check
    costs one handle here; discovering it at publication costs the entire rebuild,
    which is minutes of work on a real vault and reports a bare `WinError 5` from a
    call site the reader has to go and look up.

    POSIX replaces an open file happily, so there is nothing to detect there.

    The probe asks for exactly the access ``os.replace`` needs and no more.
    ``MoveFileEx`` requires ``DELETE`` on the target, not write access, so asking for
    ``GENERIC_WRITE`` would refuse a rebuild that a denied write ACL cannot actually
    stop -- a refusal with no way past it, since ``reindex`` has no ``--force``.
    """
    if not db_path.exists():
        return
    if sys.platform == "win32":
        _assert_windows_index_replaceable(db_path)


if sys.platform == "win32":

    def _assert_windows_index_replaceable(db_path: Path) -> None:
        """Refuse only when another process is genuinely holding the index open."""
        delete_and_read = 0x00010000 | 0x80000000  # DELETE | GENERIC_READ
        open_existing = 3
        invalid_handle = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateFileW(
            str(db_path),
            delete_and_read,
            0,  # no sharing: this is exactly what os.replace needs and cannot get
            None,
            open_existing,
            0,
            None,
        )
        if handle != invalid_handle:
            kernel32.CloseHandle(handle)
            return
        if ctypes.get_last_error() in _WINDOWS_SHARING_ERRORS:
            raise IndexRebuildError(_INDEX_HELD_MESSAGE.format(db_path=db_path))
        # Any other reason to fail an exclusive open is not this defect -- a denied ACL
        # is not a held file. Let the rebuild proceed and report its own error.

else:

    def _assert_windows_index_replaceable(db_path: Path) -> None:
        """POSIX replaces an open file happily, so there is nothing to detect."""


def _assert_no_sqlite_sidecars(db_path: Path) -> None:
    active = [path for path in _sqlite_sidecars(db_path) if path.exists()]
    if active:
        rendered = ", ".join(path.name for path in active)
        raise IndexRebuildError(
            f"atomic reindex requires an offline index without WAL/SHM sidecars; found {rendered}"
        )


def _sqlite_sidecars(db_path: Path) -> tuple[Path, Path]:
    return (
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    )


def _cleanup_sqlite_family(db_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    for sidecar in (*_sqlite_sidecars(db_path), db_path.with_name(f"{db_path.name}-journal")):
        sidecar.unlink(missing_ok=True)


def _validate_sqlite_integrity(db_path: Path) -> None:
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("PRAGMA integrity_check;").fetchall()
    finally:
        connection.close()
    if rows != [("ok",)]:
        raise IndexRebuildError(f"temp index integrity_check failed: {rows!r}")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as file_handle:
        file_handle.flush()
        os.fsync(file_handle.fileno())


def _inject(fault_injector: FaultInjector | None, point: str) -> None:
    if fault_injector is not None:
        fault_injector(point)
