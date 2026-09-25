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
"""Search, regex and backlink reads that must survive out-of-band vault edits."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import pytest

from datacron.core.config import Settings
from datacron.core.paths import sidecar_index_db
from datacron.indexing.ripgrep import ripgrep_available
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools.retrieval import SEARCH_INDEX_STALE_CODE
from datacron.mcp.tools.search import (
    _get_backlinks_impl,
    _search_regex_impl,
    _search_text_impl,
)

_THROTTLE_SECONDS: Final[float] = 3600.0
_LIMIT: Final[int] = 10
_CHAIN_NOTES: Final[int] = 40
_ALPHA_ID: Final[str] = "01J00000000000000000000A01"
_BETA_ID: Final[str] = "01J00000000000000000000B01"
_UPPER_ID: Final[str] = "01J00000000000000000000C01"
_ALPHA: Final[str] = f"---\nid: {_ALPHA_ID}\n---\n# Alpha\n\nzebra apple\n\nSee [[Beta]]\n"
_BETA: Final[str] = f"---\nid: {_BETA_ID}\n---\n# Beta\n\nzebra banana\n\nSee [[Alpha]]\n"
_ALPHA_EDITED: Final[str] = _ALPHA.replace("zebra apple", "zebra apple and more\n\nnew paragraph")

AppFactory = Callable[..., Awaitable[DatacronApp]]


@pytest.fixture
async def app_factory(tmp_path: Path) -> AsyncIterator[AppFactory]:
    """Build apps over one vault; every opened store is closed at teardown."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "alpha.md").write_text(_ALPHA, encoding="utf-8")
    (vault / "beta.md").write_text(_BETA, encoding="utf-8")
    opened: list[DatacronApp] = []

    async def make(*, read_only: bool = False) -> DatacronApp:
        settings = Settings(
            vault_root=vault,
            read_paths=[vault],
            write_paths=[vault],
            read_only=read_only,
            repair_min_interval_seconds=_THROTTLE_SECONDS,
        )
        app = build_app(settings=settings, vault_root=vault)
        await app.store.open(sidecar_index_db(vault), read_only=read_only)
        opened.append(app)
        return app

    try:
        yield make
    finally:
        for app in opened:
            await app.store.close()


def _edit_out_of_band(vault: Path, *, keep_mtime: bool = False) -> None:
    """Rewrite alpha.md the way an external editor would, inside the throttle window."""
    path = vault / "alpha.md"
    before = path.stat()
    path.write_text(_ALPHA_EDITED, encoding="utf-8")
    if keep_mtime:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _paths(payload: dict[str, Any]) -> set[str]:
    assert "error" not in payload, payload
    return {row.get("note_rel_path") or row["source_note_rel_path"] for row in payload["results"]}


