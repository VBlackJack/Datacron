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
"""Atomic single-vault to Dual-Vault migration (`datacron migrate-memory`)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from datacron.bootstrap import initialize_vault
from datacron.core.config import (
    COMPANION_STATE_DIR_NAME,
    HISTORY_DIR_NAME,
    INDEX_DIR_NAME,
    MEMORY_SUBFOLDER_NAME,
    OPLOG_DIR_NAME,
    SIDECAR_DIR_NAME,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.durability import UnexpectedRecoveryEntryError, atomic_durable_write
from datacron.core.operation_log import OperationJournal
from datacron.core.paths import PathConfinementError, is_link_stat, sidecar_vault_config
from datacron.core.vault import ULID_SIDECAR_FILENAME
from datacron.core.vault_writer import FilesystemVaultWriter, VaultLockBusyError
from datacron.i18n import t
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.setup_wizard import (
    CLIENT_NONE,
    INSTALL_SCOPE_BOTH,
    SetupPlan,
    default_write_paths,
    run_setup,
)

if TYPE_CHECKING:
    from datacron.installers.mcp_clients import InstallOutcome

__all__ = [
    "COPY_MANIFEST_FILENAME",
    "MigrationError",
    "MigrationResult",
    "SymlinkDetectedError",
    "migrate_memory_to_dedicated_vault",
    "migrate_vault_to_dual",
]

_EXCLUDED_SIDECAR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".lock", ".db", ".db-wal", ".db-shm", ".rebuild"}
)
# Sidecar entries that belong to this machine's process state, never to the migrated vault.
_LOCKS_DIR_NAME: Final[str] = "locks"
_INDEX_DIR_NAME: Final[str] = INDEX_DIR_NAME
_LOGS_DIR_NAME: Final[str] = "logs"
_SCRUBBER_DIR_NAME: Final[str] = "scrubber"
# Suffix JsonIdStore gives the identity file it merges after a vault migration.
_MIGRATED_SUFFIX: Final[str] = ".migrated"
_EXCLUDED_SIDECAR_DIRS: Final[frozenset[str]] = frozenset({_INDEX_DIR_NAME, _LOCKS_DIR_NAME})
# The writer-wide lock every note transaction takes first (see FilesystemVaultWriter).
_MUTATION_LOCK_KEY: Final[str] = "mutation"
# Kept in the destination sidecar: the SHA-256 of every file a previous run wrote, so a
# retry can tell its own earlier copy from a file somebody else put there.
COPY_MANIFEST_FILENAME: Final[str] = "migration-manifest.json"
_COPY_MANIFEST_VERSION: Final[int] = 1
# Source entries are renamed under this prefix inside the source sidecar before deletion.
_TOMBSTONE_PREFIX: Final[str] = "migration-tombstone-"
# Sidecar entries whose source copy legitimately diverges from the memory vault once the
# clients write there: a cleanup-only run does not compare them.
_DIVERGING_SIDECAR_ENTRIES: Final[frozenset[str]] = frozenset(
    {
        OPLOG_DIR_NAME,
        HISTORY_DIR_NAME,
        COMPANION_STATE_DIR_NAME,
        INDEX_DIR_NAME,
        _LOCKS_DIR_NAME,
        _LOGS_DIR_NAME,
        _SCRUBBER_DIR_NAME,
        ULID_SIDECAR_FILENAME,
        f"{ULID_SIDECAR_FILENAME}{_MIGRATED_SUFFIX}",
    }
)
# Error and warning messages list at most this many paths.
_MAX_LISTED_PATHS: Final[int] = 10


class MigrationError(ValueError):
    """Raised when a Dual-Vault migration preflight or verification check fails."""


class SymlinkDetectedError(MigrationError, PathConfinementError):
    """Raised when a symlink or Windows reparse point is encountered during migration."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Verified outcome of splitting a single vault into Docs + Memory vaults."""

    vault_path: Path
    memory_vault_path: Path
    copied_sidecar_files: int
    copied_memory_files: int
    verified_files: int
    indexed_notes: int
    indexed_chunks: int
    source_cleaned: bool
    client_installs: list[InstallOutcome] = field(default_factory=list)
    client_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resumed_files: int = 0


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _should_skip_sidecar_file(rel_path: Path) -> bool:
    if rel_path.parts and rel_path.parts[0] in _EXCLUDED_SIDECAR_DIRS:
        return True
    if rel_path.parts and rel_path.parts[0].startswith(_TOMBSTONE_PREFIX):
        return True
    if rel_path.as_posix() == COPY_MANIFEST_FILENAME:
        return True
    return any(rel_path.name.endswith(suffix) for suffix in _EXCLUDED_SIDECAR_SUFFIXES)


