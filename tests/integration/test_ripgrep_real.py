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
"""Integration tests for RipgrepWrapper using a real ``rg`` binary."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from datacron.core.vault import FilesystemVaultReader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.ripgrep import RipgrepWrapper

pytestmark = pytest.mark.integration


@pytest.fixture
def rg_path() -> str:
    resolved = shutil.which("rg")
    if resolved is None:
        pytest.skip("ripgrep binary not found on PATH")
    return resolved


@pytest.fixture
async def indexed_demo_vault(tmp_vault: Path) -> AsyncIterator[tuple[Path, SQLiteFTS5Store]]:
    reader = FilesystemVaultReader(tmp_vault)
    chunker = MarkdownChunker()
    notes = await reader.list_notes()
    store = SQLiteFTS5Store()
    await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
    for note in notes:
        await store.upsert_note(note, chunker.chunk(note))

    try:
        yield tmp_vault, store
    finally:
        await store.close()


async def test_real_rg_search_resolves_demo_vault_chunk(
    indexed_demo_vault: tuple[Path, SQLiteFTS5Store],
    rg_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_root, store = indexed_demo_vault
    monkeypatch.setenv("DATACRON_RIPGREP_PATH", rg_path)

    results = await RipgrepWrapper().search("Datacron", vault_root, limit=5, store=store)

    assert results
    assert results[0].chunk.note_rel_path == "welcome.md"
    assert "**Datacron**" in results[0].snippet


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("*.md", {"root.md", "_memory/direct.md", "_memory/deep/nested.md", "other/direct.md"}),
        ("_memory/*.md", {"_memory/direct.md"}),
        ("_memory/**/*.md", {"_memory/direct.md", "_memory/deep/nested.md"}),
        ("**/_memory/**/*.md", {"_memory/direct.md", "_memory/deep/nested.md"}),
        ("_MEMORY/*.md", set()),
        ("missing/*.md", set()),
    ],
)
async def test_relative_globs_match_identically_with_real_rg_and_fallback(
    tmp_path: Path, rg_path: str, glob: str, expected: set[str]
) -> None:
    for name in ("root.md", "_memory/direct.md", "_memory/deep/nested.md", "other/direct.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n", encoding="utf-8")
    reader = FilesystemVaultReader(tmp_path)
    store = SQLiteFTS5Store()
    await store.open(tmp_path / ".datacron/index/datacron.db")
    try:
        for note in await reader.list_notes():
            await store.upsert_note(note, MarkdownChunker().chunk(note))
        for binary in (rg_path, str(tmp_path / "missing-rg")):
            results = await RipgrepWrapper().search(
                "needle", tmp_path, glob=glob, store=store, rg_path=binary
            )
            assert {result.chunk.note_rel_path for result in results} == expected
    finally:
        await store.close()


async def test_mcp_distinguishes_empty_glob_from_empty_text_match(
    indexed_demo_vault: tuple[Path, SQLiteFTS5Store], rg_path: str
) -> None:
    from datacron.core.config import Settings
    from datacron.mcp.server import build_app
    from datacron.mcp.tools.search import _search_regex_impl

    root, store = indexed_demo_vault
    app = build_app(
        settings=Settings(vault_root=root, read_paths=[root], ripgrep_path=rg_path),
        vault_root=root,
        store=store,
    )
    missing = await _search_regex_impl(app, pattern="needle", glob="missing/*.md", limit=5)
    assert missing["error"]["code"] == "regex_glob_no_files"
    empty = await _search_regex_impl(app, pattern="unmatchedneedle", glob="welcome.md", limit=5)
    assert empty["returned"] == 0
    invalid = await _search_regex_impl(app, pattern="needle", glob="!*.md", limit=5)
    assert invalid["error"]["code"] == "regex_glob_invalid"
