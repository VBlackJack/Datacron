# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.mcp.tools`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import parse, serialize
from datacron.core.hashing import hash_text
from datacron.core.models import Note
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app


@pytest.fixture
def app(tmp_vault: Path) -> DatacronApp:
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    return build_app(settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker())


@pytest.fixture
def small_app(tmp_vault: Path) -> DatacronApp:
    """Same as ``app`` but with tiny ceilings to exercise truncation.

    ``get_note_max_tokens`` is capped too so get_note(full) still paginates;
    it is decoupled from the search budget (``max_result_tokens``).
    """
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=3,
        max_result_tokens=50,
        get_note_max_tokens=50,
    )
    return build_app(settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker())


@pytest.fixture
async def app_with_open_store(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(
            settings=settings,
            vault_root=tmp_vault,
            chunker=MarkdownChunker(),
            store=store,
        )
    finally:
        await store.close()


@pytest.fixture
async def writable_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(
        read_paths=[tmp_vault],
        write_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(
            settings=settings,
            vault_root=tmp_vault,
            chunker=MarkdownChunker(),
            store=store,
        )
    finally:
        await store.close()


def _write_memory_note(
    vault_root: Path,
    rel_path: str,
    body: str,
    *,
    metadata_overrides: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    metadata: dict[str, Any] = {
        "id": "01HQXR7K9YZ8M2N3PQRSTV4WX5",
        "title": "Journaled memory",
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "origin": "ai",
        "confidence": "high",
        "last_verified": "2026-01-01",
        "supersedes": [],
        "tags": ["memory"],
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    target = vault_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = serialize(metadata, body)
    target.write_text(raw, encoding="utf-8")
    return target, raw


class TestListNotes:
    @pytest.mark.asyncio
    async def test_returns_expected_shape(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _list_notes_impl

        result = await _list_notes_impl(app, folder=None, tags=None, limit=20)
        assert result["total"] == 6
        assert result["returned"] == 6
        assert result["truncated"] is False
        sample = next(n for n in result["notes"] if n["rel_path"] == "welcome.md")
        assert sample["title"] == "Welcome to the Demo Vault"
        assert "intro" in sample["tags"]
        assert "Welcome" in sample["aliases"]
        assert sample["created"].endswith("+00:00")

    @pytest.mark.asyncio
    async def test_folder_scope(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _list_notes_impl

        result = await _list_notes_impl(app, folder="subfolder", tags=None, limit=20)
        assert {n["rel_path"] for n in result["notes"]} == {"subfolder/nested-thoughts.md"}

    @pytest.mark.asyncio
    async def test_tag_filter_requires_all(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _list_notes_impl

        only_intro = await _list_notes_impl(app, folder=None, tags=["intro"], limit=20)
        assert {n["rel_path"] for n in only_intro["notes"]} == {"welcome.md"}

        # AND semantics: a note must carry every requested tag
        both = await _list_notes_impl(app, folder=None, tags=["intro", "datacron/demo"], limit=20)
        assert {n["rel_path"] for n in both["notes"]} == {"welcome.md"}

        missing = await _list_notes_impl(app, folder=None, tags=["does-not-exist"], limit=20)
        assert missing["notes"] == []
        assert missing["total"] == 0

    @pytest.mark.asyncio
    async def test_limit_bounded_by_max_result_count(self, small_app: DatacronApp) -> None:
        from datacron.mcp.tools import _list_notes_impl

        result = await _list_notes_impl(small_app, folder=None, tags=None, limit=1000)
        assert result["limit_applied"] == 3  # ceiling, not the requested 1000
        assert len(result["notes"]) == 3
        assert result["truncated"] is True
        assert result["total"] == 6

    @pytest.mark.asyncio
    async def test_folder_escape_returns_error_response(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _list_notes_impl

        result = await _list_notes_impl(app, folder="..", tags=None, limit=20)
        assert "error" in result
        assert result["error"]["type"] == "ValueError"


class TestGetNoteFull:
    @pytest.mark.asyncio
    async def test_full_wraps_content(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(app, id_or_path="welcome.md", fmt="full")
        assert result["format"] == "full"
        assert result["rel_path"] == "welcome.md"
        assert result["content"].startswith('<vault_content path="welcome.md">\n')
        assert result["content"].endswith("</vault_content>")
        assert "Welcome" in result["content"]
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_full_truncates_oversized_notes(self, small_app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(small_app, id_or_path="welcome.md", fmt="full")
        assert result["truncated"] is True
        assert result["estimated_tokens"] > result["returned_estimated_tokens"]
        assert result["returned_estimated_tokens"] <= 50
        assert result["next_offset"] is not None

    @pytest.mark.asyncio
    async def test_full_uses_get_note_budget_not_search_budget(self, tmp_vault: Path) -> None:
        """get_note(full) honors get_note_max_tokens, not the search budget.

        A note that would be truncated under a tiny ``max_result_tokens`` must
        come back whole when ``get_note_max_tokens`` is generous — proving the
        two budgets are decoupled (Item 1).
        """
        from datacron.mcp.tools import _get_note_impl

        settings = Settings(
            read_paths=[tmp_vault],
            vault_root=tmp_vault,
            max_result_tokens=50,  # search budget — must NOT affect get_note
            get_note_max_tokens=8000,  # generous note budget
        )
        decoupled_app = build_app(
            settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker()
        )

        result = await _get_note_impl(decoupled_app, id_or_path="welcome.md", fmt="full")
        assert result["truncated"] is False
        assert result["next_offset"] is None

    @pytest.mark.asyncio
    async def test_full_accepts_offset_and_limit(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(
            app,
            id_or_path="welcome.md",
            fmt="full",
            offset=10,
            limit=25,
        )

        assert result["offset"] == 10
        assert result["limit_applied"] == 25
        assert result["returned_chars"] == 25
        assert result["next_offset"] == 35

    @pytest.mark.asyncio
    async def test_full_accepts_chunk_id(self, tmp_vault: Path) -> None:
        from datacron.mcp.tools import _get_note_impl

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
        note = next(n for n in await app.vault_reader.list_notes() if n.rel_path == "welcome.md")
        chunks = app.chunker.chunk(note)
        await app.store.upsert_note(note, chunks)

        try:
            result = await _get_note_impl(app, id_or_path=chunks[0].chunk_id, fmt="full")
        finally:
            await store.close()

        assert result["rel_path"] == "welcome.md"
        assert result["id"] == note.id

    @pytest.mark.asyncio
    async def test_full_accepts_indexed_ulid_without_scanning(
        self,
        app_with_open_store: DatacronApp,
        tmp_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datacron.mcp.tools import _get_note_impl

        note = await app_with_open_store.vault_reader.read_note(tmp_vault / "welcome.md")
        chunks = app_with_open_store.chunker.chunk(note)
        await app_with_open_store.store.upsert_note(note, chunks)

        calls = {"n": 0}
        original_list_notes = app_with_open_store.vault_reader.list_notes

        async def counting_list_notes(
            folder: str | None = None,
            limit: int | None = None,
        ) -> list[Note]:
            calls["n"] += 1
            return await original_list_notes(folder=folder, limit=limit)

        monkeypatch.setattr(app_with_open_store.vault_reader, "list_notes", counting_list_notes)

        result = await _get_note_impl(app_with_open_store, id_or_path=note.id, fmt="full")

        assert result["rel_path"] == "welcome.md"
        assert result["id"] == note.id
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_full_accepts_unindexed_ulid_by_scan_fallback(
        self, app_with_open_store: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _get_note_impl

        note = await app_with_open_store.vault_reader.read_note(tmp_vault / "welcome.md")
        assert await app_with_open_store.store.list_indexed_notes_with_mtime() == {}

        result = await _get_note_impl(app_with_open_store, id_or_path=note.id, fmt="full")

        assert result["rel_path"] == "welcome.md"
        assert result["id"] == note.id

    @pytest.mark.asyncio
    async def test_unknown_ulid_returns_error(self, app_with_open_store: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        bogus = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        result = await _get_note_impl(app_with_open_store, id_or_path=bogus, fmt="full")
        assert "error" in result
        assert result["error"]["type"] == "FileNotFoundError"

    @pytest.mark.asyncio
    async def test_invalid_format_returns_error(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(app, id_or_path="welcome.md", fmt="raw")
        assert "error" in result
        assert "format must be one of" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_outside_vault_rejected(self, app: DatacronApp, tmp_path: Path) -> None:
        from datacron.mcp.tools import _get_note_impl

        outside = tmp_path / "elsewhere" / "secret.md"
        outside.parent.mkdir()
        outside.write_text("# secret", encoding="utf-8")
        result = await _get_note_impl(app, id_or_path=str(outside), fmt="full")
        assert "error" in result


class TestGetNoteMap:
    @pytest.mark.asyncio
    async def test_map_returns_headings(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(app, id_or_path="welcome.md", fmt="map")
        assert result["format"] == "map"
        levels = {h["level"] for h in result["headings"]}
        assert 1 in levels
        assert 2 in levels
        first = result["headings"][0]
        assert first["text"] == "Welcome"
        assert first["path"] == "Welcome"
        assert result["chunk_count"] >= len(result["headings"])

    @pytest.mark.asyncio
    async def test_map_for_empty_note(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        result = await _get_note_impl(app, id_or_path="empty.md", fmt="map")
        assert result["headings"] == []
        assert result["chunk_count"] >= 1


class TestCreateNoteAi:
    @pytest.mark.asyncio
    async def test_creates_typed_note_and_indexes_it_immediately(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.core.frontmatter import parse
        from datacron.mcp.tools import _create_note_ai_impl, _get_note_impl, _search_text_impl

        rel_path = "_memory/facts/generated.md"
        result = await _create_note_ai_impl(
            writable_app,
            rel_path=rel_path,
            title="Generated memory",
            body="# Generated memory\n\nThe durabletoken fact is stored here.\n",
            origin="ai",
            confidence="high",
            tags=["memory", "datacron"],
        )

        assert result["created"]["rel_path"] == rel_path
        assert result["created"]["title"] == "Generated memory"
        assert len(result["created"]["id"]) == 26
        assert result["indexed"] is True

        raw = (tmp_vault / rel_path).read_text(encoding="utf-8")
        metadata, body = parse(raw)
        assert metadata["id"] == result["created"]["id"]
        assert metadata["title"] == "Generated memory"
        assert metadata["origin"] == "ai"
        assert metadata["confidence"] == "high"
        assert metadata["tags"] == ["memory", "datacron"]
        assert metadata["supersedes"] == []
        assert isinstance(metadata["created"], str)
        assert metadata["created"] == metadata["updated"]
        assert isinstance(metadata["last_verified"], str)
        assert "durabletoken" in body

        search = await _search_text_impl(writable_app, query="durabletoken", limit=5)
        assert "error" not in search
        assert any(item["note_rel_path"] == rel_path for item in search["results"])

        fetched = await _get_note_impl(writable_app, id_or_path=rel_path, fmt="full")
        assert fetched["id"] == result["created"]["id"]
        assert fetched["rel_path"] == rel_path

    @pytest.mark.asyncio
    async def test_writes_off_returns_structured_error_without_creating_file(
        self, app_with_open_store: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _create_note_ai_impl

        rel_path = "_memory/facts/denied.md"
        result = await _create_note_ai_impl(
            app_with_open_store,
            rel_path=rel_path,
            title="Denied",
            body="Denied body",
            origin="ai",
            confidence="high",
            tags=["memory"],
        )

        assert result["error"]["type"] == "PathConfinementError"
        assert "writes disabled" in result["error"]["message"]
        assert not (tmp_vault / rel_path).exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("origin", "bogus", "origin must be one of"),
            ("confidence", "bogus", "confidence must be one of"),
            ("tags", [], "tags must not be empty"),
            ("rel_path", "_memory/facts/no-extension", "rel_path must end with .md"),
        ],
    )
    async def test_validation_errors_are_structured_and_do_not_write(
        self,
        writable_app: DatacronApp,
        tmp_vault: Path,
        field: str,
        value: object,
        message: str,
    ) -> None:
        from datacron.mcp.tools import _create_note_ai_impl

        payload: dict[str, Any] = {
            "rel_path": "_memory/facts/invalid.md",
            "title": "Invalid",
            "body": "Invalid body",
            "origin": "ai",
            "confidence": "high",
            "tags": ["memory"],
        }
        payload[field] = value

        result = await _create_note_ai_impl(writable_app, **payload)

        assert result["error"]["type"] == "ValueError"
        assert message in result["error"]["message"]
        assert not (tmp_vault / "_memory" / "facts" / "invalid.md").exists()
        assert not (tmp_vault / "_memory" / "facts" / "no-extension").exists()

    @pytest.mark.asyncio
    async def test_create_never_clobbers_existing_note(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _create_note_ai_impl

        rel_path = "_memory/facts/existing.md"
        target = tmp_vault / rel_path
        target.parent.mkdir(parents=True)
        target.write_text("original\n", encoding="utf-8")

        result = await _create_note_ai_impl(
            writable_app,
            rel_path=rel_path,
            title="Existing",
            body="New body",
            origin="ai",
            confidence="high",
            tags=["memory"],
        )

        assert result["error"]["type"] == "FileExistsError"
        assert "already exists" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == "original\n"

    @pytest.mark.asyncio
    async def test_write_outside_write_roots_returns_structured_error(
        self, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _create_note_ai_impl

        allowed = tmp_vault / "_memory"
        allowed.mkdir()
        settings = Settings(
            read_paths=[tmp_vault],
            write_paths=[allowed],
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
            result = await _create_note_ai_impl(
                app,
                rel_path="elsewhere/blocked.md",
                title="Blocked",
                body="Blocked body",
                origin="ai",
                confidence="high",
                tags=["memory"],
            )
        finally:
            await store.close()

        assert result["error"]["type"] == "PathConfinementError"
        assert "outside the allowed write roots" in result["error"]["message"]
        assert not (tmp_vault / "elsewhere" / "blocked.md").exists()


class TestAppendJournal:
    @pytest.mark.asyncio
    async def test_appends_to_existing_heading_and_reindexes(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl, _search_text_impl

        rel_path = "_memory/facts/journal.md"
        body = (
            "# Journaled memory\n\n"
            "Intro block.\n\n"
            "## Journal\n\n"
            "Old entry.\n\n"
            "## Later\n\n"
            "Tail block.\n"
        )
        target, original_raw = _write_memory_note(tmp_vault, rel_path, body)
        original_metadata, original_body = parse(original_raw)

        result = await _append_journal_impl(
            writable_app,
            rel_path=rel_path,
            heading="Journal",
            entry="- durableappend entry\n  continuation",
        )

        assert result["appended"] == {"rel_path": rel_path, "heading": "Journal"}
        assert result["indexed"] is True

        new_metadata, new_body = parse(target.read_text(encoding="utf-8"))
        original_without_updated = dict(original_metadata)
        original_updated = original_without_updated.pop("updated")
        new_without_updated = dict(new_metadata)
        new_updated = new_without_updated.pop("updated")

        assert new_without_updated == original_without_updated
        assert new_updated != original_updated
        assert new_body == original_body.replace(
            "Old entry.\n\n## Later",
            "Old entry.\n\n- durableappend entry\n  continuation\n\n## Later",
        )

        search = await _search_text_impl(writable_app, query="durableappend", limit=5)
        assert "error" not in search
        assert any(item["note_rel_path"] == rel_path for item in search["results"])

    @pytest.mark.asyncio
    async def test_missing_heading_is_created_at_end(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl

        rel_path = "_memory/facts/new-heading.md"
        target, _original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\nIntro block.\n",
        )

        result = await _append_journal_impl(
            writable_app,
            rel_path=rel_path,
            heading="Decisions",
            entry="- absent heading entry",
        )

        assert result["appended"] == {"rel_path": rel_path, "heading": "Decisions"}
        _metadata, new_body = parse(target.read_text(encoding="utf-8"))
        assert new_body.endswith("\n\n## Decisions\n\n- absent heading entry")

    @pytest.mark.asyncio
    async def test_append_snapshots_previous_version(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl

        rel_path = "_memory/facts/backup.md"
        _target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Journal\n\nBefore backup.\n",
        )

        result = await _append_journal_impl(
            writable_app,
            rel_path=rel_path,
            heading="Journal",
            entry="- backup durable entry",
        )

        assert result["indexed"] is True
        backup_dir = tmp_vault / ".datacron" / "backups" / "_memory" / "facts" / "backup.md"
        backups = list(backup_dir.glob("*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_missing_note_returns_structured_error_without_creating_file(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl

        rel_path = "_memory/facts/missing.md"
        result = await _append_journal_impl(
            writable_app,
            rel_path=rel_path,
            heading="Journal",
            entry="- should not write",
        )

        assert result["error"]["type"] == "FileNotFoundError"
        assert (
            "note not found at _memory/facts/missing.md; use create_note_ai"
            in result["error"]["message"]
        )
        assert not (tmp_vault / rel_path).exists()

    @pytest.mark.asyncio
    async def test_writes_off_returns_clear_error_and_leaves_file_intact(
        self, app_with_open_store: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl

        rel_path = "_memory/facts/writes-off.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Journal\n\nProtected.\n",
        )

        result = await _append_journal_impl(
            app_with_open_store,
            rel_path=rel_path,
            heading="Journal",
            entry="- denied entry",
        )

        assert result["error"]["type"] == "PathConfinementError"
        assert "writes disabled — set DATACRON_WRITE_PATHS" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_append_outside_write_roots_returns_error_and_leaves_file_intact(
        self, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _append_journal_impl

        rel_path = "elsewhere/blocked.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Journal\n\nBlocked.\n",
        )
        allowed = tmp_vault / "_memory"
        allowed.mkdir(exist_ok=True)
        settings = Settings(
            read_paths=[tmp_vault],
            write_paths=[allowed],
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
            result = await _append_journal_impl(
                app,
                rel_path=rel_path,
                heading="Journal",
                entry="- denied entry",
            )
        finally:
            await store.close()

        assert result["error"]["type"] == "PathConfinementError"
        assert "outside the allowed write roots" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw


class TestSetFrontmatter:
    @pytest.mark.asyncio
    async def test_confidence_only_preserves_body_identity_fields_and_snapshots(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/confidence.md"
        body = "# Journaled memory\n\nBody with trailing newline.\n"
        target, original_raw = _write_memory_note(tmp_vault, rel_path, body)
        original_metadata, original_body = parse(original_raw)

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            confidence=" low ",
        )

        assert result["updated"] == {"rel_path": rel_path, "fields": ["confidence"]}
        assert result["indexed"] is True

        new_metadata, new_body = parse(target.read_text(encoding="utf-8"))
        original_without_updated = dict(original_metadata)
        original_updated = original_without_updated.pop("updated")
        new_without_updated = dict(new_metadata)
        new_updated = new_without_updated.pop("updated")

        assert new_body == original_body
        assert new_metadata["confidence"] == "low"
        assert new_without_updated == {**original_without_updated, "confidence": "low"}
        assert new_updated != original_updated

        backup_dir = tmp_vault / ".datacron" / "backups" / "_memory" / "facts" / "confidence.md"
        backups = list(backup_dir.glob("*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_supersedes_replaces_and_cleans_values(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/supersedes.md"
        target, _original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\nBody.\n",
            metadata_overrides={"supersedes": ["01OLDOLDOLDOLDOLDOLDOLDOLD"]},
        )

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            supersedes=[" 01NEWNEWNEWNEWNEWNEWNEWN ", "", "01NEWNEWNEWNEWNEWNEWNEWN", "other"],
        )

        assert result["updated"] == {"rel_path": rel_path, "fields": ["supersedes"]}
        metadata, _body = parse(target.read_text(encoding="utf-8"))
        assert metadata["supersedes"] == ["01NEWNEWNEWNEWNEWNEWNEWN", "other"]

    @pytest.mark.asyncio
    async def test_last_verified_valid_date_is_partial_update(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/last-verified.md"
        target, original_raw = _write_memory_note(
            tmp_vault, rel_path, "# Journaled memory\n\nBody.\n"
        )
        original_metadata, _original_body = parse(original_raw)

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            last_verified="2026-06-30",
        )

        assert result["updated"] == {"rel_path": rel_path, "fields": ["last_verified"]}
        metadata, _body = parse(target.read_text(encoding="utf-8"))
        assert metadata["last_verified"] == "2026-06-30"
        assert metadata["confidence"] == original_metadata["confidence"]
        assert metadata["supersedes"] == original_metadata["supersedes"]

    @pytest.mark.asyncio
    async def test_invalid_last_verified_returns_error_without_write(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/invalid-date.md"
        target, original_raw = _write_memory_note(
            tmp_vault, rel_path, "# Journaled memory\n\nBody.\n"
        )

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            last_verified="20260630",
        )

        assert result["error"]["type"] == "ValueError"
        assert "last_verified must be a YYYY-MM-DD date" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_all_none_returns_error_without_write_or_backup(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/noop.md"
        target, original_raw = _write_memory_note(
            tmp_vault, rel_path, "# Journaled memory\n\nBody.\n"
        )

        result = await _set_frontmatter_impl(writable_app, rel_path=rel_path)

        assert result["error"]["type"] == "ValueError"
        assert result["error"]["message"] == "nothing to update"
        assert target.read_text(encoding="utf-8") == original_raw
        backup_dir = tmp_vault / ".datacron" / "backups" / "_memory" / "facts" / "noop.md"
        assert not backup_dir.exists()

    @pytest.mark.asyncio
    async def test_missing_note_returns_structured_error_without_creating_file(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/missing-frontmatter-target.md"
        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            confidence="low",
        )

        assert result["error"]["type"] == "FileNotFoundError"
        assert (
            "note not found at _memory/facts/missing-frontmatter-target.md; use create_note_ai"
            in result["error"]["message"]
        )
        assert not (tmp_vault / rel_path).exists()

    @pytest.mark.asyncio
    async def test_note_without_frontmatter_returns_error_and_leaves_file_intact(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/plain.md"
        target = tmp_vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        original_raw = "# Plain note\n\nNo frontmatter.\n"
        target.write_text(original_raw, encoding="utf-8")

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            confidence="low",
        )

        assert result["error"]["type"] == "ValueError"
        assert result["error"]["message"] == "note has no frontmatter"
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_invalid_confidence_returns_error_without_write(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/invalid-confidence.md"
        target, original_raw = _write_memory_note(
            tmp_vault, rel_path, "# Journaled memory\n\nBody.\n"
        )

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            confidence="bogus",
        )

        assert result["error"]["type"] == "ValueError"
        assert "confidence must be one of" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_writes_off_returns_clear_error_and_leaves_file_intact(
        self, app_with_open_store: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        rel_path = "_memory/facts/writes-off-frontmatter.md"
        target, original_raw = _write_memory_note(
            tmp_vault, rel_path, "# Journaled memory\n\nBody.\n"
        )

        result = await _set_frontmatter_impl(
            app_with_open_store,
            rel_path=rel_path,
            confidence="low",
        )

        assert result["error"]["type"] == "PathConfinementError"
        assert "writes disabled — set DATACRON_WRITE_PATHS" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_reconcile_updates_temporal_metadata_immediately(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _set_frontmatter_impl

        note_id = "01HQXR7K9YZ8M2N3PQRSTV4WX9"
        rel_path = "_memory/facts/indexed-frontmatter.md"
        _target, _original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Indexed frontmatter\n\nTemporal metadata target.\n",
            metadata_overrides={"id": note_id, "confidence": "high"},
        )

        result = await _set_frontmatter_impl(
            writable_app,
            rel_path=rel_path,
            confidence="low",
            supersedes=["01HQXR7K9YZ8M2N3PQRSTV4WX1"],
        )

        assert result["updated"] == {
            "rel_path": rel_path,
            "fields": ["confidence", "supersedes"],
        }
        temporal = await writable_app.store.list_temporal_metadata()
        assert temporal[note_id].confidence == "low"
        assert temporal[note_id].supersedes == ["01HQXR7K9YZ8M2N3PQRSTV4WX1"]


class TestPatchNoteSection:
    @pytest.mark.asyncio
    async def test_replaces_mid_file_section_preserves_rest_and_reindexes(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl, _search_text_impl

        rel_path = "_memory/facts/patch-mid.md"
        body = (
            "# Journaled memory\n\n"
            "Intro block.\n\n"
            "## Target\n\n"
            "Old target line.\n\n"
            "## Sibling\n\n"
            "Sibling block.\n"
        )
        target, original_raw = _write_memory_note(tmp_vault, rel_path, body)
        original_metadata, original_body = parse(original_raw)

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Target",
            new_content="patchedtoken line\nsecond line",
            expected_hash=hash_text(original_raw),
        )

        assert result["patched"] == {"rel_path": rel_path, "heading": "Target", "level": 2}
        assert result["indexed"] is True

        new_metadata, new_body = parse(target.read_text(encoding="utf-8"))
        original_without_updated = dict(original_metadata)
        original_updated = original_without_updated.pop("updated")
        new_without_updated = dict(new_metadata)
        new_updated = new_without_updated.pop("updated")

        assert new_without_updated == original_without_updated
        assert new_updated != original_updated
        assert new_body == (
            "# Journaled memory\n\n"
            "Intro block.\n\n"
            "## Target\n\n"
            "patchedtoken line\n"
            "second line\n\n"
            "## Sibling\n\n"
            "Sibling block."
        )
        assert new_body.split("## Target", 1)[0] == original_body.split("## Target", 1)[0]
        assert new_body.split("## Sibling", 1)[1] == original_body.split("## Sibling", 1)[1]

        backup_dir = tmp_vault / ".datacron" / "backups" / "_memory" / "facts" / "patch-mid.md"
        backups = list(backup_dir.glob("*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original_raw

        search = await _search_text_impl(writable_app, query="patchedtoken", limit=5)
        assert "error" not in search
        assert any(item["note_rel_path"] == rel_path for item in search["results"])

    @pytest.mark.asyncio
    async def test_hash_mismatch_returns_error_without_write_or_backup(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/stale-hash.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Target\n\nOriginal.\n",
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Target",
            new_content="Replacement.",
            expected_hash="0" * 64,
        )

        assert result["error"]["type"] == "ValueError"
        assert (
            "note changed since read (hash mismatch); re-read and retry"
            in result["error"]["message"]
        )
        assert target.read_text(encoding="utf-8") == original_raw
        backup_dir = tmp_vault / ".datacron" / "backups" / "_memory" / "facts" / "stale-hash.md"
        assert not backup_dir.exists()

    @pytest.mark.asyncio
    async def test_bad_expected_hash_format_errors_before_read(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/missing-bad-hash.md"
        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Target",
            new_content="Replacement.",
            expected_hash="ABC",
        )

        assert result["error"]["type"] == "ValueError"
        assert (
            "expected_hash must be a lowercase 64-character SHA-256" in result["error"]["message"]
        )
        assert not (tmp_vault / rel_path).exists()

    @pytest.mark.asyncio
    async def test_heading_not_found_returns_error_without_write(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/missing-heading.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Present\n\nBody.\n",
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Absent",
            new_content="Replacement.",
            expected_hash=hash_text(original_raw),
        )

        assert result["error"]["type"] == "ValueError"
        assert result["error"]["message"] == "heading not found; nothing to patch"
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_ambiguous_heading_can_be_disambiguated_by_level(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/ambiguous-level.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Foo\n\nOuter.\n\n### Foo\n\nInner.\n",
        )

        ambiguous = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Foo",
            new_content="Replacement.",
            expected_hash=hash_text(original_raw),
        )

        assert ambiguous["error"]["type"] == "ValueError"
        assert ambiguous["error"]["message"] == (
            "heading is ambiguous (2 matches); pass heading_level"
        )
        assert target.read_text(encoding="utf-8") == original_raw

        patched = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Foo",
            new_content="Inner replacement.",
            expected_hash=hash_text(original_raw),
            heading_level=3,
        )

        assert patched["patched"] == {"rel_path": rel_path, "heading": "Foo", "level": 3}
        _metadata, new_body = parse(target.read_text(encoding="utf-8"))
        assert new_body.endswith("### Foo\n\nInner replacement.")

    @pytest.mark.asyncio
    async def test_same_level_ambiguous_heading_still_errors_with_level(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/ambiguous-same-level.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Foo\n\nFirst.\n\n## Foo\n\nSecond.\n",
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Foo",
            new_content="Replacement.",
            expected_hash=hash_text(original_raw),
            heading_level=2,
        )

        assert result["error"]["type"] == "ValueError"
        assert result["error"]["message"] == "heading is ambiguous (2 matches); pass heading_level"
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_nested_subsections_are_part_of_target_section(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/nested-patch.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            (
                "# Journaled memory\n\n"
                "## Target\n\n"
                "Old target.\n\n"
                "### Sub\n\n"
                "Old sub.\n\n"
                "## Next\n\n"
                "Next block.\n"
            ),
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Target",
            new_content="Replacement.",
            expected_hash=hash_text(original_raw),
        )

        assert result["patched"]["level"] == 2
        _metadata, new_body = parse(target.read_text(encoding="utf-8"))
        assert "### Sub" not in new_body
        assert new_body == (
            "# Journaled memory\n\n## Target\n\nReplacement.\n\n## Next\n\nNext block."
        )

    @pytest.mark.asyncio
    async def test_last_section_replaces_to_eof(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/last-section.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Tail\n\nOld tail.\n",
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Tail",
            new_content="New tail.",
            expected_hash=hash_text(original_raw),
        )

        assert result["patched"] == {"rel_path": rel_path, "heading": "Tail", "level": 2}
        _metadata, new_body = parse(target.read_text(encoding="utf-8"))
        assert new_body == "# Journaled memory\n\n## Tail\n\nNew tail."

    @pytest.mark.asyncio
    async def test_empty_new_content_returns_error_without_write(
        self, writable_app: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/empty-patch.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Target\n\nOriginal.\n",
        )

        result = await _patch_note_section_impl(
            writable_app,
            rel_path=rel_path,
            heading="Target",
            new_content="   \n",
            expected_hash=hash_text(original_raw),
        )

        assert result["error"]["type"] == "ValueError"
        assert result["error"]["message"] == "new_content must not be empty"
        assert target.read_text(encoding="utf-8") == original_raw

    @pytest.mark.asyncio
    async def test_writes_off_returns_clear_error_and_leaves_file_intact(
        self, app_with_open_store: DatacronApp, tmp_vault: Path
    ) -> None:
        from datacron.mcp.tools import _patch_note_section_impl

        rel_path = "_memory/facts/patch-writes-off.md"
        target, original_raw = _write_memory_note(
            tmp_vault,
            rel_path,
            "# Journaled memory\n\n## Target\n\nProtected.\n",
        )

        result = await _patch_note_section_impl(
            app_with_open_store,
            rel_path=rel_path,
            heading="Target",
            new_content="Denied.",
            expected_hash=hash_text(original_raw),
        )

        assert result["error"]["type"] == "PathConfinementError"
        assert "writes disabled — set DATACRON_WRITE_PATHS" in result["error"]["message"]
        assert target.read_text(encoding="utf-8") == original_raw


class TestAudit:
    """Audit lines go through the QueueListener → file handler, so caplog
    (which intercepts at the root) doesn't see them. The test reads the
    daily log file the FileLogger fixture has redirected to tmp_path."""

    @pytest.mark.asyncio
    async def test_list_notes_emits_audit_line(self, app: DatacronApp, tmp_path: Path) -> None:
        from datacron.core.logger import configure_logging, get_logger, shutdown_logging
        from datacron.mcp.tools import _list_notes_impl

        configure_logging(app.settings)
        # Re-resolve the logger so the QueueListener is wired before the call.
        get_logger("mcp.tools").info("warmup")
        await _list_notes_impl(app, folder=None, tags=None, limit=5)
        shutdown_logging()

        log_files = list((tmp_path / "logs").glob("datacron_*.log"))
        assert log_files, "expected at least one log file under DATACRON_LOG_DIR"
        contents = log_files[0].read_text(encoding="utf-8")
        assert "AUDIT tool=list_notes" in contents

    @pytest.mark.asyncio
    async def test_get_note_emits_audit_line(self, app: DatacronApp, tmp_path: Path) -> None:
        from datacron.core.logger import configure_logging, get_logger, shutdown_logging
        from datacron.mcp.tools import _get_note_impl

        configure_logging(app.settings)
        get_logger("mcp.tools").info("warmup")
        await _get_note_impl(app, id_or_path="welcome.md", fmt="full")
        shutdown_logging()

        log_files = list((tmp_path / "logs").glob("datacron_*.log"))
        assert log_files
        contents = log_files[0].read_text(encoding="utf-8")
        assert "AUDIT tool=get_note" in contents
