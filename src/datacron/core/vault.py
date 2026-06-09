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

Implements the protocol described in ``docs/agent-briefs/01-contracts.md``
§2.6. The reader walks a Markdown vault, parses frontmatter via
:mod:`datacron.core.frontmatter`, computes canonical content hashes via
:mod:`datacron.core.hashing`, and assigns ULID identifiers without ever
mutating the user's notes — IDs are stored in a JSON sidecar at
``.datacron/ulids.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, final

from ulid import ULID

from datacron.core.config import (
    SIDECAR_DIR_NAME,
    VAULT_CONFIG_FILENAME,
    VaultConfig,
    load_vault_config,
)
from datacron.core.frontmatter import extract_tags, parse
from datacron.core.hashing import hash_text
from datacron.core.logger import get_logger
from datacron.core.models import Note

__all__ = ["FilesystemVaultReader", "JsonIdStore", "build_configured_reader"]

_LOGGER = get_logger(__name__)

MARKDOWN_GLOB: Final[str] = "*.md"
SKIPPED_FOLDERS: Final[frozenset[str]] = frozenset(
    {SIDECAR_DIR_NAME, ".git", ".obsidian", ".hg", ".svn", "node_modules"}
)
ULID_SIDECAR_FILENAME: Final[str] = "ulids.json"
MIGRATED_ULID_SIDECAR_FILENAME: Final[str] = "ulids.json.migrated"
_H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)


def build_configured_reader(
    vault_root: Path,
    *,
    id_store: JsonIdStore | None = None,
) -> FilesystemVaultReader:
    """Build a reader honoring ``excluded_folders`` from ``.datacron/VAULT.yaml``."""
    resolved_root = vault_root.expanduser().resolve()
    config_path = resolved_root / SIDECAR_DIR_NAME / VAULT_CONFIG_FILENAME
    config = load_vault_config(config_path) or VaultConfig()
    return FilesystemVaultReader(
        resolved_root,
        id_store=id_store,
        excluded_folders=frozenset(config.excluded_folders),
    )


def _normalize_rel_path(path: Path, vault_root: Path) -> str:
    """Return ``path`` relative to ``vault_root`` with POSIX separators."""
    rel = path.resolve().relative_to(vault_root.resolve())
    return str(PurePosixPath(*rel.parts))


