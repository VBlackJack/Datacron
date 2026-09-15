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
"""The archive tags come from the vault tag policy; the index and the library follow it."""

from __future__ import annotations

from pathlib import Path

from datacron.core.config import DEFAULT_ARCHIVE_TAGS, VaultConfig, load_vault_config
from datacron.core.frontmatter import serialize
from datacron.core.models import Note
from datacron.core.paths import sidecar_vault_config
from datacron.core.vault import build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.organization.library import lifecycle, vault_archive_tags

_RETIRED_ID = "01J00000000000000000000201"
_DEFAULT_ID = "01J00000000000000000000202"
_POLICY = (
    "organization:\n"
    "  scope: facts\n"
    "  rules:\n"
    "    - tag: memory/fact\n"
    "      folder: facts\n"
    "      naming: '{slug}'\n"
    "  tags:\n"
    "    placement_namespace: memory\n"
    "    archive_tags:\n"
    "      - status/retired\n"
)


def _write_note(vault: Path, name: str, note_id: str, tag: str) -> None:
    (vault / name).write_text(
        serialize(
            {"id": note_id, "tags": [tag], "last_verified": "2026-09-01"},
            f"# {name}\n\nBody.\n",
        ),
        encoding="utf-8",
        newline="",
    )


async def _read_notes(vault: Path) -> list[Note]:
    reader = build_configured_reader(vault, read_only=True)
    return [
        await reader.read_note(vault / "retired.md"),
        await reader.read_note(vault / "default.md"),
    ]


async def test_archive_tags_follow_the_vault_policy(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".datacron").mkdir(parents=True)
    sidecar_vault_config(vault).write_text(_POLICY, encoding="utf-8", newline="\n")
    _write_note(vault, "retired.md", _RETIRED_ID, "status/retired")
    _write_note(vault, "default.md", _DEFAULT_ID, DEFAULT_ARCHIVE_TAGS[0])
    config = load_vault_config(sidecar_vault_config(vault))
    assert config is not None
    assert config.archive_tags == frozenset({"status/retired"})
    assert vault_archive_tags(vault) == frozenset({"status/retired"})

    retired, default = notes = await _read_notes(vault)
    store = SQLiteFTS5Store(archive_tags=config.archive_tags)
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    try:
        for note in notes:
            await store.upsert_note(note, MarkdownChunker().chunk(note))
        metadata = await store.list_temporal_metadata()
    finally:
        await store.close()

    # The policy replaces the defaults: only its own tag archives a note.
    assert metadata[retired.id].archived is True
    assert metadata[default.id].archived is False
    assert lifecycle(retired, notes, archive_tags=config.archive_tags) == "historical"
    assert lifecycle(default, notes, archive_tags=config.archive_tags) == "active"


async def test_archive_tags_fall_back_to_the_shared_defaults(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "retired.md", _RETIRED_ID, "status/retired")
    _write_note(vault, "default.md", _DEFAULT_ID, DEFAULT_ARCHIVE_TAGS[0])
    assert VaultConfig().archive_tags == frozenset(DEFAULT_ARCHIVE_TAGS)
    assert vault_archive_tags(vault) == frozenset(DEFAULT_ARCHIVE_TAGS)

    retired, default = notes = await _read_notes(vault)
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    try:
        for note in notes:
            await store.upsert_note(note, MarkdownChunker().chunk(note))
        metadata = await store.list_temporal_metadata()
    finally:
        await store.close()

    assert metadata[retired.id].archived is False
    assert metadata[default.id].archived is True
    assert lifecycle(retired, notes) == "active"
    assert lifecycle(default, notes) == "historical"
