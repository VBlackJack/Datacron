# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the Sem-3 search tools: search_text, search_regex, get_backlinks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import Settings
from datacron.core.models import SearchResult
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.ripgrep import RipgrepWrapper
from datacron.indexing.wikilinks import RegexWikilinksExtractor
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _get_backlinks_impl,
    _search_regex_impl,
    _search_text_impl,
)

# ---------------------------------------------------------------------------
# Fixtures: build an indexed DatacronApp on top of the demo vault.
# ---------------------------------------------------------------------------


@pytest.fixture
async def indexed_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    db_path = tmp_vault / ".datacron" / "index" / "datacron.db"
    await store.open(db_path)

    app = build_app(
        settings=settings,
        vault_root=tmp_vault,
        chunker=MarkdownChunker(),
        store=store,
        ripgrep=RipgrepWrapper(),
        wikilinks=RegexWikilinksExtractor(),
    )
    notes = await app.vault_reader.list_notes()
    for note in notes:
        await app.store.upsert_note(note, app.chunker.chunk(note))

    try:
        yield app
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# search_text
# ---------------------------------------------------------------------------


class TestSearchText:
    @pytest.mark.asyncio
    async def test_returns_hits_with_sandbox_wrapped_snippet(
        self, indexed_app: DatacronApp
    ) -> None:
        result = await _search_text_impl(indexed_app, query="Welcome", limit=5)
        assert "error" not in result
        assert result["returned"] >= 1
        assert result["query"] == "Welcome"
        sample = result["results"][0]
        assert sample["snippet"].startswith('<vault_content path="')
        assert sample["snippet"].endswith("</vault_content>")
        assert "**" in sample["snippet"]  # FTS5 snippet keeps **term** highlighting
        assert sample["chunk_id"]
        assert sample["note_rel_path"]
        assert sample["score"] > 0
        assert sample["line_start"] >= 1
        assert sample["line_end"] >= sample["line_start"]

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, indexed_app: DatacronApp) -> None:
        result = await _search_text_impl(indexed_app, query="   ", limit=5)
        assert "error" in result
        assert result["error"]["type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_results(self, indexed_app: DatacronApp) -> None:
        # Plain FTS5 token (no `-` which is the NOT operator): no demo note
        # contains it, so the search must complete cleanly with zero results.
        result = await _search_text_impl(indexed_app, query="xyzzy", limit=5)
        assert "error" not in result
        assert result["returned"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_malformed_fts5_syntax_is_treated_as_literal_text(
        self, indexed_app: DatacronApp
    ) -> None:
        result = await _search_text_impl(indexed_app, query='"unterminated', limit=5)
        assert "error" not in result
        assert result["returned"] == 0

    @pytest.mark.asyncio
    async def test_limit_bounded_by_max_result_count(self, tmp_vault: Path) -> None:
        settings = Settings(
            read_paths=[tmp_vault],
            vault_root=tmp_vault,
            max_result_count=2,
            max_result_tokens=8000,
        )
        store = SQLiteFTS5Store()
        await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
        app = build_app(
            settings=settings,
            vault_root=tmp_vault,
            chunker=MarkdownChunker(),
            store=store,
        )
        chunker = app.chunker
        for note in await app.vault_reader.list_notes():
            await app.store.upsert_note(note, chunker.chunk(note))

        try:
            result = await _search_text_impl(app, query="welcome", limit=999)
        finally:
            await store.close()

        assert result["limit_applied"] == 2
        assert result["returned"] <= 2

    @pytest.mark.asyncio
    async def test_search_text_repairs_empty_index_on_read(self, tmp_vault: Path) -> None:
        settings = Settings(
            read_paths=[tmp_vault],
            vault_root=tmp_vault,
            max_result_count=20,
            max_result_tokens=8000,
        )
        store = SQLiteFTS5Store()
        await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
        app = build_app(
            settings=settings,
            vault_root=tmp_vault,
            chunker=MarkdownChunker(),
            store=store,
        )

        try:
            result = await _search_text_impl(app, query="Welcome", limit=5)
        finally:
            await store.close()

        assert "error" not in result
        assert result["returned"] >= 1
        assert result["index_repair"]["reindexed_notes"] == 6


# ---------------------------------------------------------------------------
# search_regex
# ---------------------------------------------------------------------------


class _StubRipgrep:
    """Async stub that mimics RipgrepWrapper.search without spawning rg."""

    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        pattern: str,
        vault_root: Path,
        glob: str | None = None,
        limit: int = 20,
        store: Any = None,
        rg_path: str | None = None,
    ) -> list[SearchResult]:
        self.calls.append({"pattern": pattern, "glob": glob, "limit": limit, "rg_path": rg_path})
        return self._results[:limit]


@pytest.fixture
async def stubbed_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
    app = build_app(
        settings=settings,
        vault_root=tmp_vault,
        chunker=MarkdownChunker(),
        store=store,
        ripgrep=_StubRipgrep([]),
    )
    notes = await app.vault_reader.list_notes()
    for note in notes:
        await app.store.upsert_note(note, app.chunker.chunk(note))
    try:
        yield app
    finally:
        await store.close()


class TestSearchRegex:
    @pytest.mark.asyncio
    async def test_invalid_regex_returns_structured_error(self, stubbed_app: DatacronApp) -> None:
        result = await _search_regex_impl(stubbed_app, pattern="(", glob=None, limit=5)
        assert "error" in result
        assert result["error"]["type"] == "ValueError"
        assert "invalid regex" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_empty_pattern_returns_structured_error(self, stubbed_app: DatacronApp) -> None:
        result = await _search_regex_impl(stubbed_app, pattern="", glob=None, limit=5)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_passes_glob_and_bounded_limit_to_ripgrep(self, stubbed_app: DatacronApp) -> None:
        stub = stubbed_app.ripgrep
        assert isinstance(stub, _StubRipgrep)
        await _search_regex_impl(stubbed_app, pattern="kafka", glob="*.md", limit=999)
        assert stub.calls == [
            {
                "pattern": "kafka",
                "glob": "*.md",
                "limit": 20,
                "rg_path": "rg",
            }
        ]  # limit bounded by max_result_count

    @pytest.mark.asyncio
    async def test_missing_rg_binary_returns_error(self, stubbed_app: DatacronApp) -> None:
        class _MissingRg:
            async def search(self, **_kwargs: Any) -> list[SearchResult]:
                raise FileNotFoundError("ripgrep binary not found: rg")

        replaced = build_app(
            settings=stubbed_app.settings,
            vault_root=stubbed_app.vault_root,
            chunker=stubbed_app.chunker,
            store=stubbed_app.store,
            ripgrep=_MissingRg(),  # type: ignore[arg-type]
        )
        result = await _search_regex_impl(replaced, pattern="ok", glob=None, limit=5)
        assert "error" in result
        assert result["error"]["type"] == "FileNotFoundError"

    @pytest.mark.asyncio
    async def test_results_are_sandbox_wrapped(
        self, indexed_app: DatacronApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a fake SearchResult against a real indexed chunk
        chunks = await indexed_app.store.list_chunks_for_note(
            (await indexed_app.vault_reader.list_notes())[0].id
        )
        chunk = chunks[0]
        stub = _StubRipgrep([SearchResult(chunk=chunk, score=1.0, snippet="hit **term** line")])
        replaced = build_app(
            settings=indexed_app.settings,
            vault_root=indexed_app.vault_root,
            chunker=indexed_app.chunker,
            store=indexed_app.store,
            ripgrep=stub,
        )
        result = await _search_regex_impl(replaced, pattern="x", glob=None, limit=5)
        assert "error" not in result
        assert result["returned"] == 1
        assert result["results"][0]["snippet"].startswith('<vault_content path="')
        assert "**term**" in result["results"][0]["snippet"]


# ---------------------------------------------------------------------------
# get_backlinks
# ---------------------------------------------------------------------------


class TestGetBacklinks:
    @pytest.mark.asyncio
    async def test_unresolved_target_returns_empty_list(self, indexed_app: DatacronApp) -> None:
        result = await _get_backlinks_impl(indexed_app, target="Phantom Note", limit=10)
        assert "error" not in result
        assert result["resolved_note_id"] is None
        assert result["results"] == []
        assert result["returned"] == 0

    @pytest.mark.asyncio
    async def test_empty_target_returns_error(self, indexed_app: DatacronApp) -> None:
        result = await _get_backlinks_impl(indexed_app, target="   ", limit=10)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_resolves_target_and_finds_known_backlinks(
        self, indexed_app: DatacronApp
    ) -> None:
        # The demo vault: welcome.md links to [[Important Note]],
        # nested-thoughts.md links to [[Welcome]],
        # important-note.md links to [[Welcome]].
        result = await _get_backlinks_impl(
            indexed_app, target="Welcome to the Demo Vault", limit=20
        )
        assert "error" not in result
        assert result["resolved_note_id"] is not None
        source_paths = {row["source_note_rel_path"] for row in result["results"]}
        assert {"important-note.md", "subfolder/nested-thoughts.md"} <= source_paths

    @pytest.mark.asyncio
    async def test_backlinks_resolve_through_aliases(self, indexed_app: DatacronApp) -> None:
        # welcome.md has alias "Welcome" — passing the alias must resolve and
        # surface incoming wikilinks (including those written as [[Welcome]]).
        result = await _get_backlinks_impl(indexed_app, target="Welcome", limit=20)
        assert "error" not in result
        assert result["resolved_note_id"] is not None
        source_paths = {row["source_note_rel_path"] for row in result["results"]}
        assert "important-note.md" in source_paths

    @pytest.mark.asyncio
    async def test_excludes_target_self(self, indexed_app: DatacronApp) -> None:
        """A note must never appear as its own backlink even if it references itself."""
        result = await _get_backlinks_impl(
            indexed_app, target="Welcome to the Demo Vault", limit=20
        )
        for row in result["results"]:
            assert row["source_note_id"] != result["resolved_note_id"]
