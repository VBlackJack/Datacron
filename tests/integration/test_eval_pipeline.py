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
"""Integration coverage proving Eval v2 observes temporal MCP reranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.models import EvalPipeline, EvalQuestion
from datacron.eval.harness import LocalEvalHarness
from datacron.eval.transport import e2e_search_transport
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import build_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_CURRENT_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_OLD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_DISTRACTOR_IDS = (
    "01HQXR7K9YZ8M2N3PQRSTV4WX7",
    "01HQXR7K9YZ8M2N3PQRSTV4WX8",
    "01HQXR7K9YZ8M2N3PQRSTV4WX9",
    "01HQXR7K9YZ8M2N3PQRSTV4WXA",
)


def _write_note(
    vault: Path,
    rel_path: str,
    note_id: str,
    body: str,
    *,
    supersedes: list[str] | None = None,
) -> None:
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        serialize(
            {
                "id": note_id,
                "title": target.stem,
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "origin": "ai",
                "confidence": "high",
                "last_verified": "2026-07-16",
                "supersedes": supersedes or [],
                "tags": ["memory"],
            },
            f"# {target.stem}\n\n{body}\n",
        ),
        encoding="utf-8",
    )


async def test_tool_pipeline_separates_active_and_superseded_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "memory/current.md",
        _CURRENT_ID,
        "temporalrank temporalrank current answer",
        supersedes=[_OLD_ID],
    )
    _write_note(
        vault,
        "memory/old.md",
        _OLD_ID,
        "temporalrank temporalrank temporalrank temporalrank temporalrank old answer",
    )
    for index, note_id in enumerate(_DISTRACTOR_IDS):
        _write_note(
            vault,
            f"memory/filler-{index}.md",
            note_id,
            f"temporalrank filler answer {index}",
        )

    settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    app = build_app(
        settings=settings,
        vault_root=vault,
        chunker=MarkdownChunker(),
        store=store,
    )
    question = EvalQuestion(
        id="freshness",
        question="temporalrank",
        expected_paths=["memory/current.md"],
        forbidden_paths=["memory/old.md"],
    )
    try:
        notes = await app.vault_reader.list_notes()
        for note in notes:
            await app.store.upsert_note(note, app.chunker.chunk(note))

        store_report = await LocalEvalHarness().run(
            [question],
            app,
            k_values=(5,),
            pipeline=EvalPipeline.STORE,
            render=False,
        )
        tool_report = await LocalEvalHarness().run(
            [question],
            app,
            k_values=(5,),
            render=False,
        )
    finally:
        await store.close()

    assert store_report.results[0].retrieved_paths[0] == "memory/old.md"
    assert store_report.results[0].forbidden_violation is True
    assert tool_report.results[0].retrieved_paths[0] == "memory/current.md"
    assert tool_report.results[0].recall_at_k[5] == 1.0
    assert tool_report.results[0].forbidden_violation is False


async def test_e2e_transport_calls_search_text_over_stdio(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "memory/current.md", _CURRENT_ID, "transportneedle current answer")
    settings = Settings(
        read_paths=[vault],
        vault_root=vault,
        read_only=True,
        log_dir=tmp_path / "logs",
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    app = build_app(
        settings=settings,
        vault_root=vault,
        chunker=MarkdownChunker(),
        store=store,
    )
    try:
        note = (await app.vault_reader.list_notes())[0]
        await store.upsert_note(note, app.chunker.chunk(note))
    finally:
        await store.close()

    async with e2e_search_transport(vault, settings) as search:
        payload = await search("transportneedle", 5)

    assert payload["returned"] == 1
    assert payload["results"][0]["note_rel_path"] == "memory/current.md"
    assert payload["results"][0]["snippet"].startswith('<vault_content path="')