def _coerce_aliases(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


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


def _first_h1(body: str) -> str | None:
    match = _H1_PATTERN.search(body)
    return match.group(1).strip() if match else None


def _resolve_title(metadata: dict[str, object], body: str, path: Path) -> str:
    front_title = metadata.get("title")
    if isinstance(front_title, str) and front_title.strip():
        return front_title.strip()
    h1 = _first_h1(body)
    if h1:
        return h1
    return path.stem


def _read_id_mapping(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"ULID sidecar {path} is not a JSON object (found {type(data).__name__}).")
    return {str(k): str(v) for k, v in data.items()}


@final
class JsonIdStore:
    """JSON-backed mapping from vault-relative paths to ULIDs.

    The store is lazy: it reads ``ulids.json`` on first access and persists
    after every mutation. Phase-0 only — Codex's FTS5 ``ulid_paths`` table
    becomes the source of truth once it lands in Sem 2; the JSON file remains
    as a fallback bootstrap.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load_sync(self) -> dict[str, str]:
        primary_exists = self._path.exists()
        try:
            primary = _read_id_mapping(self._path) if primary_exists else {}
        except OSError as exc:
            _LOGGER.error("Failed to read ULID sidecar %s: %s", self._path, exc)
            raise

        migrated_path = self._path.with_name(MIGRATED_ULID_SIDECAR_FILENAME)
        if not migrated_path.exists():
            return primary

        try:
            migrated = _read_id_mapping(migrated_path)
        except OSError as exc:
            _LOGGER.error("Failed to read migrated ULID sidecar %s: %s", migrated_path, exc)
            raise
        if not migrated:
            return primary

        merged = dict(primary)
        merged.update(migrated)
        if merged != primary:
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
        async with self._lock:
            cache = await self._ensure_loaded()
            cache[rel_path] = note_id
            await asyncio.to_thread(self._write_sync, dict(cache))

    async def snapshot(self) -> dict[str, str]:
        async with self._lock:
            cache = await self._ensure_loaded()
            return dict(cache)


@final
class FilesystemVaultReader:
    """Filesystem-backed implementation of the :class:`VaultReader` protocol.

    A reader is bound to a single ``vault_root`` at construction (contracts
    §2.6 amendment fe5dbc6). All methods operate on that bound root; there
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
        excluded_folders: frozenset[str] | None = None,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        sidecar = self._vault_root / SIDECAR_DIR_NAME / ULID_SIDECAR_FILENAME
        self._id_store = id_store or JsonIdStore(sidecar)
        self._skipped_folders = SKIPPED_FOLDERS | frozenset(excluded_folders or ())
        self._alias_cache: dict[str, str | None] | None = None
        self._alias_lock = asyncio.Lock()

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    @property
    def id_store(self) -> JsonIdStore:
        return self._id_store

    # ------------------------------------------------------------------ read

    async def read_note(self, path: Path) -> Note:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Note not found: {resolved}")
        if not self._is_inside_vault(resolved):
            raise ValueError(f"Path {resolved} is outside the vault root {self._vault_root}.")

        raw_text = await asyncio.to_thread(resolved.read_text, "utf-8")
        stat = await asyncio.to_thread(resolved.stat)
        metadata, body = parse(raw_text)

        rel_path = _normalize_rel_path(resolved, self._vault_root)
        note_id = await self._resolve_id(metadata, rel_path)
        title = _resolve_title(metadata, body, resolved)
        tags = extract_tags(metadata, body)
        aliases = _coerce_aliases(metadata.get("aliases"))

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
            content_hash=hash_text(raw_text),
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
                raise
        return notes

    async def resolve_alias(self, alias: str) -> str | None:
        normalized = alias.strip().lower()
        if not normalized:
            return None
        index = await self._build_alias_index()
        return index.get(normalized)

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
        results: list[Path] = []
        for current_dir, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._should_skip_dir(d))
            for filename in sorted(filenames):
                if filename.lower().endswith(".md"):
                    results.append(Path(current_dir) / filename)
        return results

    def _should_skip_dir(self, name: str) -> bool:
        return name in self._skipped_folders or name.startswith(".")

    async def _resolve_id(self, metadata: dict[str, object], rel_path: str) -> str:
        front_id = metadata.get("id")
        if isinstance(front_id, str) and len(front_id) == 26:
            return front_id
        existing = await self._id_store.get(rel_path)
        if existing:
            return existing
        new_id = str(ULID())
        await self._id_store.set(rel_path, new_id)
        return new_id

    async def _build_alias_index(self) -> dict[str, str | None]:
        async with self._alias_lock:
            if self._alias_cache is not None:
                return self._alias_cache

            paths = await asyncio.to_thread(self._collect_markdown_paths, self._vault_root)
            notes: list[Note] = []
            for path in paths:
                try:
                    notes.append(await self.read_note(path))
                except (OSError, ValueError) as exc:
                    _LOGGER.warning("Alias index: skipping %s: %s", path, exc)

            index: dict[str, str | None] = {}
            # Strict global priority per contracts §2.6: title → filename stem
            # → aliases. A higher tier shadows lower tiers entirely. Within a
            # tier, multiple notes claiming the same key resolve to None
            # (ambiguous within that tier).
            self._merge_alias_tier(index, notes, lambda n: (n.title,))
            self._merge_alias_tier(index, notes, lambda n: (Path(n.rel_path).stem,))
            self._merge_alias_tier(index, notes, lambda n: tuple(n.aliases))

            self._alias_cache = index
            return index

    @staticmethod
    def _merge_alias_tier(
        index: dict[str, str | None],
        notes: list[Note],
        extract: Callable[[Note], Iterable[str]],
    ) -> None:
        tier: dict[str, str | None] = {}
        for note in notes:
            for raw in extract(note):
                key = raw.strip().lower()
                if not key or key in index:
                    continue
                if key in tier:
                    if tier[key] != note.id:
                        tier[key] = None
                else:
                    tier[key] = note.id
        for key, value in tier.items():
            if value is None:
                _LOGGER.warning(
                    "Alias %r ambiguous within priority tier; marking unresolved.",
                    key,
                )
            index[key] = value


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
    """Force mypy to verify :class:`FilesystemVaultReader` ↔ Protocol parity."""
    return reader


def _assert_conformance() -> None:
    """Static check only — never invoked at runtime."""
    _conformance_check(FilesystemVaultReader(Path()))
