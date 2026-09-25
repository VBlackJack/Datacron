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
"""Bounded costs of the read tools: query size, regex dialect, walks and realpaths."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest

from datacron.core.config import MAX_SEARCH_QUERY_CHARS, MAX_SEARCH_QUERY_TERMS, Settings
from datacron.core.paths import sidecar_index_db
from datacron.indexing.ripgrep import ripgrep_available
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools.read import _get_note_impl
from datacron.mcp.tools.search import (
    SEARCH_QUERY_TOO_LARGE_CODE,
    _search_regex_impl,
    _search_text_impl,
)

_THROTTLE_SECONDS: Final[float] = 3600.0
_LIMIT: Final[int] = 10
_NOTES: Final[int] = 12
_UNKNOWN_ULID: Final[str] = "01JZZZZZZZZZZZZZZZZZZZZZZZ"
# A read admits and reads a note with a handful of realpaths; the cold pass paid
# about thirteen per note before the walked pair was trusted.
_MAX_RESOLVES_PER_NOTE: Final[int] = 8


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(_NOTES):
        (vault / f"n{index}.md").write_text(
            f"---\nid: 01J{index:023d}\n---\n# N{index}\n\nEcole Zurich word{index}\n",
            encoding="utf-8",
        )
    settings = Settings(
        vault_root=vault,
        read_paths=[vault],
        write_paths=[vault],
        repair_min_interval_seconds=_THROTTLE_SECONDS,
    )
    built = build_app(settings=settings, vault_root=vault)
    await built.store.open(sidecar_index_db(vault))
    try:
        yield built
    finally:
        await built.store.close()


class TestQueryBounds:
    async def test_an_oversized_query_is_refused_without_echo(self, app: DatacronApp) -> None:
        query = "x" * (MAX_SEARCH_QUERY_CHARS + 1)
        payload = await _search_text_impl(app, query=query, limit=_LIMIT)
        assert payload["error"]["code"] == SEARCH_QUERY_TOO_LARGE_CODE
        assert len(json.dumps(payload)) < MAX_SEARCH_QUERY_CHARS

    async def test_too_many_terms_are_refused(self, app: DatacronApp) -> None:
        refused = " ".join(f"w{index}" for index in range(MAX_SEARCH_QUERY_TERMS + 1))
        payload = await _search_text_impl(app, query=refused, limit=_LIMIT)
        assert payload["error"]["code"] == SEARCH_QUERY_TOO_LARGE_CODE
        accepted = " ".join(f"w{index}" for index in range(MAX_SEARCH_QUERY_TERMS))
        assert "error" not in await _search_text_impl(app, query=accepted, limit=_LIMIT)


@pytest.mark.skipif(not ripgrep_available(), reason="ripgrep is not installed")
class TestRegexDialect:
    async def test_ripgrep_syntax_is_not_judged_by_python(self, app: DatacronApp) -> None:
        payload = await _search_regex_impl(app, pattern=r"\p{Lu}\w+", glob=None, limit=_LIMIT)
        assert "error" not in payload, payload
        assert payload["returned"] > 0

    async def test_ripgrep_still_rejects_a_broken_pattern(self, app: DatacronApp) -> None:
        payload = await _search_regex_impl(app, pattern="(", glob=None, limit=_LIMIT)
        assert "regex parse error" in payload["error"]["message"]


class TestUnknownIdentity:
    async def test_repeated_unknown_ulid_walks_the_vault_once(
        self, app: DatacronApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _search_text_impl(app, query="word1", limit=_LIMIT)
        walks: list[int] = []
        stat_notes = app.vault_reader.stat_notes

        async def counting() -> dict[str, tuple[Path, int]]:
            walks.append(1)
            return await stat_notes()

        monkeypatch.setattr(app.vault_reader, "stat_notes", counting)
        for _attempt in range(3):
            payload = await _get_note_impl(app, id_or_path=_UNKNOWN_ULID, fmt="map")
            assert "error" in payload
        # The first lookup walks, so a note written since the last sweep is still
        # found; repeating the same unknown ID inside the interval does not.
        assert len(walks) == 1


class TestColdIndexRealpaths:
    async def test_cold_pass_resolves_a_few_times_per_note(
        self, app: DatacronApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolves: list[int] = []
        resolve = Path.resolve

        def counting(self: Path, strict: bool = False) -> Path:
            resolves.append(1)
            return resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", counting)
        payload = await _search_text_impl(app, query="word1", limit=_LIMIT)
        monkeypatch.undo()
        assert "error" not in payload, payload
        assert len(resolves) <= _NOTES * _MAX_RESOLVES_PER_NOTE
