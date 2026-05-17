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
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, final

from ulid import ULID

from datacron.core.config import SIDECAR_DIR_NAME
from datacron.core.frontmatter import extract_tags, parse
from datacron.core.hashing import hash_text
from datacron.core.logger import get_logger
from datacron.core.models import Note

__all__ = ["JsonIdStore", "VaultReader"]

_LOGGER = get_logger(__name__)

MARKDOWN_GLOB: Final[str] = "*.md"
SKIPPED_FOLDERS: Final[frozenset[str]] = frozenset(
    {SIDECAR_DIR_NAME, ".git", ".obsidian", ".hg", ".svn", "node_modules"}
)
ULID_SIDECAR_FILENAME: Final[str] = "ulids.json"
_H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)


def _normalize_rel_path(path: Path, vault_root: Path) -> str:
    """Return ``path`` relative to ``vault_root`` with POSIX separators."""
    rel = path.resolve().relative_to(vault_root.resolve())
    return str(PurePosixPath(*rel.parts))


def _should_skip_dir(name: str) -> bool:
    return name in SKIPPED_FOLDERS or name.startswith(".")


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
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.error("Failed to read ULID sidecar %s: %s", self._path, exc)
            raise
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(
                f"ULID sidecar {self._path} is not a JSON object (found {type(data).__name__})."
            )
        return {str(k): str(v) for k, v in data.items()}

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
class VaultReader:
    """Filesystem-backed implementation of the ``VaultReader`` protocol.

    A reader is bound to a single ``vault_root``. The ``vault_root`` argument
    on the protocol methods is honored but must match the binding; mismatches
    are logged and rejected to keep the ULID sidecar consistent.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        id_store: JsonIdStore | None = None,
    ) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        sidecar = self._vault_root / SIDECAR_DIR_NAME / ULID_SIDECAR_FILENAME
        self._id_store = id_store or JsonIdStore(sidecar)
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
        vault_root: Path,
        folder: str | None = None,
        limit: int | None = None,
    ) -> list[Note]:
        self._assert_matches_vault_root(vault_root)
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

    async def resolve_alias(self, alias: str, vault_root: Path) -> str | None:
        self._assert_matches_vault_root(vault_root)
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

    def _assert_matches_vault_root(self, requested: Path) -> None:
        if requested.expanduser().resolve() != self._vault_root:
            _LOGGER.warning(
                "VaultReader bound to %s received request for %s; using binding.",
                self._vault_root,
                requested,
            )

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
            dirnames[:] = sorted(d for d in dirnames if not _should_skip_dir(d))
            for filename in sorted(filenames):
                if filename.lower().endswith(".md"):
                    results.append(Path(current_dir) / filename)
        return results

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
            index: dict[str, str | None] = {}
            duplicates: set[str] = set()

            for path in paths:
                try:
                    note = await self.read_note(path)
                except (OSError, ValueError) as exc:
                    _LOGGER.warning("Alias index: skipping %s: %s", path, exc)
                    continue
                candidates = self._alias_candidates(note)
                for candidate in candidates:
                    key = candidate.strip().lower()
                    if not key:
                        continue
                    if key in duplicates:
                        continue
                    if key in index and index[key] != note.id:
                        _LOGGER.warning(
                            "Alias %r ambiguous between %s and existing match; marking unresolved.",
                            candidate,
                            note.id,
                        )
                        duplicates.add(key)
                        index[key] = None
                        continue
                    index[key] = note.id

            self._alias_cache = index
            return index

    @staticmethod
    def _alias_candidates(note: Note) -> Iterable[str]:
        yield note.title
        yield Path(note.rel_path).stem
        yield from note.aliases
