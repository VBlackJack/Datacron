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
"""Smoke tests for the shared fixtures exposed in ``tests/conftest.py``.

These cover the public surface that Codex's indexing and eval tests are
allowed to rely on. If we change a fixture signature, these tests fail
loudly before Codex sees the breakage.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from datacron.core.config import Settings
from datacron.core.hashing import hash_text
from datacron.core.models import Chunk, ChunkType, Note

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]


def test_note_factory_default(note_factory: NoteFactory) -> None:
    note = note_factory()
    assert isinstance(note, Note)
    assert len(note.id) == 26
    assert note.rel_path == "note.md"
    assert note.title == "Test Note"
    assert note.content_hash == hash_text(note.raw_content)


def test_note_factory_recomputes_hash(note_factory: NoteFactory) -> None:
    note = note_factory(raw_content="# Custom\n\nBody.\n")
    assert note.raw_content == "# Custom\n\nBody.\n"
    assert note.content_hash == hash_text("# Custom\n\nBody.\n")


def test_note_factory_honors_overrides(note_factory: NoteFactory) -> None:
    note = note_factory(
        rel_path="folder/sub.md",
        title="Sub",
        tags=["foo"],
    )
    assert note.rel_path == "folder/sub.md"
    assert note.title == "Sub"
    assert note.tags == ["foo"]


def test_chunk_factory_default(chunk_factory: ChunkFactory) -> None:
    chunk = chunk_factory()
    assert isinstance(chunk, Chunk)
    assert chunk.chunk_type is ChunkType.NARRATIVE
    assert chunk.ordinal == 0
    assert chunk.chunk_id.endswith("::::0000")


def test_chunk_factory_bound_to_note(
    note_factory: NoteFactory, chunk_factory: ChunkFactory
) -> None:
    note = note_factory(rel_path="foo/bar.md")
    chunk = chunk_factory(note=note, header_path="intro", ordinal=3)
    assert chunk.note_id == note.id
    assert chunk.note_rel_path == "foo/bar.md"
    assert chunk.chunk_id == f"{note.id}::intro::0003"


def test_isolated_env_strips_every_settings_variable(
    isolated_env_vars: frozenset[str],
) -> None:
    """A Settings field without its variable in the fixture leaks host state into tests.

    Reproduced before the fixture derived its list from the model: exporting
    DATACRON_SESSION_CONTEXT_SECTIONS made test_default_preferences_select_no_sections
    fail while the manual list silently ignored eight fields.
    """
    prefix = str(Settings.model_config.get("env_prefix", ""))
    expected = {f"{prefix}{name}".upper() for name in Settings.model_fields}

    missing = sorted(expected - isolated_env_vars)

    assert not missing, f"Settings fields without an isolated variable: {missing}"
    # The autouse fixture strips every variable, then pins only the log directory.
    leaked = sorted(name for name in isolated_env_vars if name in os.environ)
    assert leaked == ["DATACRON_LOG_DIR"], leaked


def test_isolated_env_gives_every_test_a_private_user_profile(tmp_path: Path) -> None:
    """Client installers write under the user profile; a test must never reach the real one.

    Reproduced on 2026-10-18: with the profile left as is, a migrate-memory test that
    reconfigured clients rewrote the datacron entry of the developer's own client configs.
    """
    from datacron.installers.mcp_clients import (
        CLAUDE_CODE,
        CODEX_CLI,
        CURSOR,
        GEMINI_CLI,
        _user_config_path,
    )

    profile_root = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(profile_root)
    for key in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        assert Path(os.environ[key]).resolve().is_relative_to(profile_root), key
    for client in (CLAUDE_CODE, CODEX_CLI, CURSOR, GEMINI_CLI):
        config_path = _user_config_path(client)
        assert config_path is not None, client
        assert config_path.resolve().is_relative_to(profile_root), client
