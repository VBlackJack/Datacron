# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.mcp.resources`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.indexing.chunker import MarkdownChunker
from datacron.mcp.resources import (
    _build_policy_active,
    _build_vault_info,
    _build_vault_map,
    _truncate_to_token_budget,
)
from datacron.mcp.server import DatacronApp, build_app


@pytest.fixture
def app(tmp_vault: Path) -> DatacronApp:
    settings = Settings(
        read_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_tokens=8000,
    )
    return build_app(settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker())


class TestVaultMap:
    @pytest.mark.asyncio
    async def test_lists_top_level_and_subfolders(self, app: DatacronApp) -> None:
        rendered = await _build_vault_map(app)
        assert "welcome.md" in rendered
        assert "important-note.md" in rendered
        assert "## subfolder/" in rendered
        assert "nested-thoughts.md" in rendered

    @pytest.mark.asyncio
    async def test_marks_important_notes(self, app: DatacronApp) -> None:
        rendered = await _build_vault_map(app)
        # `★` is appended to notes carrying `important: true`
        important_line = next(line for line in rendered.splitlines() if "important-note.md" in line)
        assert important_line.endswith("★") or "★" in important_line

    @pytest.mark.asyncio
    async def test_renders_titles(self, app: DatacronApp) -> None:
        rendered = await _build_vault_map(app)
        assert "Welcome to the Demo Vault" in rendered
        assert "Important Note" in rendered

    @pytest.mark.asyncio
    async def test_truncates_when_over_budget(self, tmp_vault: Path) -> None:
        tight = Settings(
            read_paths=[tmp_vault],
            vault_root=tmp_vault,
            max_result_tokens=20,
        )
        app = build_app(settings=tight, vault_root=tmp_vault, chunker=MarkdownChunker())
        rendered = await _build_vault_map(app)
        assert "vault map truncated" in rendered


class TestVaultInfo:
    @pytest.mark.asyncio
    async def test_returns_json_with_required_keys(self, app: DatacronApp) -> None:
        rendered = await _build_vault_info(app)
        info = json.loads(rendered)
        assert info["datacron_version"]
        assert info["vault_root"].endswith("vault")
        assert info["note_count"] == 6
        assert info["index"]["built"] is False
        assert info["limits"]["max_result_count"] == 20

    @pytest.mark.asyncio
    async def test_detects_initialized_vault(self, app: DatacronApp, tmp_vault: Path) -> None:
        sidecar = tmp_vault / ".datacron"
        sidecar.mkdir(exist_ok=True)
        (sidecar / "VAULT.yaml").write_text("vault_id: 01HQ\n", encoding="utf-8")
        rendered = await _build_vault_info(app)
        info = json.loads(rendered)
        assert info["vault_initialized"] is True
        assert info["vault_config"].endswith("VAULT.yaml")


class TestPolicyActive:
    def test_phase_zero_is_read_only(self) -> None:
        policy = json.loads(_build_policy_active())
        assert policy["mode"] == "read-only"
        assert policy["write_tools_enabled"] is False
        assert policy["write_paths"] == []
        assert "trust_categories" in policy
        assert set(policy["trust_categories"]) == {"auto-create", "review-patch", "dangerous"}


class TestTruncation:
    def test_short_text_unchanged(self) -> None:
        assert _truncate_to_token_budget("hello world", 100) == "hello world"

    def test_long_text_truncated_with_marker(self) -> None:
        long = "a" * 4000
        truncated = _truncate_to_token_budget(long, 10)
        assert "vault map truncated" in truncated
        assert len(truncated) < len(long)

    def test_truncation_marker_visible(self) -> None:
        truncated = _truncate_to_token_budget("x" * 100, 10)
        assert truncated.endswith("\n")
