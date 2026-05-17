# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.mcp.tools`."""

from __future__ import annotations

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.indexing.chunker import MarkdownChunker
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
    """Same as ``app`` but with a tiny result ceiling to exercise truncation."""
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=3,
        max_result_tokens=50,
    )
    return build_app(settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker())


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
        # estimated_tokens is capped at max_result_tokens (50)
        assert result["estimated_tokens"] <= 50

    @pytest.mark.asyncio
    async def test_full_accepts_ulid(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        # Build the id by reading once, then re-querying by id
        first = await _get_note_impl(app, id_or_path="welcome.md", fmt="full")
        by_id = await _get_note_impl(app, id_or_path=first["id"], fmt="full")
        assert by_id["rel_path"] == "welcome.md"
        assert by_id["id"] == first["id"]

    @pytest.mark.asyncio
    async def test_unknown_ulid_returns_error(self, app: DatacronApp) -> None:
        from datacron.mcp.tools import _get_note_impl

        bogus = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        result = await _get_note_impl(app, id_or_path=bogus, fmt="full")
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
