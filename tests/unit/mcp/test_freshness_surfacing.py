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
"""recall and session_context say whether each returned note is still verified."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.types import CallToolResult

from datacron.core.config import MIN_READ_RESPONSE_CHARS, Settings
from datacron.core.memory_protocol import CONTRACT_HASH
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tool_contract import GetNoteOutput
from datacron.mcp.tools import _create_note_ai_impl
from datacron.mcp.tools import essential as essential_module
from datacron.mcp.tools import read as read_module
from datacron.mcp.tools import session as session_module
from datacron.mcp.tools.essential import _recall_impl
from datacron.mcp.tools.read import _get_note_impl, _serialized_chars
from datacron.mcp.tools.session import session_context

_TODAY = date(2026, 10, 1)
_FACT_WINDOW = 30
_PROJECT_WINDOW = 90
_FRESH_FACT = "_memory/facts/2026-09-25-quasar-fresh.md"
_UNVERIFIED_FACT = "_memory/facts/2026-09-26-quasar-unverified.md"
_STALE_PROJECT = "_memory/projects/quasar-stale.md"
_UNTYPED = "_memory/misc/quasar-untyped.md"
_RULES: list[dict[str, Any]] = [
    {"tag": "memory/fact", "folder": "_memory/facts", "review_after_days": _FACT_WINDOW},
    {
        "tag": "memory/project",
        "folder": "_memory/projects",
        "review_after_days": _PROJECT_WINDOW,
    },
]
_POLICY: dict[str, Any] = {"scope": "_memory", "rules": _RULES}


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(essential_module, "utc_today", lambda: _TODAY)
    monkeypatch.setattr(session_module, "utc_today", lambda: _TODAY)
    monkeypatch.setattr(read_module, "utc_today", lambda: _TODAY)


@pytest.fixture
async def app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    async for opened in _open_app(tmp_vault, _POLICY):
        yield opened


@pytest.fixture
async def windowless_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    rules = [
        {key: value for key, value in rule.items() if key != "review_after_days"} for rule in _RULES
    ]
    async for opened in _open_app(tmp_vault, {**_POLICY, "rules": rules}):
        yield opened


@pytest.fixture
async def tight_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    async for opened in _open_app(
        tmp_vault, _POLICY, settings_overrides={"max_read_response_chars": MIN_READ_RESPONSE_CHARS}
    ):
        yield opened


async def _open_app(
    tmp_vault: Path,
    policy: dict[str, Any],
    settings_overrides: dict[str, Any] | None = None,
) -> AsyncIterator[DatacronApp]:
    config_path = tmp_vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {}
    if config_path.exists():
        document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    document["organization"] = policy
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    settings = Settings(
        read_paths=[tmp_vault],
        write_paths=[tmp_vault],
        vault_root=tmp_vault,
        max_result_count=20,
        max_result_tokens=8000,
        **(settings_overrides or {}),
    )
    store = SQLiteFTS5Store()
    await store.open(tmp_vault / ".datacron" / "index" / "datacron.db")
    try:
        opened = build_app(
            settings=settings, vault_root=tmp_vault, chunker=MarkdownChunker(), store=store
        )
        # An AI capture with no supplied date, as remember writes it, must read as
        # unverified: writing a note is not verifying it.
        for rel_path, tags, verified in (
            (_FRESH_FACT, ["memory/fact"], "2026-09-25"),
            (_UNVERIFIED_FACT, ["memory/fact"], None),
            (_STALE_PROJECT, ["memory/project"], "2026-06-01"),
            (_UNTYPED, ["kind/misc"], "2026-09-01"),
        ):
            created = await _create_note_ai_impl(
                opened,
                rel_path=rel_path,
                title="Quasar",
                body="# Quasar\n\n## Calibration\n\nQuasar calibration notes.\n",
                origin="ai",
                confidence="high",
                tags=tags,
                last_verified=verified,
            )
            assert "error" not in created, created
        yield opened
    finally:
        await store.close()


def _by_path(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["rel_path"]: item["freshness"] for item in items}


_EXPECTED = {
    _FRESH_FACT: {
        "status": "fresh",
        "rule_tag": "memory/fact",
        "review_after_days": _FACT_WINDOW,
        "last_verified": "2026-09-25",
        "age_days": 6,
    },
    _UNVERIFIED_FACT: {
        "status": "unverified",
        "rule_tag": "memory/fact",
        "review_after_days": _FACT_WINDOW,
        "last_verified": None,
        "age_days": None,
    },
    _STALE_PROJECT: {
        "status": "stale",
        "rule_tag": "memory/project",
        "review_after_days": _PROJECT_WINDOW,
        "last_verified": "2026-06-01",
        "age_days": 122,
    },
    # Status alone: every source pays for this field out of the response budget.
    _UNTYPED: {"status": "not_governed"},
}


async def test_recall_judges_each_result_against_the_window_of_its_type(
    app: DatacronApp,
) -> None:
    found = await _recall_impl(app, question="quasar calibration", limit=10)

    assert "error" not in found, found
    assert _by_path(found["results"]) == _EXPECTED


async def test_recall_reads_the_note_again_when_its_metadata_was_dropped_for_size(
    app: DatacronApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = {
        "title": "",
        "tags": [],
        "aliases": [],
        "frontmatter": {},
        "metadata_truncated": True,
    }
    monkeypatch.setattr(read_module, "_sanitized_note_metadata", lambda _app, _note: dict(dropped))

    found = await _recall_impl(app, question="quasar calibration", limit=10)

    assert "error" not in found, found
    assert _by_path(found["results"])[_STALE_PROJECT]["status"] == "stale"


async def test_session_context_sources_carry_the_same_verdict(app: DatacronApp) -> None:
    result = await session_context(
        app, note_paths=list(_EXPECTED), known_contract_hash=CONTRACT_HASH
    )

    assert "error" not in result, result
    assert _by_path(result["sources"]) == _EXPECTED


async def test_a_budget_refit_source_keeps_its_verdict(app: DatacronApp) -> None:
    """The single-source refit rebuilds the source; the verdict must survive it."""
    result = await session_context(
        app, note_paths=[_STALE_PROJECT], known_contract_hash=CONTRACT_HASH, max_tokens=None
    )
    assert "error" not in result, result
    roomy = result["sources"][0]["freshness"]

    shrunk = session_module._full_source(app, await _read(app, _STALE_PROJECT), 1)

    assert shrunk["freshness"] == roomy == _EXPECTED[_STALE_PROJECT]


async def _read(app: DatacronApp, rel_path: str) -> Any:
    return await app.vault_reader.read_note(app.scope.authorize_note_rel_path(rel_path))


async def test_a_vault_without_review_windows_gets_no_freshness_field(
    windowless_app: DatacronApp,
) -> None:
    """The field costs response budget; a vault that never asked for it pays nothing."""
    found = await _recall_impl(windowless_app, question="quasar calibration", limit=10)
    result = await session_context(
        windowless_app, note_paths=list(_EXPECTED), known_contract_hash=CONTRACT_HASH
    )

    assert "error" not in found, found
    assert "error" not in result, result
    assert found["results"]
    assert result["sources"]
    assert all("freshness" not in item for item in found["results"])
    assert all("freshness" not in item for item in result["sources"])


async def test_both_tools_tell_the_agent_to_reverify_a_source_that_is_not_fresh(
    app: DatacronApp,
) -> None:
    """The recall description carries the guidance for the essential profile."""
    tools = {tool.name: tool for tool in await create_server(app).list_tools()}
    guidance = (
        "freshness is stale, unverified or invalid before relying on it, then set last_verified"
    )

    assert guidance in (tools["session_context"].description or "")
    assert guidance in essential_module._RECALL_DESCRIPTION


async def _chunk_id(app: DatacronApp, rel_path: str) -> str:
    note = await _read(app, rel_path)
    chunks = await app.store.list_chunks_for_note(note.id)
    assert chunks
    return str(chunks[0].chunk_id)


def _note_requests(chunk_id: str) -> dict[str, dict[str, Any]]:
    return {
        "full": {"fmt": "full"},
        "map": {"fmt": "map"},
        "metadata": {"fmt": "metadata"},
        "section": {"fmt": "full", "heading_path": ["Quasar", "Calibration"]},
        "chunk": {"fmt": "chunk", "id_or_path": chunk_id},
    }


@pytest.mark.parametrize("kind", ["full", "map", "metadata", "section", "chunk"])
@pytest.mark.parametrize("rel_path", [_FRESH_FACT, _UNVERIFIED_FACT, _STALE_PROJECT, _UNTYPED])
async def test_get_note_carries_the_verdict_in_every_format(
    app: DatacronApp, kind: str, rel_path: str
) -> None:
    request = {"id_or_path": rel_path, **_note_requests(await _chunk_id(app, rel_path))[kind]}

    page = await _get_note_impl(app, **request)

    assert "error" not in page, page
    assert page["freshness"] == _EXPECTED[rel_path]


@pytest.mark.parametrize("kind", ["full", "map", "metadata", "section", "chunk"])
async def test_get_note_in_a_vault_without_windows_has_no_freshness_key(
    windowless_app: DatacronApp, kind: str
) -> None:
    chunk_id = await _chunk_id(windowless_app, _STALE_PROJECT)
    request = {"id_or_path": _STALE_PROJECT, **_note_requests(chunk_id)[kind]}

    page = await _get_note_impl(windowless_app, **request)

    assert "error" not in page, page
    assert "freshness" not in page


async def test_get_note_over_the_wire_returns_a_verdict_the_schema_accepts(
    app: DatacronApp,
) -> None:
    server = create_server(app)
    tools = await server.list_tools()
    schema = next(tool.output_schema for tool in tools if tool.name == "get_note")
    assert schema is not None
    chunk_id = await _chunk_id(app, _STALE_PROJECT)

    for arguments in (
        {"id_or_path": _STALE_PROJECT},
        {"id_or_path": _STALE_PROJECT, "format": "map"},
        {"id_or_path": _STALE_PROJECT, "format": "metadata"},
        {"id_or_path": chunk_id},
    ):
        result = await server.call_tool("get_note", arguments)
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None, arguments
        Draft202012Validator(schema).validate(result.structured_content)
        assert result.structured_content["freshness"] == _EXPECTED[_STALE_PROJECT], arguments


async def test_get_note_over_the_wire_omits_the_key_in_a_vault_without_windows(
    windowless_app: DatacronApp,
) -> None:
    result = await create_server(windowless_app).call_tool(
        "get_note", {"id_or_path": _STALE_PROJECT}
    )

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert "freshness" not in result.structured_content


async def test_get_note_description_announces_the_verdict(app: DatacronApp) -> None:
    tools = {tool.name: tool for tool in await create_server(app).list_tools()}

    assert "same freshness as recall" in (tools["get_note"].description or "")


@pytest.mark.parametrize("fmt", ["full", "map"])
async def test_get_note_budget_accounts_for_the_verdict_at_the_minimum_budget(
    tight_app: DatacronApp, fmt: str
) -> None:
    rel_path = "_memory/projects/quasar-long.md"
    sections = "".join(
        f"## Section {index}\n\nQuasar calibration line {index}.\n\n" for index in range(400)
    )
    body = f"# Quasar\n\n{sections}"
    created = await _create_note_ai_impl(
        tight_app,
        rel_path=rel_path,
        title="Quasar",
        body=body,
        origin="ai",
        confidence="high",
        tags=["memory/project"],
        last_verified="2026-06-01",
    )
    assert "error" not in created, created

    page = await _get_note_impl(tight_app, id_or_path=rel_path, fmt=fmt)

    assert "error" not in page, page
    assert page["truncated"] is True
    assert page["freshness"] == _EXPECTED[_STALE_PROJECT]
    assert _serialized_chars(page) <= MIN_READ_RESPONSE_CHARS


async def test_get_note_over_the_wire_accepts_a_status_only_verdict_and_a_section_read(
    app: DatacronApp,
) -> None:
    server = create_server(app)
    tools = await server.list_tools()
    schema = next(tool.output_schema for tool in tools if tool.name == "get_note")
    assert schema is not None

    for arguments, expected in (
        ({"id_or_path": _UNTYPED}, _EXPECTED[_UNTYPED]),
        (
            {"id_or_path": _STALE_PROJECT, "heading_path": ["Quasar", "Calibration"]},
            _EXPECTED[_STALE_PROJECT],
        ),
    ):
        result = await server.call_tool("get_note", arguments)
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None, arguments
        Draft202012Validator(schema).validate(result.structured_content)
        assert result.structured_content["freshness"] == expected, arguments


def test_the_size_estimate_does_not_count_a_null_freshness_key() -> None:
    payload: dict[str, Any] = {"format": "full", "id": "n1", "content": "text"}
    padded_keys = [key for key in GetNoteOutput.__annotations__ if key != "freshness"]
    expected = len(
        json.dumps({**dict.fromkeys(padded_keys), **payload}, indent=2, ensure_ascii=True)
    )

    assert _serialized_chars(payload) == expected
    assert _serialized_chars({**payload, "freshness": {"status": "fresh"}}) > expected


async def test_recall_reads_do_not_attach_a_verdict_of_their_own(app: DatacronApp) -> None:
    page = await _get_note_impl(app, id_or_path=_STALE_PROJECT, fmt="full", with_freshness=False)

    assert "error" not in page, page
    assert "freshness" not in page
