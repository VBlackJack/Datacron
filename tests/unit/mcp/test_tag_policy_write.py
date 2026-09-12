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
"""``create_note_ai`` refuses notes that break the vault's declared tag policy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from datacron.core.config import Settings
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import _create_note_ai_impl

_POLICY = {
    "scope": "_memory",
    "rules": [
        {"tag": "memory/fact", "folder": "_memory/facts", "naming": "{iso_date}-{slug}"},
        {"tag": "memory/decision", "folder": "_memory/decisions"},
    ],
    "tags": {
        "placement_namespace": "memory",
        "markers": ["memory/decision"],
        "subject_namespace": "project",
        "subjects": [{"tag": "project/heimdall", "aliases": ["heimdall"]}],
        "allowed_namespaces": ["meta"],
    },
}


def _declare_policy(vault: Path) -> None:
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {}
    if config_path.exists():
        document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    document["organization"] = _POLICY
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
async def writable_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    _declare_policy(tmp_vault)
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


async def _create(
    app: DatacronApp, rel_path: str, tags: list[str], body: str = "# Note\n"
) -> dict[str, Any]:
    return await _create_note_ai_impl(
        app,
        rel_path=rel_path,
        title="Note",
        body=body,
        origin="ai",
        confidence="high",
        tags=tags,
    )


async def test_bare_alias_is_refused_with_a_typed_error(
    writable_app: DatacronApp, tmp_vault: Path
) -> None:
    result = await _create(
        writable_app, "_memory/facts/2026-09-12-x.md", ["memory/fact", "heimdall"]
    )

    assert result["error"]["type"] == "TagPolicyError"
    assert result["error"]["code"] == "tag_policy_violation"
    assert "alias of the registered subject project/heimdall" in result["error"]["message"]
    assert not (tmp_vault / "_memory" / "facts" / "2026-09-12-x.md").exists()


async def test_inline_body_tag_counts_as_effective_tag(writable_app: DatacronApp) -> None:
    result = await _create(
        writable_app,
        "_memory/facts/2026-09-12-y.md",
        ["memory/fact"],
        body="# Note\n\nsee #topic/rdp\n",
    )

    assert result["error"]["code"] == "tag_policy_violation"
    assert "namespace 'topic' is not declared" in result["error"]["message"]


async def test_compliant_note_is_created(writable_app: DatacronApp) -> None:
    result = await _create(
        writable_app,
        "_memory/facts/2026-09-12-z.md",
        ["memory/fact", "memory/decision", "project/heimdall", "meta/workflow", "ssh"],
    )

    assert "error" not in result
    assert result["indexed"] is True


async def test_equivalent_path_spellings_cannot_escape_the_guard(
    writable_app: DatacronApp,
) -> None:
    for rel_path in ("./_memory/facts/2026-09-12-w.md", "_memory//facts/2026-09-12-w.md"):
        result = await _create(writable_app, rel_path, ["memory/fact", "heimdall"])

        assert result["error"]["code"] == "tag_policy_violation", rel_path


async def test_notes_outside_the_scope_are_not_judged(writable_app: DatacronApp) -> None:
    result = await _create(writable_app, "_drafts/2026-09-12-scratch.md", ["heimdall"])

    assert "error" not in result
