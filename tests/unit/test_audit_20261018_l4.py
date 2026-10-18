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
"""Regression tests for Audit 2026-10-18 lot L4 (indexing and vault roots)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from datacron.core import config as config_module
from datacron.core import markdown_headings as headings_module
from datacron.core.config import Settings
from datacron.core.markdown_sections import HeadingNotFoundError, find_section_span
from datacron.core.models import Chunk, Note, SearchResult
from datacron.core.vault import FilesystemVaultReader
from datacron.indexing import chunker as chunker_module
from datacron.indexing import fts5_store as fts5_module
from datacron.indexing import ripgrep as ripgrep_module
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.indexing.ripgrep import RipgrepError, RipgrepWrapper, _build_command

# Enough chunks to need more than one keyset page of ``iter_all_chunks``.
_MANY_NOTES = 40
_SECTIONS_PER_NOTE = 10
_PREVIOUS_CHUNKER_VERSION = 2
_SEARCH_TIMEOUT_SECONDS = 5.0
_SLOW_ROOT_SECONDS = 0.5


def _reader(root: Path) -> FilesystemVaultReader:
    return FilesystemVaultReader(root, memory_root=root, extra_read_vaults={})


def _db_path(root: Path) -> Path:
    return root / ".datacron" / "index" / "datacron.db"


async def _open_store(root: Path) -> SQLiteFTS5Store:
    store = SQLiteFTS5Store()
    await store.open(_db_path(root))
    return store


async def _read(root: Path, name: str) -> Note:
    return await _reader(root).read_note(root / name)


# C-1 ---------------------------------------------------------------------------------


async def test_record_mtime_joins_the_bulk_transaction(tmp_path: Path) -> None:
    for index in range(2):
        (tmp_path / f"n{index}.md").write_text(f"# N{index}\n\nbody\n", encoding="utf-8")
    store = await _open_store(tmp_path)
    try:
        chunker = MarkdownChunker()
        notes = [await _read(tmp_path, f"n{index}.md") for index in range(2)]
        connection = store._conn
        assert connection is not None
        commits = 0
        original_commit = connection.commit

        async def counting_commit() -> None:
            nonlocal commits
            commits += 1
            await original_commit()

        connection.commit = counting_commit  # type: ignore[method-assign]
        async with store.bulk_writes(commit_every=500):
            await store.upsert_note(notes[0], chunker.chunk(notes[0]), fs_mtime_ns=1)
            await store.record_mtime(notes[0].id, 2)
            # The batch is still one open transaction, and nothing committed yet.
            assert connection.in_transaction
            assert commits == 0
            await store.upsert_note(notes[1], chunker.chunk(notes[1]), fs_mtime_ns=1)
        assert commits == 1
    finally:
        await store.close()
    with sqlite3.connect(_db_path(tmp_path)) as other:
        row = other.execute(
            "SELECT fs_mtime FROM notes WHERE note_id = ?", (notes[0].id,)
        ).fetchone()
    assert row[0] == 2


async def test_record_mtime_outside_a_bulk_scope_still_commits(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("# N\n\nbody\n", encoding="utf-8")
    store = await _open_store(tmp_path)
    try:
        note = await _read(tmp_path, "n.md")
        await store.upsert_note(note, MarkdownChunker().chunk(note), fs_mtime_ns=1)
        await store.record_mtime(note.id, 7)
        connection = store._conn
        assert connection is not None
        assert not connection.in_transaction
    finally:
        await store.close()
    with sqlite3.connect(_db_path(tmp_path)) as other:
        row = other.execute("SELECT fs_mtime FROM notes WHERE note_id = ?", (note.id,)).fetchone()
    assert row[0] == 7


# C-2 / A-5 / E-6 -----------------------------------------------------------------------


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    vault = tmp_path / "docs"
    memory = tmp_path / "memory"
    elsewhere = tmp_path / "reference"
    for folder in (vault, memory, elsewhere):
        folder.mkdir()
    return vault, memory, elsewhere


def test_sibling_vaults_and_an_outside_mount_are_accepted(tmp_path: Path) -> None:
    vault, memory, elsewhere = _layout(tmp_path)
    settings = Settings(vault_root=vault, memory_root=memory, extra_read_vaults={"ref": elsewhere})
    assert settings.extra_read_vaults == {"ref": elsewhere.resolve()}
    # A memory root equal to the vault root is the single-vault layout.
    assert Settings(vault_root=vault, memory_root=vault).memory_root == vault.resolve()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("mount_contains_vault", "overlaps vault_root"),
        ("mount_inside_vault", "overlaps vault_root"),
        ("mount_is_memory", "overlaps memory_root"),
        ("mount_inside_memory", "overlaps memory_root"),
        ("nested_mounts", "mounts must be disjoint"),
        ("memory_inside_vault", "is inside vault_root"),
        ("memory_contains_vault", "contains vault_root"),
    ],
)
def test_overlapping_roots_are_refused(tmp_path: Path, case: str, message: str) -> None:
    vault, memory, elsewhere = _layout(tmp_path)
    (vault / "sub").mkdir()
    (memory / "_memory").mkdir()
    (elsewhere / "inner").mkdir()
    layouts: dict[str, dict[str, Any]] = {
        "mount_contains_vault": {"extra_read_vaults": {"all": str(tmp_path)}},
        "mount_inside_vault": {"extra_read_vaults": {"mirror": str(vault / "sub")}},
        "mount_is_memory": {"extra_read_vaults": {"mem": str(memory)}},
        "mount_inside_memory": {"extra_read_vaults": {"mem": str(memory / "_memory")}},
        "nested_mounts": {
            "extra_read_vaults": {"ref": str(elsewhere), "inner": str(elsewhere / "inner")}
        },
        "memory_inside_vault": {"memory_root": vault / "sub"},
        "memory_contains_vault": {"memory_root": tmp_path},
    }
    arguments: dict[str, Any] = {"vault_root": vault, "memory_root": memory}
    arguments.update(layouts[case])
    with pytest.raises(ValidationError, match=message):
        Settings(**arguments)


# C-3 ---------------------------------------------------------------------------------


def test_masked_docs_folder_and_missing_mount_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _memory, elsewhere = _layout(tmp_path)
    (vault / "Ref").mkdir()
    (vault / "Ref" / "a.md").write_text("# A\n", encoding="utf-8")
    missing = tmp_path / "does-not-exist"
    warnings: list[str] = []

    def record(message: str, *args: object) -> None:
        warnings.append(message % args if args else message)

    monkeypatch.setattr(config_module._LOGGER, "warning", record)
    Settings(vault_root=vault, extra_read_vaults={"ref": missing})

    assert any("masks the docs folder 'Ref'" in item for item in warnings)
    assert any("not an existing folder" in item for item in warnings)
    warnings.clear()
    Settings(vault_root=vault, extra_read_vaults={"other": elsewhere})
    assert warnings == []


# C-4 ---------------------------------------------------------------------------------


async def test_chunk_scan_holds_no_read_cursor_between_pages(tmp_path: Path) -> None:
    sections = "".join(f"\n## S{index}\n\nbody {index}\n" for index in range(_SECTIONS_PER_NOTE))
    for index in range(_MANY_NOTES):
        (tmp_path / f"n{index}.md").write_text(f"# N{index}\n{sections}", encoding="utf-8")
    store = await _open_store(tmp_path)
    try:
        await reconcile(store, _reader(tmp_path), MarkdownChunker(), mtime_gate=True)
        stats = await store.stats()
        scan = cast("AsyncGenerator[Chunk, None]", store.iter_all_chunks())
        await anext(scan)
        try:
            # Another process writing while the scan is paused on its consumer.
            with sqlite3.connect(_db_path(tmp_path), timeout=0) as writer:
                writer.execute("UPDATE notes SET fs_mtime = fs_mtime")
                writer.commit()
            remaining = [chunk async for chunk in scan]
        finally:
            await scan.aclose()
        assert len(remaining) + 1 == stats.chunk_count
    finally:
        await store.close()


# C-5 / A-7 -----------------------------------------------------------------------------


def test_heading_map_ignores_unicode_line_separators() -> None:
    for separator in ("\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\x85", "\x0b", "\x0c"):
        body = f"Intro line{separator}## Fake\nmore\n## Real\nx\n"
        # Callers still splitting with str.splitlines get the same answer, in their indices.
        lines = body.splitlines(keepends=True)
        found = [
            (item.level, item.text, item.start) for item in headings_module.markdown_headings(lines)
        ]
        assert found == [(2, "Real", 3)], separator
        with pytest.raises(HeadingNotFoundError):
            find_section_span(lines, "Fake", 2)
        shared = headings_module.split_markdown_lines(body)
        assert len(shared) == body.count("\n")


def test_shared_splitter_breaks_on_cr_lf_and_crlf_only() -> None:
    text = "a\r\nb\rc\nd\N{LINE SEPARATOR}e\x0cf"
    assert headings_module.split_markdown_lines(text) == [
        "a\r\n",
        "b\r",
        "c\n",
        "d\N{LINE SEPARATOR}e\x0cf",
    ]
    assert headings_module.split_markdown_lines("") == []


async def test_chunk_line_numbers_follow_newline_numbering(tmp_path: Path) -> None:
    raw = (
        "---\ntitle: T\n---\n# A\n\npara one\N{LINE SEPARATOR}still para\x0cmore\n\n"
        "## B\n\nneedle here\n"
    )
    (tmp_path / "n.md").write_text(raw, encoding="utf-8", newline="")
    note = await _read(tmp_path, "n.md")
    chunks = MarkdownChunker().chunk(note)
    needle = next(chunk for chunk in chunks if "needle" in chunk.content)
    expected_line = raw[: raw.index("needle")].count("\n") + 1
    assert needle.line_start == expected_line
    assert [chunk.content for chunk in chunks if chunk.content.startswith("## ")] == ["## B"]


async def test_lone_cr_notes_keep_ordered_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "cr.md").write_bytes(b"# A\r\rpara\r\r## B\r\rneedle\r")
    note = await _read(tmp_path, "cr.md")
    chunks = MarkdownChunker().chunk(note)
    spans = [(chunk.line_start, chunk.line_end) for chunk in chunks]
    assert spans == sorted(spans)
    assert [chunk.line_start for chunk in chunks if "needle" in chunk.content] == [7]


def test_chunker_version_moved_so_indexes_rechunk() -> None:
    assert chunker_module.CHUNKER_VERSION > _PREVIOUS_CHUNKER_VERSION


# C-6 ---------------------------------------------------------------------------------


def test_ripgrep_reads_no_ignore_file_and_keeps_dot_folders_out() -> None:
    command = _build_command("rg", "needle", "notes/**")
    assert "--no-ignore" in command
    assert "--hidden" in command
    separator = command.index("--")
    assert command[separator - 4 : separator] == ["--glob", "notes/**", "--glob", "!.*/"]


# C-7 ---------------------------------------------------------------------------------


async def test_index_archive_reads_normalized_tags_and_shared_statuses(tmp_path: Path) -> None:
    (tmp_path / "hash.md").write_text(
        "---\ntags: ['#meta/archive']\n---\n# H\n\nbody\n", encoding="utf-8"
    )
    (tmp_path / "inline.md").write_text("# I\n\nbody #meta/archive\n", encoding="utf-8")
    (tmp_path / "superseded.md").write_text(
        "---\nstatus: superseded\n---\n# S\n\nbody\n", encoding="utf-8"
    )
    (tmp_path / "live.md").write_text("# L\n\nbody\n", encoding="utf-8")
    store = await _open_store(tmp_path)
    try:
        await reconcile(store, _reader(tmp_path), MarkdownChunker(), mtime_gate=True)
        metadata = await store.list_temporal_metadata()
        archived = {
            rel_path: metadata[note_id].archived
            for rel_path, note_id in [
                (name, await store.get_note_id(name))
                for name in ("hash.md", "inline.md", "superseded.md", "live.md")
            ]
            if note_id is not None
        }
    finally:
        await store.close()
    assert archived == {
        "hash.md": True,
        "inline.md": True,
        "superseded.md": True,
        "live.md": False,
    }
    assert "superseded" in config_module.HISTORICAL_STATUSES


# C-10 --------------------------------------------------------------------------------


async def test_tied_scores_keep_their_order_across_a_reindex(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / f"{name}.md").write_text(
            "---\ntitle: Same\n---\nshared needle text\n", encoding="utf-8"
        )
    store = await _open_store(tmp_path)
    try:
        await reconcile(store, _reader(tmp_path), MarkdownChunker(), mtime_gate=True)
        before = [result.chunk.note_rel_path for result in await store.search("needle", 2)]
        (tmp_path / "a.md").write_text(
            "---\ntitle: Same\nextra: 1\n---\nshared needle text\n", encoding="utf-8"
        )
        await reconcile(store, _reader(tmp_path), MarkdownChunker(), mtime_gate=True)
        after = [result.chunk.note_rel_path for result in await store.search("needle", 2)]
    finally:
        await store.close()
    assert before == after == ["a.md", "b.md"]


# C-12 --------------------------------------------------------------------------------


async def test_regex_roots_share_one_deadline_and_report_partial_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    budgets: list[float] = []
    sentinel = AsyncMock()

    async def fake_root(**kwargs: Any) -> list[SearchResult]:
        budgets.append(float(kwargs["timeout_seconds"]))
        if kwargs["prefix"] is None:
            await asyncio.sleep(_SLOW_ROOT_SECONDS)
            return [sentinel]
        raise RipgrepError(2, "mount failed")

    warnings: list[str] = []
    monkeypatch.setattr(ripgrep_module, "_search_single_root", fake_root)
    monkeypatch.setattr(
        ripgrep_module._LOGGER,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    results = await RipgrepWrapper().search(
        "needle",
        tmp_path,
        store=AsyncMock(),
        fallback_timeout_seconds=_SEARCH_TIMEOUT_SECONDS,
        extra_roots={"mnt": mount},
    )
    assert results == [sentinel]
    assert budgets[0] == pytest.approx(_SEARCH_TIMEOUT_SECONDS)
    assert budgets[1] <= _SEARCH_TIMEOUT_SECONDS - _SLOW_ROOT_SECONDS / 2
    assert any("partial results" in item for item in warnings)


# C-16 --------------------------------------------------------------------------------


async def test_underscore_only_terms_do_not_drop_the_and_tier(tmp_path: Path) -> None:
    assert fts5_module.fts5_query_terms("alpha ___ beta") == ["alpha", "beta"]
    (tmp_path / "n.md").write_text("# N\n\nalpha beta gamma\n", encoding="utf-8")
    store = await _open_store(tmp_path)
    try:
        await reconcile(store, _reader(tmp_path), MarkdownChunker(), mtime_gate=True)
        results = await store.search("alpha ___", 5)
    finally:
        await store.close()
    assert results
    assert {result.tier for result in results} == {0}