def _validate_migration_roots(vault_root: Path, memory_root: Path) -> tuple[Path, Path]:
    resolved_vault = vault_root.expanduser().resolve()
    resolved_memory = memory_root.expanduser().resolve()
    home_root = Path.home().expanduser().resolve()
    if home_root in {resolved_vault, resolved_memory}:
        raise MigrationError(t("cli.migrate_memory.error.unsafe_home_root"))
    if not resolved_vault.is_dir():
        raise NotADirectoryError(
            t("cli.migrate_memory.error.vault_not_found", path=str(resolved_vault))
        )
    if resolved_vault == resolved_memory:
        raise MigrationError(t("cli.migrate_memory.same_root", path=resolved_vault))
    if resolved_memory.is_relative_to(resolved_vault) or resolved_vault.is_relative_to(
        resolved_memory
    ):
        raise MigrationError(
            t(
                "cli.migrate_memory.overlapping_roots",
                vault=resolved_vault,
                memory_vault=resolved_memory,
            )
        )
    has_sidecar = (resolved_vault / SIDECAR_DIR_NAME / "VAULT.yaml").exists()
    has_memory = (resolved_vault / MEMORY_SUBFOLDER_NAME).exists()
    if not has_sidecar and not has_memory:
        raise MigrationError(t("cli.migrate_memory.missing_source", vault=resolved_vault))
    return resolved_vault, resolved_memory


def _collect_tree_pairs(
    src_dir: Path,
    dst_dir: Path,
    prefix_name: str,
    *,
    skip_sidecar_excludes: bool,
) -> list[tuple[Path, Path, str]]:
    try:
        dir_stat = os.lstat(src_dir)
    except FileNotFoundError:
        return []
    if is_link_stat(dir_stat):
        raise SymlinkDetectedError(t("migration.error.symlink", path=src_dir))
    if not stat.S_ISDIR(dir_stat.st_mode):
        return []

    pairs: list[tuple[Path, Path, str]] = []
    for root_str, dirnames, filenames in os.walk(src_dir, followlinks=False):
        root_path = Path(root_str)
        dirnames.sort()
        for dirname in dirnames:
            dir_entry = root_path / dirname
            if is_link_stat(os.lstat(dir_entry)):
                raise SymlinkDetectedError(t("migration.error.symlink", path=dir_entry))
        for filename in sorted(filenames):
            file_entry = root_path / filename
            file_stat = os.lstat(file_entry)
            if is_link_stat(file_stat):
                raise SymlinkDetectedError(t("migration.error.symlink", path=file_entry))
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            rel_in_dir = file_entry.relative_to(src_dir)
            if skip_sidecar_excludes and _should_skip_sidecar_file(rel_in_dir):
                continue
            rel_label = (Path(prefix_name) / rel_in_dir).as_posix()
            dst_file = dst_dir / rel_in_dir
            pairs.append((file_entry, dst_file, rel_label))
    return pairs


def _collect_migration_pairs(
    resolved_vault: Path,
    resolved_memory: Path,
) -> tuple[list[tuple[Path, Path, str]], list[tuple[Path, Path, str]]]:
    sidecar_pairs = _collect_tree_pairs(
        resolved_vault / SIDECAR_DIR_NAME,
        resolved_memory / SIDECAR_DIR_NAME,
        SIDECAR_DIR_NAME,
        skip_sidecar_excludes=True,
    )
    memory_pairs = _collect_tree_pairs(
        resolved_vault / MEMORY_SUBFOLDER_NAME,
        resolved_memory / MEMORY_SUBFOLDER_NAME,
        MEMORY_SUBFOLDER_NAME,
        skip_sidecar_excludes=False,
    )
    return sidecar_pairs, memory_pairs


