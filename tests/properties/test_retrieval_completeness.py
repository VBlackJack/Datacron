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
"""Property guards for retrieval completeness and index convergence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _get_backlinks_impl,
    _get_note_impl,
    _search_regex_impl,
    _search_text_impl,
)

pytestmark = pytest.mark.invariants


def _write_note(vault: Path, rel_path: str, note_id: str, title: str, body: str) -> Path:
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialize({"id": note_id, "title": title}, body).encode("utf-8"))
    return target


async def _open_app(vault: Path) -> tuple[DatacronApp, SQLiteFTS5Store]:
    settings = Settings(
        read_paths=[vault],
        vault_root=vault,
        max_result_count=100,
        max_result_tokens=100_000,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    return build_app(settings=settings, vault_root=vault, store=store), store


@pytest.mark.parametrize(
    ("query", "body", "tool"),
    [
        ("WORLDLINE0004", "Exact identifier WORLDLINE0004 is present.", "text"),
        (r"G:\_DATA\knowledge", r"Path G:\_DATA\knowledge is present.", "text"),
        (
            "8ec9a00bfd09b3190ac6b22251dbb1aa95a0579d",
            "Hash 8ec9a00bfd09b3190ac6b22251dbb1aa95a0579d.",
            "text",
        ),
        ("module.monitoring-requirements", "Token module.monitoring-requirements.", "text"),
        ("durcissement", "Le Durcissement est documente.", "text"),
        ("service", "Plural substring services is present.", "regex"),
    ],
)
async def test_prop_04_term_recall(
    tmp_path: Path,
    query: str,
    body: str,
    tool: str,
) -> None:
    """PROP-04: text or regex retrieval returns every indexed literal category."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel_path = "facts/recall.md"
    _write_note(
        vault,
        rel_path,
        "01J00000000000000000000011",
        "Recall property",
        f"# Recall property\n\n{body}\n",
    )
    app, store = await _open_app(vault)
    try:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        if tool == "text":
            result = await _search_text_impl(
                app,
                query=query,
                limit=100,
                include_superseded=True,
            )
        else:
            result = await _search_regex_impl(
                app,
                pattern=re.escape(query),
                glob="*.md",
                limit=100,
            )
    finally:
        await store.close()

    assert "error" not in result
    assert rel_path in {item["note_rel_path"] for item in result["results"]}


async def test_prop_14_delete_propagation(tmp_path: Path) -> None:
    """PROP-14: deletion removes reads/search hits and preserves broken backlink evidence."""
    vault = tmp_path / "vault"
    vault.mkdir()
    target_id = "01J00000000000000000000012"
    source_id = "01J00000000000000000000013"
    target_rel = "facts/delete-target.md"
    source_rel = "facts/delete-source.md"
    target = _write_note(
        vault,
        target_rel,
        target_id,
        "Delete target",
        "# Delete target\n\ndeletepropagationtoken\n",
    )
    _write_note(
        vault,
        source_rel,
        source_id,
        "Delete source",
        f"# Delete source\n\nReferences [[{target_id}]].\n",
    )
    app, store = await _open_app(vault)
    try:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        target.unlink()
        search = await _search_text_impl(
            app,
            query="deletepropagationtoken",
            limit=100,
            include_superseded=True,
        )
        fetched = await _get_note_impl(app, id_or_path=target_rel, fmt="full")
        backlinks = await _get_backlinks_impl(app, target=target_id, limit=100)
    finally:
        await store.close()

    assert search.get("index_repair", {}).get("deleted_notes") == 1
    assert target_rel not in {item["note_rel_path"] for item in search["results"]}
    assert fetched["error"]["type"] == "FileNotFoundError"
    assert backlinks["resolved_note_id"] == target_id
    assert source_rel in {item["source_note_rel_path"] for item in backlinks["results"]}


async def _logical_chunk_snapshot(store: SQLiteFTS5Store) -> list[dict[str, Any]]:
    rows = [chunk.model_dump(mode="json") async for chunk in store.iter_all_chunks()]
    return sorted(rows, key=lambda row: str(row["chunk_id"]))


async def test_prop_15_reindex_determinism_and_ground_truth(tmp_path: Path) -> None:
    """PROP-15: repeated reconciliation is stable and matches literal ground truth."""
    vault = tmp_path / "vault"
    vault.mkdir()
    literal_term = "deterministicgroundtruth"
    expected_paths = {"facts/one.md", "facts/two.md"}
    _write_note(
        vault,
        "facts/one.md",
        "01J00000000000000000000014",
        "One",
        f"# One\n\n{literal_term} alpha.\n",
    )
    _write_note(
        vault,
        "facts/two.md",
        "01J00000000000000000000015",
        "Two",
        f"# Two\n\n{literal_term} beta.\n",
    )
    _write_note(
        vault,
        "facts/other.md",
        "01J00000000000000000000016",
        "Other",
        "# Other\n\nNo target literal.\n",
    )
    app, store = await _open_app(vault)
    try:
        first = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        first_notes = await store.list_indexed_notes_with_mtime()
        first_chunks = await _logical_chunk_snapshot(store)
        first_results = await store.search(literal_term, limit=100)

        second = await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        second_notes = await store.list_indexed_notes_with_mtime()
        second_chunks = await _logical_chunk_snapshot(store)
        second_results = await store.search(literal_term, limit=100)
    finally:
        await store.close()

    first_paths = {result.chunk.note_rel_path for result in first_results}
    second_paths = {result.chunk.note_rel_path for result in second_results}
    literal_ground_truth = {
        path.relative_to(vault).as_posix()
        for path in vault.rglob("*.md")
        if literal_term in path.read_text(encoding="utf-8")
    }
    assert first["reindexed_notes"] == 3
    assert second["reindexed_notes"] == 0
    assert first_notes == second_notes
    assert first_chunks == second_chunks
    assert first_paths == second_paths == literal_ground_truth == expected_paths
