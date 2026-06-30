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
"""Confined, reversible filesystem writer for Markdown vault notes."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import final
from uuid import uuid4

from datacron.core.config import Settings
from datacron.core.paths import PathConfinementError, assert_within_write_paths, sidecar_dir
from datacron.core.protocols import VaultWriter

__all__ = ["FilesystemVaultWriter"]


@final
class FilesystemVaultWriter:
    """Write vault-relative Markdown files safely under configured write roots."""

    def __init__(self, vault_root: Path, settings: Settings) -> None:
        self._vault_root = vault_root.expanduser().resolve()
        self._settings = settings

    async def write_note_atomic(self, rel_path: str, content: str, *, overwrite: bool) -> None:
        """Write ``content`` to ``rel_path`` using backup + atomic replace."""
        await asyncio.to_thread(self._write_note_atomic_sync, rel_path, content, overwrite)

    def _write_note_atomic_sync(self, rel_path: str, content: str, overwrite: bool) -> None:
        candidate = (self._vault_root / rel_path).expanduser().resolve()
        target = assert_within_write_paths(candidate, self._settings)
        safe_rel_path = self._safe_relative_path(target)

        if target.exists() and not overwrite:
            raise FileExistsError(f"{safe_rel_path} already exists.")

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._snapshot_existing(target, safe_rel_path)

        temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)

    def _safe_relative_path(self, target: Path) -> Path:
        try:
            return target.relative_to(self._vault_root)
        except ValueError as exc:
            raise PathConfinementError(
                f"Path {target} is outside the bound vault root {self._vault_root}."
            ) from exc

    def _snapshot_existing(self, target: Path, rel_path: Path) -> None:
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = sidecar_dir(self._vault_root) / "backups" / rel_path
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_dir / f"{timestamp}.bak")


def _conformance_check(writer: VaultWriter) -> None:
    """Mypy structural conformance: FilesystemVaultWriter satisfies VaultWriter."""
    _ = writer


_conformance_check(FilesystemVaultWriter(Path("."), Settings()))
