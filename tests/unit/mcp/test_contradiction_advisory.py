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
"""Isolation tests for the contradiction advisory MCP surface."""

from __future__ import annotations

from pathlib import Path

import pytest

import datacron.mcp.tools.advisory as advisory_module
from datacron.core.config import Settings
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import build_app
from datacron.mcp.tools.advisory import _contradiction_scan_impl
from datacron.mcp.tools.ops import _get_health_impl


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def test_candidates_do_not_mutate_vault_or_degrade_health(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    app = build_app(
        settings=Settings(read_paths=[vault], vault_root=vault),
        vault_root=vault,
        store=store,
    )
    try:
        before = _snapshot(vault)
        advisory = await _contradiction_scan_impl()
        after = _snapshot(vault)
        health = await _get_health_impl(app)
    finally:
        await store.close()

    assert advisory["candidate_count"] == 24
    assert before == after
    assert health["status"] == "healthy"
    assert "contradiction" not in health


async def test_advisory_failure_is_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replay() -> dict[str, object]:
        raise RuntimeError("broken frozen resource")

    monkeypatch.setattr(advisory_module, "build_advisory_report", fail_replay)

    result = await _contradiction_scan_impl()

    assert next(iter(result)) == "warning"
    assert result["available"] is False
    assert result["candidate_count"] == 0
    assert result["effects"]["writes"] == "none"