def _copy_manifest_path(resolved_memory: Path) -> Path:
    return resolved_memory / SIDECAR_DIR_NAME / COPY_MANIFEST_FILENAME


def _read_copy_manifest(resolved_memory: Path) -> dict[str, str]:
    """Return the per-file hashes a previous run wrote, or nothing when unreadable."""
    manifest_path = _copy_manifest_path(resolved_memory)
    try:
        decoded = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict) or decoded.get("version") != _COPY_MANIFEST_VERSION:
        return {}
    files = decoded.get("files")
    if not isinstance(files, dict):
        return {}
    return {str(key): str(value) for key, value in files.items() if isinstance(value, str)}


def _write_copy_manifest(resolved_memory: Path, files: dict[str, str]) -> None:
    manifest_path = _copy_manifest_path(resolved_memory)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _COPY_MANIFEST_VERSION, "files": dict(sorted(files.items()))}
    atomic_durable_write(manifest_path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def _copy_and_verify_pairs(
    pairs: list[tuple[Path, Path, str]],
    resolved_memory: Path,
) -> tuple[int, int]:
    """Copy every pair and verify it, resuming over a previous run's unchanged copy.

    A destination file is overwritable only when it already holds the source bytes or
    the exact bytes a previous run recorded in the copy manifest. Anything else was
    written by somebody else and stops the migration before a single byte is copied.
    The manifest is written before the copy, so a run interrupted mid-copy can resume.
    """
    previous = _read_copy_manifest(resolved_memory)
    planned: dict[str, str] = dict(previous)
    resumed = 0
    for src_file, dst_file, rel_label in pairs:
        source_digest = _sha256_file(src_file)
        planned[rel_label] = source_digest
        if not dst_file.exists():
            continue
        if not dst_file.is_file():
            raise MigrationError(t("cli.migrate_memory.target_conflict", rel_path=rel_label))
        dest_digest = _sha256_file(dst_file)
        if dest_digest == source_digest:
            resumed += 1
            continue
        if previous.get(rel_label) != dest_digest:
            raise MigrationError(t("cli.migrate_memory.target_conflict", rel_path=rel_label))
    if pairs:
        _write_copy_manifest(resolved_memory, planned)
    verified = 0
    for src_file, dst_file, rel_label in pairs:
        raw_bytes = src_file.read_bytes()
        expected_digest = hashlib.sha256(raw_bytes).hexdigest()
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if not dst_file.is_file() or _sha256_file(dst_file) != expected_digest:
            atomic_durable_write(dst_file, raw_bytes)
        if _sha256_file(dst_file) != expected_digest:
            raise MigrationError(t("cli.migrate_memory.hash_mismatch", rel_path=rel_label))
        verified += 1
    return verified, resumed


def _record_destination_hashes(
    pairs: list[tuple[Path, Path, str]],
    resolved_memory: Path,
) -> None:
    """Record the destination bytes as left by this run (initialization and rebuild included)."""
    recorded = _read_copy_manifest(resolved_memory)
    for _src_file, dst_file, rel_label in pairs:
        if dst_file.is_file():
            recorded[rel_label] = _sha256_file(dst_file)
    if pairs:
        _write_copy_manifest(resolved_memory, recorded)


def _source_settings(base: Settings, resolved_vault: Path) -> Settings:
    return base.model_copy(
        update={
            "vault_root": resolved_vault,
            "memory_root": None,
            "read_paths": [resolved_vault],
            "write_paths": [resolved_vault],
            "read_only": False,
        }
    )


def _load_source_vault_config(resolved_vault: Path, settings: Settings) -> VaultConfig:
    try:
        return load_vault_config(sidecar_vault_config(resolved_vault, settings=settings)) or (
            VaultConfig()
        )
    except (ValueError, TypeError, OSError):
        return VaultConfig()


async def _refuse_pending_operations(
    writer: FilesystemVaultWriter,
    resolved_vault: Path,
    vault_cfg: VaultConfig,
) -> None:
    journal = OperationJournal(
        resolved_vault,
        retention_days=vault_cfg.history_retention_days,
        history_mode=vault_cfg.history_mode,
    )
    try:
        pending_paths, stray = journal.scan_pending()
        pending_batches = await writer.has_pending_organization_batches()
    except UnexpectedRecoveryEntryError as exc:
        raise MigrationError(
            t("migration.error.pending_operations", vault=resolved_vault, count=len(exc.entries))
        ) from exc
    count = len(pending_paths) + len(stray) + (1 if pending_batches else 0)
    if count:
        raise MigrationError(
            t("migration.error.pending_operations", vault=resolved_vault, count=count)
        )


@asynccontextmanager
async def _source_write_lock(resolved_vault: Path, base: Settings) -> AsyncIterator[None]:
    """Hold the source vault's writer-wide mutation lock for the whole migration window.

    Every note transaction of a single-vault server takes this lock first, so no write
    can land in the source between the copy, its verification and the cleanup. A source
    with pending operation-journal entries is refused: those entries would be copied as
    pending into the memory vault and replayed against a layout they were not written for.
    """
    settings = _source_settings(base, resolved_vault)
    vault_cfg = _load_source_vault_config(resolved_vault, settings)
    writer = FilesystemVaultWriter(resolved_vault, settings, vault_cfg)
    with ExitStack() as stack:
        try:
            # The writer exposes no public lock for maintenance spanning many files;
            # this is the exact lock its own transactions take first.
            stack.enter_context(writer._advisory_lock(_MUTATION_LOCK_KEY))
        except VaultLockBusyError as exc:
            raise MigrationError(t("migration.error.lock_busy", vault=resolved_vault)) from exc
        await _refuse_pending_operations(writer, resolved_vault, vault_cfg)
        yield


def _quarantine_source(resolved_vault: Path) -> Path:
    """Move the source sidecar content and `_memory/` into one tombstone, or move nothing.

    Each move is a same-volume rename, so it either happens whole or not at all. The
    index directory goes first: an index database held open by a running server makes
    that rename fail before anything else has moved. On any failure every completed move
    is undone, so the source is left exactly as it was; a move that cannot be undone is
    named in the error, with where its content now lives.
    """
    src_sidecar = resolved_vault / SIDECAR_DIR_NAME
    src_sidecar.mkdir(parents=True, exist_ok=True)
    tombstone = src_sidecar / f"{_TOMBSTONE_PREFIX}{uuid4().hex}"
    tombstone.mkdir()
    entries = sorted(
        (
            entry
            for entry in src_sidecar.iterdir()
            if entry.name not in {_LOCKS_DIR_NAME, tombstone.name}
        ),
        key=lambda entry: (entry.name != _INDEX_DIR_NAME, entry.name),
    )
    src_memory = resolved_vault / MEMORY_SUBFOLDER_NAME
    moves = [(entry, tombstone / entry.name) for entry in entries]
    if os.path.lexists(src_memory):
        moves.append((src_memory, tombstone / MEMORY_SUBFOLDER_NAME))
    completed: list[tuple[Path, Path]] = []
    for original, target in moves:
        try:
            os.replace(original, target)
        except OSError as exc:
            stranded = _undo_moves(completed)
            if stranded:
                raise MigrationError(
                    t(
                        "migration.error.cleanup_rollback_incomplete",
                        path=original,
                        detail=exc,
                        stranded="; ".join(stranded),
                    )
                ) from exc
            with suppress(OSError):
                tombstone.rmdir()
            raise MigrationError(
                t("migration.error.cleanup_blocked", path=original, detail=exc)
            ) from exc
        completed.append((original, target))
    return tombstone


def _undo_moves(completed: list[tuple[Path, Path]]) -> list[str]:
    """Undo every completed move, newest first; return ``original <- current`` for failures."""
    stranded: list[str] = []
    for done_original, done_target in reversed(completed):
        try:
            os.replace(done_target, done_original)
        except OSError:
            stranded.append(f"{done_original} <- {done_target}")
    return stranded


def _delete_tree(root: Path) -> list[str]:
    """Delete ``root`` bottom-up, clearing read-only bits; return the paths left behind."""
    left: list[str] = []

    def _remove(path: Path, *, directory: bool) -> None:
        try:
            if directory:
                path.rmdir()
            else:
                path.unlink()
        except PermissionError:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                if directory:
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                left.append(str(path))
        except OSError:
            left.append(str(path))

    for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in filenames:
            _remove(current_path / filename, directory=False)
        for dirname in dirnames:
            child = current_path / dirname
            if os.path.lexists(child):
                _remove(child, directory=not os.path.islink(child))
    if os.path.lexists(root):
        _remove(root, directory=True)
    return left


def _remove_lock_leftovers(resolved_vault: Path) -> None:
    """Remove the emptied source sidecar once the lock is released, if nothing else remains."""
    src_sidecar = resolved_vault / SIDECAR_DIR_NAME
    locks_dir = src_sidecar / _LOCKS_DIR_NAME
    try:
        if locks_dir.is_dir():
            for lock_file in locks_dir.iterdir():
                lock_file.unlink()
            locks_dir.rmdir()
        src_sidecar.rmdir()
    except OSError:
        return


def _clean_source(resolved_vault: Path, warnings: list[str]) -> bool:
    """Quarantine then delete the source; return whether the source is fully gone.

    A refusal to move (the source is intact) or an incomplete deletion is reported as a
    warning: by then the copy, the reindex and the client reconfiguration have succeeded,
    and failing the whole call would hide that.
    """
    try:
        tombstone = _quarantine_source(resolved_vault)
    except MigrationError as exc:
        warnings.append(str(exc))
        return False
    left = _delete_tree(tombstone)
    if left:
        warnings.append(
            t(
                "migration.warning.tombstone_left",
                path=tombstone,
                detail="; ".join(left[:_MAX_LISTED_PATHS]),
            )
        )
        return False
    return True


def _verify_cleanup_matches_destination(resolved_vault: Path, resolved_memory: Path) -> None:
    """Refuse a cleanup-only run unless deleting the source loses nothing.

    Every source `_memory/` file must be byte-identical to its destination copy, or still
    hold the exact bytes a previous run copied (the destination copy may have been edited
    since the switch). Sidecar state that legitimately diverges after the switch (journal,
    history, index, identities, logs) is ignored; any other sidecar file must have been
    copied at least once.
    """
    manifest = _read_copy_manifest(resolved_memory)
    if not manifest or not (resolved_memory / SIDECAR_DIR_NAME / "VAULT.yaml").is_file():
        raise MigrationError(
            t("migration.error.cleanup_without_copy", memory_vault=resolved_memory)
        )
    sidecar_pairs, memory_pairs = _collect_migration_pairs(resolved_vault, resolved_memory)
    differing: list[str] = []
    for src_file, dst_file, rel_label in memory_pairs:
        source_digest = _sha256_file(src_file)
        if dst_file.is_file() and _sha256_file(dst_file) == source_digest:
            continue
        if manifest.get(rel_label) == source_digest:
            continue
        differing.append(rel_label)
    for _src_file, dst_file, rel_label in sidecar_pairs:
        if rel_label.split("/")[1] in _DIVERGING_SIDECAR_ENTRIES:
            continue
        if not dst_file.is_file() and rel_label not in manifest:
            differing.append(rel_label)
    if differing:
        shown = ", ".join(differing[:_MAX_LISTED_PATHS])
        raise MigrationError(
            t("migration.error.cleanup_source_differs", count=len(differing), paths=shown)
        )


async def _cleanup_source_only(
    resolved_vault: Path,
    resolved_memory: Path,
    base: Settings,
) -> MigrationResult:
    warnings: list[str] = []
    async with _source_write_lock(resolved_vault, base):
        _verify_cleanup_matches_destination(resolved_vault, resolved_memory)
        tombstone = _quarantine_source(resolved_vault)
        left = _delete_tree(tombstone)
        if left:
            warnings.append(
                t(
                    "migration.warning.tombstone_left",
                    path=tombstone,
                    detail="; ".join(left[:_MAX_LISTED_PATHS]),
                )
            )
    if not left:
        _remove_lock_leftovers(resolved_vault)
    return MigrationResult(
        vault_path=resolved_vault,
        memory_vault_path=resolved_memory,
        copied_sidecar_files=0,
        copied_memory_files=0,
        verified_files=0,
        indexed_notes=0,
        indexed_chunks=0,
        source_cleaned=not left,
        warnings=warnings,
    )


async def migrate_vault_to_dual(
    vault_root: Path,
    memory_root: Path,
    *,
    keep_source: bool = False,
    client: str = CLIENT_NONE,
    install_scope: str = INSTALL_SCOPE_BOTH,
    enable_write: bool = True,
    settings: Settings | None = None,
    cleanup_only: bool = False,
) -> MigrationResult:
    """Copy `.datacron/` and `_memory/` to `memory_root`, verify SHA-256, reindex, then clean.

    The whole copy, verification, reindex, client reconfiguration and cleanup run under
    the source vault's write lock. Cleanup moves the source entries into a tombstone
    first and deletes only that tombstone, so it never stops halfway through the user's
    notes. A run that stopped earlier can be rerun: files already copied are verified
    and kept, and files a previous run wrote are refreshed from the source.

    ``cleanup_only`` finishes a migration whose source was kept: nothing is copied,
    reindexed or reconfigured; the source is removed once every source `_memory/` file is
    proven to be in the memory vault. It works after the memory vault has been written
    to, when a full rerun would refuse the diverged sidecar.
    """
    resolved_vault, resolved_memory = _validate_migration_roots(vault_root, memory_root)
    base = settings or get_settings()
    if cleanup_only:
        return await _cleanup_source_only(resolved_vault, resolved_memory, base)
    warnings: list[str] = []
    client_installs: list[InstallOutcome] = []
    client_errors: list[str] = []
    source_cleaned = False

    async with _source_write_lock(resolved_vault, base):
        sidecar_pairs, memory_pairs = _collect_migration_pairs(resolved_vault, resolved_memory)

        resolved_memory.mkdir(parents=True, exist_ok=True)
        (resolved_memory / MEMORY_SUBFOLDER_NAME).mkdir(parents=True, exist_ok=True)

        all_pairs = [*sidecar_pairs, *memory_pairs]
        verified_files, resumed_files = _copy_and_verify_pairs(all_pairs, resolved_memory)

        initialize_vault(resolved_memory, force=False)

        write_roots = default_write_paths(resolved_vault, resolved_memory)
        dual_settings = base.model_copy(
            update={
                "vault_root": resolved_vault,
                "memory_root": resolved_memory,
                "read_paths": [resolved_vault, resolved_memory],
                "write_paths": [resolved_vault, resolved_memory],
                "read_only": False,
            }
        )
        vault_cfg = (
            load_vault_config(sidecar_vault_config(resolved_vault, settings=dual_settings))
            or VaultConfig()
        )
        rebuilt = await rebuild_index_atomic(resolved_vault, dual_settings, vault_cfg)
        _record_destination_hashes(all_pairs, resolved_memory)

        # Clients are pointed at the new layout before the source disappears, so a
        # failed reconfiguration never leaves a client bound to a cleaned vault.
        if client != CLIENT_NONE:
            setup_res = await run_setup(
                SetupPlan(
                    vault_path=resolved_vault,
                    memory_vault_path=resolved_memory,
                    build_index=False,
                    enable_write=enable_write,
                    write_paths=write_roots if enable_write else [],
                    client=client,
                    install_scope=install_scope,
                )
            )
            client_installs = list(setup_res.client_installs)
            client_errors = list(setup_res.client_errors)

        if not keep_source and client_errors:
            warnings.append(t("migration.warning.cleanup_skipped_client_errors"))
        elif not keep_source:
            source_cleaned = _clean_source(resolved_vault, warnings)

    if source_cleaned:
        _remove_lock_leftovers(resolved_vault)

    return MigrationResult(
        vault_path=resolved_vault,
        memory_vault_path=resolved_memory,
        copied_sidecar_files=len(sidecar_pairs),
        copied_memory_files=len(memory_pairs),
        verified_files=verified_files,
        indexed_notes=int(rebuilt["reindexed_notes"]),
        indexed_chunks=int(rebuilt["chunk_count"]),
        source_cleaned=source_cleaned,
        client_installs=client_installs,
        client_errors=client_errors,
        warnings=warnings,
        resumed_files=resumed_files,
    )


migrate_memory_to_dedicated_vault = migrate_vault_to_dual
