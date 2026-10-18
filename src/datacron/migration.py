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
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from datacron.bootstrap import initialize_vault
from datacron.core.config import (
    MEMORY_SUBFOLDER_NAME,
    SIDECAR_DIR_NAME,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.durability import atomic_durable_write
from datacron.core.paths import PathConfinementError, is_link_stat, sidecar_vault_config
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
    "MigrationError",
    "MigrationResult",
    "SymlinkDetectedError",
    "migrate_memory_to_dedicated_vault",
    "migrate_vault_to_dual",
]

_EXCLUDED_SIDECAR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".lock", ".db", ".db-wal", ".db-shm", ".rebuild"}
)


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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _should_skip_sidecar_file(rel_path: Path) -> bool:
    if rel_path.parts and rel_path.parts[0] == "index":
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
        raise SymlinkDetectedError(f"Refusing to migrate symlink or reparse point: {src_dir}")
    if not stat.S_ISDIR(dir_stat.st_mode):
        return []

    pairs: list[tuple[Path, Path, str]] = []
    for root_str, dirnames, filenames in os.walk(src_dir, followlinks=False):
        root_path = Path(root_str)
        dirnames.sort()
        for dirname in dirnames:
            dir_entry = root_path / dirname
            if is_link_stat(os.lstat(dir_entry)):
                raise SymlinkDetectedError(
                    f"Refusing to migrate symlink or reparse point: {dir_entry}"
                )
        for filename in sorted(filenames):
            file_entry = root_path / filename
            file_stat = os.lstat(file_entry)
            if is_link_stat(file_stat):
                raise SymlinkDetectedError(
                    f"Refusing to migrate symlink or reparse point: {file_entry}"
                )
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


def _copy_and_verify_pairs(pairs: list[tuple[Path, Path, str]]) -> int:
    for src_file, dst_file, rel_label in pairs:
        if dst_file.exists() and (
            not dst_file.is_file() or _sha256_file(src_file) != _sha256_file(dst_file)
        ):
            raise MigrationError(t("cli.migrate_memory.target_conflict", rel_path=rel_label))
    verified = 0
    for src_file, dst_file, rel_label in pairs:
        raw_bytes = src_file.read_bytes()
        expected_digest = hashlib.sha256(raw_bytes).hexdigest()
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_durable_write(dst_file, raw_bytes)
        if _sha256_file(dst_file) != expected_digest:
            raise MigrationError(t("cli.migrate_memory.hash_mismatch", rel_path=rel_label))
        verified += 1
    return verified


async def migrate_vault_to_dual(
    vault_root: Path,
    memory_root: Path,
    *,
    keep_source: bool = False,
    client: str = CLIENT_NONE,
    install_scope: str = INSTALL_SCOPE_BOTH,
    enable_write: bool = True,
    settings: Settings | None = None,
) -> MigrationResult:
    """Atomically copy `.datacron/` and `_memory/` to `memory_root`, verify SHA-256, and reindex."""
    resolved_vault, resolved_memory = _validate_migration_roots(vault_root, memory_root)
    sidecar_pairs, memory_pairs = _collect_migration_pairs(resolved_vault, resolved_memory)

    resolved_memory.mkdir(parents=True, exist_ok=True)
    (resolved_memory / MEMORY_SUBFOLDER_NAME).mkdir(parents=True, exist_ok=True)

    all_pairs = [*sidecar_pairs, *memory_pairs]
    verified_files = _copy_and_verify_pairs(all_pairs)

    initialize_vault(resolved_memory, force=False)

    base = settings or get_settings()
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

    if not keep_source:
        # Remove .datacron/ before _memory/ so that an open SQLite handle on Windows
        # fails before any user memory notes are deleted from the source vault.
        src_sidecar = resolved_vault / SIDECAR_DIR_NAME
        if src_sidecar.exists():
            shutil.rmtree(src_sidecar)
        src_memory = resolved_vault / MEMORY_SUBFOLDER_NAME
        if src_memory.exists():
            shutil.rmtree(src_memory)

    client_installs: list[InstallOutcome] = []
    client_errors: list[str] = []
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

    return MigrationResult(
        vault_path=resolved_vault,
        memory_vault_path=resolved_memory,
        copied_sidecar_files=len(sidecar_pairs),
        copied_memory_files=len(memory_pairs),
        verified_files=verified_files,
        indexed_notes=int(rebuilt["reindexed_notes"]),
        indexed_chunks=int(rebuilt["chunk_count"]),
        source_cleaned=not keep_source,
        client_installs=client_installs,
        client_errors=client_errors,
    )


migrate_memory_to_dedicated_vault = migrate_vault_to_dual
