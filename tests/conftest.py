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
"""Shared pytest fixtures for the Datacron test suite.

The demo vault under ``tests/fixtures/demo-vault/`` is the canonical fixture
used by both Claude Code's core/MCP tests and Codex's indexing tests (per
``docs/agent-briefs/01-contracts.md`` §5). ``tmp_vault`` copies that vault
into a per-test temporary directory so mutations stay isolated.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from ulid import ULID

from datacron.core.config import reset_settings_cache
from datacron.core.hashing import hash_text
from datacron.core.logger import shutdown_logging
from datacron.core.models import Chunk, ChunkType, Note
from datacron.core.vault import FilesystemVaultReader

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_DEMO_VAULT_DIR: Final[Path] = Path(__file__).parent / "fixtures" / "demo-vault"
FROZEN_TIMESTAMP: Final[datetime] = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

_ENV_VARS: Final[tuple[str, ...]] = (
    "DATACRON_LOG_LEVEL",
    "DATACRON_LOG_DIR",
    "DATACRON_READ_PATHS",
    "DATACRON_VAULT_ROOT",
    "DATACRON_MAX_RESULT_TOKENS",
    "DATACRON_MAX_RESULT_COUNT",
    "DATACRON_RIPGREP_PATH",
    "DATACRON_CHUNK_MAX_TOKENS",
)


@pytest.fixture(autouse=True)
def _isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Strip Datacron env vars and point logs to a tmp dir before every test."""
    for key in _ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATACRON_LOG_DIR", str(tmp_path / "logs"))
    reset_settings_cache()
    yield
    shutdown_logging()
    reset_settings_cache()


@pytest.fixture(scope="session")
def demo_vault_source() -> Path:
    """Return the read-only source path of the bundled demo vault."""
    assert _DEMO_VAULT_DIR.is_dir(), f"Demo vault missing at {_DEMO_VAULT_DIR}"
    return _DEMO_VAULT_DIR


@pytest.fixture
def tmp_vault(demo_vault_source: Path, tmp_path: Path) -> Path:
    """Copy the demo vault into ``tmp_path`` so tests may freely mutate it."""
    target = tmp_path / "vault"
    shutil.copytree(demo_vault_source, target)
    return target


@pytest.fixture
def vault_reader(tmp_vault: Path) -> FilesystemVaultReader:
    """A :class:`FilesystemVaultReader` bound to a freshly-copied demo vault."""
    return FilesystemVaultReader(tmp_vault)


@pytest.fixture
def configured_read_paths(monkeypatch: pytest.MonkeyPatch, tmp_vault: Path) -> Path:
    """Configure ``DATACRON_READ_PATHS`` to point at ``tmp_vault``."""
    monkeypatch.setenv("DATACRON_READ_PATHS", str(tmp_vault))
    monkeypatch.setenv("DATACRON_VAULT_ROOT", str(tmp_vault))
    reset_settings_cache()
    return tmp_vault


@pytest.fixture
def extra_read_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tmp_vault: Path,
) -> tuple[Path, Path]:
    """Configure two read roots separated by the OS path separator."""
    second = tmp_path / "second-vault"
    second.mkdir()
    raw = os.pathsep.join([str(tmp_vault), str(second)])
    monkeypatch.setenv("DATACRON_READ_PATHS", raw)
    reset_settings_cache()
    return tmp_vault, second


@pytest.fixture
def frozen_timestamp() -> datetime:
    """Deterministic timestamp for tests that need a fixed clock reference."""
    return FROZEN_TIMESTAMP


@pytest.fixture
def note_factory() -> NoteFactory:
    """Build :class:`Note` instances in-memory without touching the filesystem.

    Intended for downstream tests (e.g. Codex's chunker) that need to feed a
    populated Note into pure-function code paths. All fields can be overridden
    via kwargs; ``content_hash`` is recomputed from ``raw_content`` whenever
    the caller does not pin it explicitly.
    """

    def _build(**overrides: Any) -> Note:
        raw_content = overrides.pop("raw_content", "# Test Note\n\nBody.\n")
        content = overrides.pop("content", raw_content)
        defaults: dict[str, Any] = {
            "id": str(ULID()),
            "path": Path("vault") / "note.md",
            "rel_path": "note.md",
            "title": "Test Note",
            "frontmatter": {},
            "content": content,
            "raw_content": raw_content,
            "created": FROZEN_TIMESTAMP,
            "updated": FROZEN_TIMESTAMP,
            "content_hash": hash_text(raw_content),
            "tags": [],
            "aliases": [],
        }
        defaults.update(overrides)
        return Note.model_validate(defaults)

    return _build


@pytest.fixture
def chunk_factory(note_factory: NoteFactory) -> ChunkFactory:
    """Build :class:`Chunk` instances bound to a synthetic Note.

    The default chunk is a top-level narrative chunk with ordinal 0. Pass
    ``note=...`` to bind to an existing Note; otherwise a fresh one is built
    via :func:`note_factory`.
    """

    def _build(**overrides: Any) -> Chunk:
        note = overrides.pop("note", None) or note_factory()
        header_path = overrides.pop("header_path", "")
        ordinal = overrides.pop("ordinal", 0)
        content = overrides.pop("content", "Hello world.")
        defaults: dict[str, Any] = {
            "chunk_id": f"{note.id}::{header_path}::{ordinal:04d}",
            "note_id": note.id,
            "note_rel_path": note.rel_path,
            "header_path": header_path,
            "section_title": None,
            "chunk_type": ChunkType.NARRATIVE,
            "content": content,
            "ordinal": ordinal,
            "content_hash": hash_text(content),
            "token_count": max(1, len(content) // 4),
            "wikilinks_out": [],
            "lang": None,
        }
        defaults.update(overrides)
        return Chunk.model_validate(defaults)

    return _build
