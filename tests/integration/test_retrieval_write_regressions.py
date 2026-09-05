# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Behavioral regressions across real retrieval and durable-write boundaries."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.paths import sidecar_index_db
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.search import _search_regex_impl, _search_text_impl
from datacron.mcp.tools.write import _append_journal_impl

_NOTE_ID = "01J00000000000000000000091"
_OTHER_ID = "01J00000000000000000000092"
_SYNTHETIC_VALUE = "SyntheticAuditValue123"


@pytest.fixture
async def regression_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(
        vault_root=tmp_path,
        read_paths=[tmp_path],
        write_paths=[tmp_path],
        redact_secrets="all",
        repair_min_interval_seconds=0,
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        yield app
    finally:
        await app.store.close()


def _write(app: DatacronApp, body: str, *, eol: str = "\n", bom: str = "") -> Path:
    path = app.vault_root / "sample.md"
    raw = serialize({"id": _NOTE_ID, "title": "Sample"}, body)
    path.write_bytes((bom + raw.replace("\n", eol)).encode("utf-8"))
    return path


@pytest.mark.parametrize("query", ["password", "SyntheticAuditValue123", "AuditValue"])
@pytest.mark.parametrize("route", ["fts", "rg", "fallback"])
async def test_search_masks_secret_before_highlighting(
    regression_app: DatacronApp,
    query: str,
    route: str,
) -> None:
    app = regression_app
    if route == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    _write(app, f"# Sample\n\npassword: {_SYNTHETIC_VALUE}\n")
    if route == "fallback":
        app = replace(
            app,
            settings=app.settings.model_copy(update={"ripgrep_path": "datacron-missing-ripgrep"}),
        )
    if route == "fts":
        # FTS matches complete tokens, while regex also exercises an inner span.
        query = "password" if query == "AuditValue" else query
        result = await _search_text_impl(app, query=query, limit=10)
    else:
        result = await _search_regex_impl(app, pattern=query, glob=None, limit=10)
    assert result["returned"] == 1
    snippet = result["results"][0]["snippet"]
    assert _SYNTHETIC_VALUE not in snippet
    assert "[REDACTED]" in snippet


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
async def test_regex_returns_physical_line_and_correct_chunk(
    regression_app: DatacronApp,
    eol: str,
    bom: str,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    app = regression_app
    path = _write(app, "# Sample\n\nFirstMarker\n\n## Journal\n\nLastMarker\n", eol=eol, bom=bom)
    for marker in ("FirstMarker", "LastMarker"):
        result = await _search_regex_impl(app, pattern=marker, glob=None, limit=10)
        assert result["returned"] == 1
        row = result["results"][0]
        physical = next(
            i for i, line in enumerate(path.read_bytes().decode().splitlines(), 1) if marker in line
        )
        assert row["line_start"] <= physical <= row["line_end"]
        chunk = await app.store.get_chunk(row["chunk_id"])
        assert chunk is not None
        assert marker in chunk.content


@pytest.mark.parametrize("moved", [False, True])
async def test_indexed_identity_never_returns_replacement_note(
    regression_app: DatacronApp,
    moved: bool,
) -> None:
    app = regression_app
    path = _write(app, "# Original\n\nOriginal body\n")
    await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=False)
    if moved:
        path.rename(app.vault_root / "moved.md")
    path.write_bytes(serialize({"id": _OTHER_ID}, "# Replacement\n").encode())
    result = await _get_note_impl(app, id_or_path=_NOTE_ID, fmt="full")
    if moved:
        assert result["id"] == _NOTE_ID
        assert result["rel_path"] == "moved.md"
    else:
        assert result["error"]["type"] == "FileNotFoundError"


async def test_mcp_write_error_preserves_committed_hash_and_cas(
    regression_app: DatacronApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = regression_app
    path = _write(app, "# Sample\n\n## Journal\n\nInitial\n")
    before_hash = sha256_bytes(path.read_bytes())
    monkeypatch.setattr(app.store, "upsert_note", AsyncMock(side_effect=OSError("index fault")))
    server = create_server(app)
    result = await server.call_tool(
        "append_journal",
        {
            "rel_path": "sample.md",
            "heading": "Journal",
            "entry": "UniqueEntry",
            "expected_hash": before_hash,
        },
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    payload = json.loads(result.content[0].text)
    error = payload["error"]
    assert error["code"] == "committed_index_incomplete"
    assert error["committed"] is True
    assert error["indexed"] is False
    assert error["content_hash"] == sha256_bytes(path.read_bytes())
    assert "correlation_id" in error
    assert len(await app.vault_writer.list_operations()) == 1
    retry = await _append_journal_impl(
        app, rel_path="sample.md", heading="Journal", entry="UniqueEntry", expected_hash=before_hash
    )
    assert "hash mismatch" in retry["error"]["message"]
    assert path.read_text().count("UniqueEntry") == 1


@pytest.mark.parametrize(
    "tool",
    [
        "create_note_ai",
        "append_journal",
        "set_frontmatter",
        "patch_note_preamble",
        "patch_note_section",
        "rename_note_section",
        "delete_note_section",
        "revert_note",
    ],
)
async def test_all_ordinary_writes_report_post_commit_index_failure(
    regression_app: DatacronApp,
    tool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = regression_app
    path = _write(app, "Old preamble\n\n# Sample\n\n## Journal\n\nInitial\n")
    original_hash = sha256_bytes(path.read_bytes())
    prior = await _append_journal_impl(app, rel_path="sample.md", heading="Journal", entry="Prior")
    assert "error" not in prior
    arguments: dict[str, object] = {"rel_path": "sample.md"}
    if tool == "create_note_ai":
        arguments = {
            "rel_path": "created.md",
            "title": "Created",
            "body": "Created body",
            "origin": "human",
            "confidence": "high",
            "tags": ["memory/fact"],
        }
        path = app.vault_root / "created.md"
    elif tool == "append_journal":
        arguments.update(heading="Journal", entry="Next")
    elif tool == "set_frontmatter":
        arguments.update(confidence="low", invalidated_by=_OTHER_ID)
    elif tool == "patch_note_preamble":
        arguments.update(new_content="New preamble", expected_hash=prior["content_hash"])
    elif tool == "patch_note_section":
        arguments.update(heading="Journal", new_content="Replacement")
    elif tool == "rename_note_section":
        arguments.update(heading="Journal", new_heading="Renamed")
    elif tool == "delete_note_section":
        arguments.update(heading="Journal")
    else:
        arguments = {"note": "sample.md", "to_hash": original_hash}
    monkeypatch.setattr(app.store, "upsert_note", AsyncMock(side_effect=OSError("index fault")))
    arguments["request_id"] = "all-writers-replay"
    result = await create_server(app).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "committed_index_incomplete"
    assert error["content_hash"] == sha256_bytes(path.read_bytes())
    assert len(await app.vault_writer.list_operations()) == 2

    replay = await create_server(app).call_tool(tool, arguments)
    assert isinstance(replay, CallToolResult)
    assert not replay.is_error
    assert isinstance(replay.content[0], TextContent)
    receipt = json.loads(replay.content[0].text)
    assert receipt["replayed"] is True
    assert receipt["operation_id"] == error["operation_id"]
    assert receipt["content_hash"] == error["content_hash"]
    assert len(await app.vault_writer.list_operations()) == 2


@pytest.mark.parametrize("policy", ["all", "off"])
async def test_search_retains_safe_highlighting_and_respects_redaction_policy(
    regression_app: DatacronApp,
    policy: str,
) -> None:
    app = replace(
        regression_app,
        settings=regression_app.settings.model_copy(update={"redact_secrets": policy}),
    )
    _write(app, "# Sample\n\nOrdinary words\n")
    result = await _search_text_impl(app, query="Ordinary", limit=10)
    assert "**Ordinary**" in result["results"][0]["snippet"]
    _write(app, f"# Sample\n\npassword: {_SYNTHETIC_VALUE}\n")
    result = await _search_text_impl(app, query="password", limit=10)
    snippet = result["results"][0]["snippet"]
    assert (_SYNTHETIC_VALUE in snippet) is (policy == "off")


async def test_fts_redacts_when_excerpt_omits_the_secret_label(
    regression_app: DatacronApp,
) -> None:
    app = regression_app
    secret = "filler " * 80 + _SYNTHETIC_VALUE
    _write(app, f'# Sample\n\npassword: "{secret}"\n')
    result = await _search_text_impl(app, query=_SYNTHETIC_VALUE, limit=10)
    assert result["returned"] == 1
    assert _SYNTHETIC_VALUE not in result["results"][0]["snippet"]
    assert "[REDACTED]" in result["results"][0]["snippet"]


async def test_chunk_coordinates_ignore_identical_frontmatter_text(
    regression_app: DatacronApp,
) -> None:
    app = regression_app
    path = _write(app, "Sample", eol="\r\n")
    note = await app.vault_reader.read_note(path)
    chunks = app.chunker.chunk(note)
    assert len(chunks) == 1
    assert chunks[0].line_start == len(path.read_bytes().decode().splitlines())


async def test_restore_by_stale_identity_does_not_mutate_replacement(
    regression_app: DatacronApp,
) -> None:
    app = regression_app
    path = _write(app, "# Sample\n\n## Journal\n\nInitial\n")
    original_hash = sha256_bytes(path.read_bytes())
    await _append_journal_impl(app, rel_path="sample.md", heading="Journal", entry="Prior")
    replacement = serialize({"id": _OTHER_ID}, "# Replacement\n").encode()
    path.write_bytes(replacement)
    result = await create_server(app).call_tool(
        "revert_note",
        {
            "note": _NOTE_ID,
            "to_hash": original_hash,
            "expected_hash": sha256_bytes(replacement),
        },
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error
    assert path.read_bytes() == replacement
    assert len(await app.vault_writer.list_operations()) == 1
