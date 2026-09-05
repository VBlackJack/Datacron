# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Real retrieval oracles for note-wide secrets and regex stream boundaries."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import sidecar_index_db
from datacron.core.security import SecretRedactor
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.search import _search_regex_impl, _search_text_impl

_ID = "01J00000000000000000000091"
_MARKER = "SyntheticMaterialOnly"


@pytest.fixture
async def boundary_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    app = build_app(
        settings=Settings(
            vault_root=tmp_path,
            read_paths=[tmp_path],
            write_paths=[tmp_path],
            repair_min_interval_seconds=0,
            redact_secrets="all",
        ),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        yield app
    finally:
        await app.store.close()


async def _index(app: DatacronApp, body: str, title: str = "Example") -> bytes:
    data = serialize({"id": _ID, "title": title}, body).encode()
    (app.vault_root / "note.md").write_bytes(data)
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    return data


async def _mcp(app: DatacronApp, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await create_server(app).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    return dict(json.loads(result.content[0].text))


@pytest.mark.parametrize("route", ["chunk", "fts", "rg", "fallback"])
@pytest.mark.parametrize("custom", [False, True])
async def test_split_secret_is_masked_on_every_route(
    boundary_app: DatacronApp, route: str, custom: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = boundary_app
    if route == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    if route == "fallback":
        monkeypatch.setenv("DATACRON_RIPGREP_PATH", "missing-datacron-ripgrep")
    opening, closing = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    if custom:
        opening, closing = "SensitiveStart", "SensitiveEnd"
        app = replace(app, secret_redactor=SecretRedactor([r"(?s)SensitiveStart.*?SensitiveEnd"]))
    body = (
        "# Example\n\n```text\n"
        + opening
        + "\n"
        + (_MARKER + "\n") * 500
        + closing
        + "\n```\n\nUnrelatedPublicMarker\n"
    )
    before = await _index(app, body)
    full = await _get_note_impl(app, id_or_path="note.md", fmt="full")
    assert _MARKER not in full["content"]
    if route == "chunk":
        for chunk in await app.store.list_chunks_for_note(_ID):
            result = await _get_note_impl(app, id_or_path=chunk.chunk_id, fmt="chunk")
            assert _MARKER not in result["content"]
            assert result["note_content_hash"] == sha256_bytes(before)
    else:
        tool = "search_text" if route == "fts" else "search_regex"
        arguments = {"query" if route == "fts" else "pattern": _MARKER, "limit": 10}
        result = await _mcp(app, tool, arguments)
        assert result["returned"] > 0
        assert _MARKER not in json.dumps(result["results"])
    public = await _search_text_impl(app, query="UnrelatedPublicMarker", limit=1)
    assert "UnrelatedPublicMarker" in public["results"][0]["snippet"]
    assert (app.vault_root / "note.md").read_bytes() == before


@pytest.mark.parametrize("length", [45000, 198000])
@pytest.mark.parametrize("character", ["a", "é"])
async def test_long_regex_line_keeps_match_and_bounds_output(
    boundary_app: DatacronApp, length: int, character: str
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    app = boundary_app
    await _index(app, "# Example\n\n" + character * length + " TargetMarker\n")
    result = await _mcp(app, "search_regex", {"pattern": "TargetMarker", "limit": 1})
    assert "error" not in result
    assert result["returned"] == 1
    assert "TargetMarker" in result["results"][0]["snippet"]
    assert (
        len(json.dumps(result["results"], ensure_ascii=True, indent=2))
        <= app.settings.max_result_tokens * 4
    )
    assert result["truncated_for_tokens"] is True


async def test_frontmatter_does_not_consume_result_limit(boundary_app: DatacronApp) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    app = boundary_app
    await _index(app, "# Heading\n\nTargetMarker in body.\n", title="TargetMarker")
    result = await _search_regex_impl(app, pattern="TargetMarker", glob="note.md", limit=1)
    assert result["returned"] == 1
    assert "in body" in result["results"][0]["snippet"]


async def test_large_frame_refusal_is_actionable_and_server_recovers(
    boundary_app: DatacronApp,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    app = replace(
        boundary_app,
        settings=boundary_app.settings.model_copy(update={"regex_max_frame_bytes": 1024}),
    )
    await _index(app, "# Example\n\n" + "x" * 5000 + " TargetMarker\n")
    failed = await _mcp(app, "search_regex", {"pattern": "TargetMarker", "limit": 1})
    assert failed["error"]["code"] == "regex_frame_too_large"
    assert "DATACRON_REGEX_MAX_FRAME_BYTES" in failed["error"]["message"]
    await _index(app, "# Example\n\nTargetMarker\n")
    recovered = await _mcp(app, "search_regex", {"pattern": "TargetMarker", "limit": 1})
    assert recovered["returned"] == 1


async def test_changed_parent_cannot_unmask_stale_indexed_secret(
    boundary_app: DatacronApp,
) -> None:
    app = boundary_app
    await _index(
        app,
        "# Example\n\n-----BEGIN PRIVATE KEY-----\n"
        + (_MARKER + "\n") * 500
        + "-----END PRIVATE KEY-----\n",
    )
    # Freeze read repair after replacing the live source with innocuous content.
    (app.vault_root / "note.md").write_bytes(serialize({"id": _ID}, "# Public\n").encode())
    app = replace(
        app, settings=app.settings.model_copy(update={"repair_min_interval_seconds": 3600})
    )
    app.repair_state.last_sweep_completed_at = time.monotonic()
    result = await _search_text_impl(app, query=_MARKER, limit=1)
    assert "error" in result
    assert _MARKER not in json.dumps(result)
