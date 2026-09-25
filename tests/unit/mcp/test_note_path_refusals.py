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
"""Note write tools refuse paths the reader could never admit afterwards.

A note written under a hidden folder was committed, then refused by every
read, patch and revert, while the result told the client to re-read it. A
colon (an NTFS alternate data stream) or a reserved device name failed only
after a stray temp file had been created next to the target.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import _create_note_ai_impl
from datacron.mcp.tools.write_validation import _assert_markdown_rel_path


@pytest.fixture
async def writable_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(read_paths=[tmp_path], write_paths=[tmp_path], vault_root=tmp_path)
    store = SQLiteFTS5Store()
    await store.open(tmp_path / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(
            settings=settings, vault_root=tmp_path, chunker=MarkdownChunker(), store=store
        )
    finally:
        await store.close()


@pytest.mark.parametrize(
    "rel_path",
    [".datacron/evil.md", ".obsidian/x.md", "notes/.hidden/x.md", "notes/host.md:hidden.md"],
)
async def test_a_path_the_reader_cannot_admit_is_refused_before_any_write(
    writable_app: DatacronApp, rel_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The colon is a Win32 rule; the hidden-folder rule holds everywhere.
    monkeypatch.setattr(sys, "platform", "win32")
    before = sorted(
        path.relative_to(writable_app.vault_root) for path in writable_app.vault_root.rglob("*")
    )

    result = await _create_note_ai_impl(
        writable_app,
        rel_path=rel_path,
        title="Refused",
        body="body",
        origin="ai",
        confidence="high",
        tags=["memory"],
    )

    assert result["error"]["type"] == "ValueError", result
    after = sorted(
        path.relative_to(writable_app.vault_root) for path in writable_app.vault_root.rglob("*")
    )
    assert after == before


@pytest.mark.parametrize("rel_path", ["CON.md", "notes/nul.md", "notes/com1.backup.md"])
async def test_a_reserved_device_name_is_refused(
    writable_app: DatacronApp, rel_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    result = await _create_note_ai_impl(
        writable_app,
        rel_path=rel_path,
        title="Refused",
        body="body",
        origin="ai",
        confidence="high",
        tags=["memory"],
    )

    assert "reserved device name" in result["error"]["message"], result


async def test_an_ordinary_note_is_still_written(writable_app: DatacronApp) -> None:
    result = await _create_note_ai_impl(
        writable_app,
        rel_path="_memory/facts/console.md",
        title="Console",
        body="body",
        origin="ai",
        confidence="high",
        tags=["memory"],
    )

    assert "error" not in result, result
    assert (writable_app.vault_root / "_memory" / "facts" / "console.md").is_file()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("rel_path", ["projects/Con.md", "Meetings/10:30 standup.md"])
def test_windows_only_names_are_ordinary_notes_elsewhere(
    rel_path: str, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader admits these names on Linux and macOS, so the writer must as well."""
    monkeypatch.setattr(sys, "platform", platform)

    _assert_markdown_rel_path(rel_path)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_a_hidden_folder_is_refused_on_every_platform(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(ValueError, match="hidden folder"):
        _assert_markdown_rel_path("notes/.hidden/x.md")
