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
"""Vault reader implementation (``core.VaultReader``).

The reader walks a Markdown vault, parses frontmatter via
:mod:`datacron.core.frontmatter`, computes canonical content hashes via
:mod:`datacron.core.hashing`, and assigns ULID identifiers without ever
mutating the user's notes -- IDs are stored in a JSON sidecar at
``.datacron/ulids.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, final

from datacron.core.config import (
    SIDECAR_DIR_NAME,
    VAULT_CONFIG_FILENAME,
    VaultConfig,
    load_vault_config,
)
from datacron.core.durability import read_bytes_with_windows_retry
from datacron.core.frontmatter import (
    FrontmatterError,
    build_tiered_alias_index,
    coerce_string_list,
    extract_tags,
    parse,
    resolve_note_title,
)
from datacron.core.hashing import NOTE_ID_LENGTH, derive_note_id, sha256_bytes
from datacron.core.logger import get_logger
from datacron.core.models import Note
from datacron.core.paths import read_ulid_mappings

__all__ = [
    "H1_PATTERN",
    "FilesystemVaultReader",
    "JsonIdStore",
    "NoteAdmissionPolicy",
    "build_configured_reader",
]

_LOGGER = get_logger(__name__)

MARKDOWN_GLOB: Final[str] = "*.md"
_MARKDOWN_SUFFIX: Final[str] = ".md"
SKIPPED_FOLDERS: Final[frozenset[str]] = frozenset(
    {SIDECAR_DIR_NAME, ".git", ".obsidian", ".hg", ".svn", "node_modules"}
)
ULID_SIDECAR_FILENAME: Final[str] = "ulids.json"
MIGRATED_ULID_SIDECAR_FILENAME: Final[str] = "ulids.json.migrated"
# The first-level ATX heading a note title falls back to when its frontmatter has no
# title: the same rule for the reader and for every other module that derives a title.
H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)
_H1_PATTERN: Final[re.Pattern[str]] = H1_PATTERN


class DuplicateNoteIdentityError(ValueError):
    """Multiple paths claim one note identity; refuse index replacement."""

    code = "duplicate_note_identity"

    def __init__(self, note_id: str, first: str, second: str) -> None:
        paths = sorted((first, second))
        super().__init__(f"note ID {note_id} is claimed by {paths[0]!r} and {paths[1]!r}")


@final
@dataclass(frozen=True)
class NoteAdmissionPolicy:
    """Immutable effective exclusions shared by the vault reader and scope."""

    excluded_folders: frozenset[str]
    excluded_files: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "excluded_folders",
            frozenset(item.casefold() for item in self.excluded_folders),
        )
        object.__setattr__(
            self,
            "excluded_files",
            frozenset(item.casefold() for item in self.excluded_files),
        )


def build_configured_reader(
    vault_root: Path,
    *,
    id_store: JsonIdStore | None = None,
    read_only: bool = False,
    admission_policy: NoteAdmissionPolicy | None = None,
) -> FilesystemVaultReader:
    """Build a reader honoring vault exclusions from ``.datacron/VAULT.yaml``."""
    resolved_root = vault_root.expanduser().resolve()
    effective_policy = admission_policy
    if effective_policy is None:
        config_path = resolved_root / SIDECAR_DIR_NAME / VAULT_CONFIG_FILENAME
        config = load_vault_config(config_path) or VaultConfig()
        effective_policy = NoteAdmissionPolicy(
            excluded_folders=SKIPPED_FOLDERS | frozenset(config.excluded_folders),
            excluded_files=frozenset(config.excluded_files),
        )
    return FilesystemVaultReader(
        resolved_root,
        id_store=id_store,
        read_only=read_only,
        admission_policy=effective_policy,
    )


def _normalize_rel_path(path: Path, vault_root: Path) -> str:
    """Return ``path`` relative to ``vault_root`` with POSIX separators."""
    rel = path.resolve().relative_to(vault_root.resolve())
    return str(PurePosixPath(*rel.parts))


def _walked_rel_path(path: Path, resolved_vault_root: Path) -> str:
    """Return the vault-relative POSIX path of a file the vault walk produced.

    The walk starts at an already-resolved root, so the answer is arithmetic on
    strings. :func:`_normalize_rel_path` resolves both sides instead, which is two
    filesystem round trips for something already known. Its callers here run once
    per note in the vault, so that pair dominated a sweep that otherwise only stats.
    """
    return str(PurePosixPath(*path.relative_to(resolved_vault_root).parts))


def _coerce_datetime(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


@final
class JsonIdStore:
    """JSON-backed mapping from vault-relative paths to ULIDs.

    The store is lazy: it reads ``ulids.json`` on first access and persists after
    every mutation, or once per pass inside :meth:`deferred_writes`. The FTS5
    ``ulid_paths`` table becomes the source of truth after indexing; the JSON file
    remains as a fallback bootstrap.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._path = path
        self._read_only = read_only
        self._cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        # True while a pass is staging identities to be written once at its end.
        self._deferring = False
        self._deferred_dirty = False

    @property
    def path(self) -> Path:
        return self._path

    def _load_sync(self) -> dict[str, str]:
        primary_exists = self._path.exists()
        try:
            primary = read_ulid_mappings(self._path) if primary_exists else {}
        except OSError as exc:
            _LOGGER.error("Failed to read ULID sidecar %s: %s", self._path, exc)
            raise

        migrated_path = self._path.with_name(MIGRATED_ULID_SIDECAR_FILENAME)
        if not migrated_path.exists():
            return primary

        try:
            migrated = read_ulid_mappings(migrated_path)
        except OSError as exc:
            _LOGGER.error("Failed to read migrated ULID sidecar %s: %s", migrated_path, exc)
            raise
        if not migrated:
            return primary

        merged = dict(primary)
        merged.update(migrated)
        if merged != primary:
            if self._read_only:
                _LOGGER.info(
                    "Using migrated ULID mappings without repairing read-only sidecar %s",
                    self._path,
                )
            else:
                self._write_sync(merged)
                _LOGGER.info(
                    "Repaired ULID sidecar %s from %s (%s mappings)",
                    self._path,
                    migrated_path,
                    len(merged),
                )
        return merged

    def _write_sync(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(serialized + "\n", encoding="utf-8")
        os.replace(tmp_path, self._path)

    async def _ensure_loaded(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = await asyncio.to_thread(self._load_sync)
        return self._cache

    async def get(self, rel_path: str) -> str | None:
        async with self._lock:
            cache = await self._ensure_loaded()
            return cache.get(rel_path)

    async def set(self, rel_path: str, note_id: str) -> None:
        if self._read_only:
            raise PermissionError("read-only ULID store refuses mutation")
        async with self._lock:
            cache = await self._ensure_loaded()
            cache[rel_path] = note_id
            if self._deferring:
                self._deferred_dirty = True
                return
            await asyncio.to_thread(self._write_sync, dict(cache))

    @asynccontextmanager
    async def deferred_writes(self) -> AsyncIterator[None]:
        """Stage identity writes in memory and persist them once, on exit.

        Every write reserialized the whole mapping and replaced the file, so a pass
        that resolves N new identities wrote the file N times and its total bytes grew
        with the square of the vault. That is the shipped cold index: nothing else
        resolves identities in bulk.

        Deferring loses nothing on a crash. The identity of a note with no frontmatter
        id is a pure function of its vault-relative path, so a pass that dies before
        the flush recomputes exactly the same identities on the next run. The flush
        also runs when the body raises, for the same reason it is safe to skip: the
        file is a cache of a derivation, and leaving it closer to the truth is never
        worse. A flush that fails there is logged rather than allowed to replace the
        error that is already on its way out.
        """
        if self._read_only:
            # A read-only store never writes, so there is nothing to defer. The scope
            # stays usable because the pass that opens it does not know, and must not
            # have to know, whether its reader can persist identities.
            yield
            return
        if self._deferring:
            raise RuntimeError("identity writes are already deferred")
        self._deferring = True
        self._deferred_dirty = False
        try:
            yield
        except BaseException:
            with suppress(OSError):
                await self._flush_deferred()
            raise
        else:
            await self._flush_deferred()
        finally:
            self._deferring = False
            self._deferred_dirty = False

    async def _flush_deferred(self) -> None:
        """Write the staged mapping once, if anything was staged."""
        async with self._lock:
            if not self._deferred_dirty or self._cache is None:
                return
            snapshot = dict(self._cache)
            self._deferred_dirty = False
        await asyncio.to_thread(self._write_sync, snapshot)

    async def invalidate_cache(self) -> None:
        """Drop the lazy snapshot after an out-of-band sidecar mutation."""
        async with self._lock:
            self._cache = None

    async def snapshot(self) -> dict[str, str]:
        async with self._lock:
            cache = await self._ensure_loaded()
            return dict(cache)


@dataclass(frozen=True)
class _AliasRecord:
    """What one note contributes to the alias index, and nothing more.

    The alias tiers read an identity, a title, a filename stem and a list of
    aliases. Carrying a whole :class:`Note` instead meant reading, hashing and
    parsing every note in the vault to build them.
    """

    note_id: str
    title: str
    rel_path: str
    aliases: list[str]
    fingerprint: tuple[int, int]
    """The note file's ``(st_mtime_ns, st_size)`` when this record was read."""


@final
class FilesystemVaultReader:
    """Filesystem-backed implementation of the :class:`VaultReader` protocol.

    A reader is bound to a single ``vault_root`` at construction (contracts
    section 2.6 amendment fe5dbc6). All methods operate on that bound root; there
    is no per-call override.

    The concrete class deliberately differs in name from the Protocol so
    that consumers can write ``reader: VaultReader = FilesystemVaultReader(...)``
    without a self-shadowing import. Structural conformance is checked at the
    bottom of this module.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        id_store: JsonIdStore | None = None,
        read_only: bool = False,
        excluded_folders: frozenset[str] | None = None,
        excluded_files: frozenset[str] | None = None,
        admission_policy: NoteAdmissionPolicy | None = None,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        sidecar = self._vault_root / SIDECAR_DIR_NAME / ULID_SIDECAR_FILENAME
        self._read_only = read_only
        self._id_store = id_store or JsonIdStore(sidecar, read_only=read_only)
        if admission_policy is not None and (
            excluded_folders is not None or excluded_files is not None
        ):
            raise ValueError(
                "admission_policy cannot be combined with excluded_folders or excluded_files"
            )
        self._admission_policy = admission_policy or NoteAdmissionPolicy(
            excluded_folders=SKIPPED_FOLDERS | frozenset(excluded_folders or ()),
            excluded_files=frozenset(excluded_files or ()),
        )
        self._alias_cache: dict[str, str | None] | None = None
        self._path_link_cache: dict[str, str | None] = {}
        # Survives an alias-cache invalidation on purpose: dropping the resolved index
        # is how a write is noticed, and re-reading the notes that did not move is
        # what made noticing it cost the whole vault.
        self._alias_records: dict[str, _AliasRecord] = {}
        self._alias_lock = asyncio.Lock()

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    @property
    def id_store(self) -> JsonIdStore:
        return self._id_store

    @property
    def admission_policy(self) -> NoteAdmissionPolicy:
        """Return the immutable note policy used by filesystem discovery."""
        return self._admission_policy

    # ------------------------------------------------------------------ read

    def defer_identity_writes(self) -> AbstractAsyncContextManager[None]:
        """Stage the identities resolved in this scope and persist them once at its end.

        Resolving an identity rewrote the whole sidecar, so a pass over a vault of
        id-less notes wrote it once per note. See :meth:`JsonIdStore.deferred_writes`
        for why deferring cannot lose anything.
        """
        return self._id_store.deferred_writes()

    async def read_note(self, path: Path) -> Note:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Note not found: {resolved}")
        if not self._is_inside_vault(resolved):
            raise ValueError(f"Path {resolved} is outside the vault root {self._vault_root}.")

        raw_bytes = await asyncio.to_thread(read_bytes_with_windows_retry, resolved)
        raw_text = raw_bytes.decode("utf-8", errors="strict")
        stat = await asyncio.to_thread(resolved.stat)
        metadata: dict[str, Any]
        body: str
        try:
            metadata, body = parse(raw_text)
        except FrontmatterError as exc:
            _LOGGER.warning(
                "Invalid YAML frontmatter in %s; reading note with empty metadata: %s",
                resolved,
                exc,
            )
            metadata = {}
            body = raw_text

        # ``resolved`` is a realpath under the resolved root, so its relative spelling
        # is arithmetic; resolving both sides again cost two realpaths per read.
        rel_path = _walked_rel_path(resolved, self._vault_root)
        note_id = await self._resolve_id(metadata, rel_path)
        title = resolve_note_title(
            metadata,
            body,
            resolved,
            h1_pattern=_H1_PATTERN,
            empty_h1_falls_back=True,
        )
        tags = extract_tags(metadata, body)
        aliases = coerce_string_list(metadata.get("aliases"), keep_empty_scalar=True)

        fs_ctime = datetime.fromtimestamp(stat.st_ctime, tz=UTC)
        fs_mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        created = _coerce_datetime(metadata.get("created"), fs_ctime)
        updated = _coerce_datetime(metadata.get("updated"), fs_mtime)

        return Note(
            id=note_id,
            path=resolved,
            rel_path=rel_path,
            title=title,
            frontmatter=metadata,
            content=body,
            raw_content=raw_text,
            created=created,
            updated=updated,
            content_hash=sha256_bytes(raw_bytes),
            tags=tags,
            aliases=aliases,
        )

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        root = self._scope_root(folder)
        if not root.exists():
            return []
        paths = await asyncio.to_thread(self._collect_markdown_paths, root)
        if limit is not None:
            paths = paths[:limit]
        notes: list[Note] = []
        for path in paths:
            try:
                notes.append(await self.read_note(path))
            except (OSError, ValueError) as exc:
                _LOGGER.warning("Skipping unreadable note %s: %s", path, exc)
        return notes

    async def note_paths(self) -> dict[str, Path]:
        """Return ``rel_path -> absolute_path`` for every live note, without stat()."""
        return await asyncio.to_thread(self._walk_markdown_paths)

    def _walk_markdown_paths(self) -> dict[str, Path]:
        return {
            _walked_rel_path(path, self._vault_root): path
            for path in self._collect_markdown_paths(self._vault_root)
        }

    async def stat_notes(self) -> dict[str, tuple[Path, int]]:
        """Return ``rel_path -> (absolute_path, st_mtime_ns)`` for every live note.

        Mirrors :meth:`list_notes` enumeration (same exclusions) but only
        ``stat()``s each file. The read-repair uses this to skip the read+hash
        of notes whose filesystem mtime is unchanged.
        """
        return await asyncio.to_thread(self._stat_markdown_paths)

    def _stat_markdown_paths(self) -> dict[str, tuple[Path, int]]:
        result: dict[str, tuple[Path, int]] = {}
        for path in self._collect_markdown_paths(self._vault_root):
            rel_path = _walked_rel_path(path, self._vault_root)
            result[rel_path] = (path, path.stat().st_mtime_ns)
        return result

    async def resolve_alias(self, alias: str) -> str | None:
        normalized = alias.strip().lower()
        if not normalized:
            return None
        index = await self._build_alias_index()
        if normalized in index:
            return index[normalized]
        # A wikilink may name its target by vault path or with the .md suffix, which
        # Obsidian writes whenever two notes share a stem and the documentation
        # recommends. Those forms reached no tier, so the note vanished from its own
        # backlinks with truncated=False. They are tried only after every tier has
        # missed, so the title -> stem -> alias precedence is unchanged.
        without_suffix = normalized.removesuffix(_MARKDOWN_SUFFIX)
        if without_suffix in index:
            return index[without_suffix]
        return self._path_link_cache.get(without_suffix.lstrip("/"))

    async def invalidate_alias_cache(self) -> None:
        async with self._alias_lock:
            self._alias_cache = None
            await self._id_store.invalidate_cache()

    # -------------------------------------------------------------- internals

    def _is_inside_vault(self, path: Path) -> bool:
        try:
            path.relative_to(self._vault_root)
        except ValueError:
            return False
        return True

    def _scope_root(self, folder: str | None) -> Path:
        if folder is None:
            return self._vault_root
        target = (self._vault_root / folder).resolve()
        if not self._is_inside_vault(target):
            raise ValueError(f"Folder {folder!r} escapes vault root {self._vault_root}.")
        return target

    def _collect_markdown_paths(self, root: Path) -> list[Path]:
        """Walk the vault in ``os.walk`` order, skipping symlinked files and folders.

        A symlinked note used to be keyed by its link path and its target path at
        once, and two keys for one identity made reconcile raise
        DuplicateNoteIdentityError, which failed every index-backed tool for the
        whole vault. ``os.walk`` already never descends into a linked folder; the
        linked file is skipped the same way, and the note is still reached under
        its real path. ``os.scandir`` gives the link bit with the entry, so this
        costs no system call per note.
        """
        results: list[Path] = []
        self._collect_markdown_paths_into(root, results)
        return results

    def _collect_markdown_paths_into(self, directory: Path, results: list[Path]) -> None:
        try:
            with os.scandir(directory) as scanned:
                entries = sorted(scanned, key=lambda entry: entry.name)
        except OSError:
            return
        subdirectories: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_directory:
                if not self._should_skip_dir(entry.name):
                    subdirectories.append(Path(entry.path))
                continue
            is_markdown = entry.name.lower().endswith(_MARKDOWN_SUFFIX)
            if is_markdown and not self._should_skip_file(entry.name):
                results.append(Path(entry.path))
        for subdirectory in subdirectories:
            self._collect_markdown_paths_into(subdirectory, results)

    def _should_skip_dir(self, name: str) -> bool:
        return name.casefold() in self._admission_policy.excluded_folders or name.startswith(".")

    def _should_skip_file(self, name: str) -> bool:
        return name.casefold() in self._admission_policy.excluded_files

    async def _resolve_id(self, metadata: dict[str, object], rel_path: str) -> str:
        front_id = metadata.get("id")
        if isinstance(front_id, str) and len(front_id) == NOTE_ID_LENGTH:
            return front_id
        existing = await self._id_store.get(rel_path)
        if existing:
            return existing
        new_id = derive_note_id(rel_path)
        if self._read_only:
            return new_id
        await self._id_store.set(rel_path, new_id)
        return new_id

    async def _read_alias_record(self, path: Path, fingerprint: tuple[int, int]) -> _AliasRecord:
        """Read only the four values the alias tiers are built from.

        The index is rebuilt whenever a write invalidates it, which in the normal
        loop of writing a note and then asking for its backlinks means once per
        write. Reading every note in full to get four fields made that rebuild cost
        the whole vault: the hash, the tag extraction, the timestamps and the note
        object itself are all discarded immediately.

        Identity, title and aliases are resolved by the same helpers
        :meth:`read_note` uses, so the two cannot answer differently. What is skipped
        is only what the tiers never look at.
        """
        raw_bytes = await asyncio.to_thread(read_bytes_with_windows_retry, path)
        raw_text = raw_bytes.decode("utf-8", errors="strict")
        try:
            metadata, body = parse(raw_text)
        except FrontmatterError as exc:
            _LOGGER.warning(
                "Invalid YAML frontmatter in %s; reading note with empty metadata: %s",
                path,
                exc,
            )
            metadata, body = {}, raw_text
        rel_path = _walked_rel_path(path, self._vault_root)
        return _AliasRecord(
            note_id=await self._resolve_id(metadata, rel_path),
            title=resolve_note_title(
                metadata,
                body,
                path,
                h1_pattern=_H1_PATTERN,
                empty_h1_falls_back=True,
            ),
            rel_path=rel_path,
            aliases=coerce_string_list(metadata.get("aliases"), keep_empty_scalar=True),
            fingerprint=fingerprint,
        )

    async def _collect_alias_records(self) -> list[_AliasRecord]:
        """Return one record per note, re-reading only the notes that moved.

        The resolved index is dropped after every write, so this runs once per write
        in the loop of writing a note and then asking for its backlinks. Re-reading
        the whole vault each time made that loop cost the whole vault; a note whose
        file has not moved cannot have changed the four values a record holds.

        The gate is the note's ``(st_mtime_ns, st_size)``, which is the gate
        ``reconcile`` already applies to decide whether to re-read a note, so this
        trusts nothing the index does not trust already. Unlike ``reconcile`` there is
        no stored hash to fall back on, so an in-place edit that leaves both the
        nanosecond mtime and the size untouched is not seen here until something else
        drops the records. Writes through Datacron do not rely on that: they update
        the mtime.
        """
        paths = await asyncio.to_thread(self._collect_markdown_paths, self._vault_root)
        fingerprints = await asyncio.to_thread(self._fingerprint_notes, paths)
        reused = self._alias_records
        records: dict[str, _AliasRecord] = {}
        for path, fingerprint in fingerprints.items():
            rel_path = _walked_rel_path(path, self._vault_root)
            known = reused.get(rel_path)
            if known is not None and known.fingerprint == fingerprint:
                records[rel_path] = known
                continue
            try:
                records[rel_path] = await self._read_alias_record(path, fingerprint)
            except (OSError, ValueError) as exc:
                _LOGGER.warning("Alias index: skipping %s: %s", path, exc)
        self._alias_records = records
        return list(records.values())

    @staticmethod
    def _fingerprint_notes(paths: list[Path]) -> dict[Path, tuple[int, int]]:
        """Stat every note once, dropping the ones that vanished mid-sweep."""
        fingerprints: dict[Path, tuple[int, int]] = {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError as exc:
                _LOGGER.warning("Alias index: skipping %s: %s", path, exc)
                continue
            fingerprints[path] = (stat.st_mtime_ns, stat.st_size)
        return fingerprints

    async def _build_alias_index(self) -> dict[str, str | None]:
        async with self._alias_lock:
            if self._alias_cache is not None:
                return self._alias_cache

            records = await self._collect_alias_records()

            # Strict global priority per contracts section 2.6: title -> filename stem
            # -> aliases. A higher tier shadows lower tiers entirely. Within a
            # tier, multiple notes claiming the same key resolve to None
            # (ambiguous within that tier).
            self._alias_cache = build_tiered_alias_index(
                records,
                identity=lambda record: record.note_id,
                title=lambda record: (record.title,),
                stem=lambda record: (Path(record.rel_path).stem,),
                aliases=lambda record: record.aliases,
                normalize=lambda value: value.strip().lower(),
            )
            path_links: dict[str, str | None] = {}
            for record in records:
                key = record.rel_path.lower().removesuffix(_MARKDOWN_SUFFIX)
                path_links[key] = None if key in path_links else record.note_id
            self._path_link_cache = path_links
            for key, value in self._alias_cache.items():
                if value is None:
                    _LOGGER.warning(
                        "Alias %r ambiguous within priority tier; marking unresolved.",
                        key,
                    )
            return self._alias_cache


# ---------------------------------------------------------------------------
# Structural conformance check
# ---------------------------------------------------------------------------
#
# mypy validates that FilesystemVaultReader satisfies the VaultReader Protocol.
# Drift in either direction (Protocol method renamed, concrete class signature
# changed) fails type-check before runtime. No cost at import time beyond the
# variable assignment.

from datacron.core.protocols import VaultReader as _VaultReaderProtocol  # noqa: E402


def _conformance_check(reader: _VaultReaderProtocol) -> _VaultReaderProtocol:
    """Force mypy to verify :class:`FilesystemVaultReader` <-> Protocol parity."""
    return reader


def _assert_conformance() -> None:
    """Static check only -- never invoked at runtime."""
    _conformance_check(FilesystemVaultReader(Path()))
