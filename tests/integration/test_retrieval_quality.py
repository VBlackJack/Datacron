# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Versioned bilingual retrieval corpus through the public MCP tool boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.models import EvalQuestion
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.eval.harness import LocalEvalHarness, load_eval_questions
from datacron.mcp.server import build_app, create_server

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "retrieval_quality"


async def test_versioned_quality_corpus(tmp_path: Path) -> None:
    corpus = json.loads((_FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    for item in corpus:
        path = tmp_path / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize(item["frontmatter"], item["body"]), encoding="utf-8")
    config_path = sidecar_vault_config(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("excluded_folders: [private]\n", encoding="utf-8")
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], max_result_tokens=8000),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    server = create_server(app)

    async def search(query: str, limit: int) -> dict[str, Any]:
        result = await server.call_tool("search_text", {"query": query, "limit": limit})
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        assert isinstance(result.content[0], TextContent)
        return dict(json.loads(result.content[0].text))

    try:
        report = await LocalEvalHarness(tool_search=search).run(
            load_eval_questions(_FIXTURES / "questions.yaml"),
            app,
            render=False,
        )
        assert report.summary.question_count == 32
        assert report.summary.empty_accuracy == 1.0
        assert report.summary.forbidden_violation_rate == 0.0, [
            result.model_dump() for result in report.results if result.forbidden_violation
        ]
        assert report.summary.note_recall_at_k[5] >= 0.95, [
            result.model_dump()
            for result in report.results
            if result.empty_correct is None and result.recall_at_k[5] < 1.0
        ]
        assert report.summary.mrr >= 0.8
        assert report.summary.total_tokens_returned > 0
        assert report.summary.latency_p95_ms > 0
    finally:
        await app.store.close()


def test_negative_ground_truth_refuses_positive_expectations() -> None:
    with pytest.raises(ValueError, match="expected_empty"):
        EvalQuestion(id="bad", question="query", expected_empty=True, expected_paths=["note.md"])