class TestStaleHitsAfterOutOfBandEdit:
    async def test_search_text_serves_other_notes_then_heals(self, app_factory: AppFactory) -> None:
        app = await app_factory()
        assert _paths(await _search_text_impl(app, query="zebra", limit=_LIMIT)) == {
            "alpha.md",
            "beta.md",
        }
        _edit_out_of_band(app.vault_root)

        first = await _search_text_impl(app, query="zebra", limit=_LIMIT)
        assert _paths(first) == {"beta.md"}
        assert app.repair_state.stale_note_paths == {"alpha.md"}

        # The next read sweeps at once despite the throttle and serves the new bytes.
        healed = await _search_text_impl(app, query="paragraph", limit=_LIMIT)
        assert _paths(healed) == {"alpha.md"}
        assert app.repair_state.stale_note_paths == set()

    async def test_search_regex_and_backlinks_survive_the_edit(
        self, app_factory: AppFactory
    ) -> None:
        app = await app_factory()
        await _search_text_impl(app, query="zebra", limit=_LIMIT)
        _edit_out_of_band(app.vault_root)

        regex = await _search_regex_impl(app, pattern="zebra", glob=None, limit=_LIMIT)
        assert "beta.md" in _paths(regex)
        backlinks = await _get_backlinks_impl(app, target="Alpha", limit=_LIMIT)
        assert _paths(backlinks) == {"beta.md"}

    async def test_only_stale_hits_give_a_typed_retryable_refusal(
        self, app_factory: AppFactory
    ) -> None:
        app = await app_factory()
        await _search_text_impl(app, query="zebra", limit=_LIMIT)
        _edit_out_of_band(app.vault_root)

        refused = await _search_text_impl(app, query="apple", limit=_LIMIT)
        assert refused["error"]["code"] == SEARCH_INDEX_STALE_CODE
        assert "next_action" in refused["error"]
        retried = await _search_text_impl(app, query="apple", limit=_LIMIT)
        assert _paths(retried) == {"alpha.md"}

    async def test_an_edit_that_keeps_the_mtime_is_still_reindexed(
        self, app_factory: AppFactory
    ) -> None:
        app = await app_factory()
        await _search_text_impl(app, query="zebra", limit=_LIMIT)
        _edit_out_of_band(app.vault_root, keep_mtime=True)

        assert _paths(await _search_text_impl(app, query="zebra", limit=_LIMIT)) == {"beta.md"}
        assert _paths(await _search_text_impl(app, query="paragraph", limit=_LIMIT)) == {"alpha.md"}

    async def test_chunks_from_an_older_chunker_are_rechunked(
        self, app_factory: AppFactory
    ) -> None:
        # Same bytes, same mtime, same hash: only the stored chunks differ, as after
        # a chunker change. The hash comparison alone never re-chunked such a note.
        app = await app_factory()
        await _search_text_impl(app, query="zebra", limit=_LIMIT)
        path = app.vault_root / "alpha.md"
        note = await app.vault_reader.read_note(path)
        older = [
            chunk.model_copy(update={"wikilinks_out": ["Ghost"]})
            for chunk in app.chunker.chunk(note)
        ]
        await app.store.upsert_note(note, older, fs_mtime_ns=path.stat().st_mtime_ns)

        assert _paths(await _get_backlinks_impl(app, target="Beta", limit=_LIMIT)) == set()
        assert app.repair_state.stale_note_paths == set()
        assert _paths(await _search_text_impl(app, query="zebra", limit=_LIMIT)) == {"beta.md"}
        assert _paths(await _get_backlinks_impl(app, target="Beta", limit=_LIMIT)) == {"alpha.md"}

    async def test_read_only_server_serves_the_unchanged_notes(
        self, app_factory: AppFactory
    ) -> None:
        writer = await app_factory()
        await _search_text_impl(writer, query="zebra", limit=_LIMIT)
        await writer.store.close()
        _edit_out_of_band(writer.vault_root)

        reader = await app_factory(read_only=True)
        for _attempt in range(2):
            payload = await _search_text_impl(reader, query="zebra", limit=_LIMIT)
            assert _paths(payload) == {"beta.md"}
        refused = await _search_text_impl(reader, query="apple", limit=_LIMIT)
        assert refused["error"]["code"] == SEARCH_INDEX_STALE_CODE


@pytest.mark.skipif(not ripgrep_available(), reason="ripgrep is not installed")
class TestRegexExtensionCase:
    async def test_search_regex_reaches_an_upper_case_extension(
        self, app_factory: AppFactory
    ) -> None:
        app = await app_factory()
        upper = f"---\nid: {_UPPER_ID}\n---\n# Upper\n\nneedlexyz upper\n"
        (app.vault_root / "Upper.MD").write_text(upper, encoding="utf-8")
        text = await _search_text_impl(app, query="needlexyz", limit=_LIMIT)
        assert _paths(text) == {"Upper.MD"}
        regex = await _search_regex_impl(app, pattern="needlexyz", glob=None, limit=_LIMIT)
        assert _paths(regex) == {"Upper.MD"}


class TestBacklinkAdmissionCost:
    async def test_backlinks_admit_sources_not_every_linked_alias(
        self, app_factory: AppFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = await app_factory()
        for index in range(_CHAIN_NOTES):
            (app.vault_root / f"chain{index}.md").write_text(
                f"---\nid: 01J{index:023d}\n---\n# Chain {index}\n\nsee [[Chain {index + 1}]]\n",
                encoding="utf-8",
            )
        await _search_text_impl(app, query="zebra", limit=_LIMIT)
        calls: list[str] = []
        admit = app.scope.allows_note_rel_path

        def counting(rel_path: str) -> bool:
            calls.append(rel_path)
            return admit(rel_path)

        monkeypatch.setattr(app.scope, "allows_note_rel_path", counting)
        backlinks = await _get_backlinks_impl(app, target="Alpha", limit=_LIMIT)

        assert _paths(backlinks) == {"beta.md"}
        indexed_notes = _CHAIN_NOTES + 2
        # One admission per source note, plus the target's resolution and its one
        # admission in the scan; resolving every distinct alias through the scoped
        # reader added one more per linked note.
        assert len(calls) <= indexed_notes + 2
