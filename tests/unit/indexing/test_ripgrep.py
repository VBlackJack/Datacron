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
"""Unit tests for the async ripgrep wrapper."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from datacron.core.models import Chunk, Note
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.ripgrep import RipgrepError, RipgrepWrapper, _build_command

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]

_NOTE_ID_1 = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_NOTE_ID_2 = "01HQXR7K9YZ8M2N3PQRSTV4WX6"


@dataclass(frozen=True)
class _IndexedFixture:
    vault_root: Path
    store: SQLiteFTS5Store
    chunks: dict[str, Chunk]


class _AsyncBytes:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._index = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0)
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[bytes],
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.stdout = _AsyncBytes(stdout_lines)
        self.stderr = MagicMock()
        self.stderr.read = AsyncMock(return_value=stderr)
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.killed = False
        self.kill = MagicMock(side_effect=self._kill)
        self.wait = AsyncMock(side_effect=self._wait)

    def _kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def _wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode


class _PendingStderr:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = False
        self.task: asyncio.Task[Any] | None = None

    async def read(self) -> bytes:
        self.task = asyncio.current_task()
        self.started.set()
        never: asyncio.Future[bytes] = asyncio.Future()
        try:
            return await never
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished = True


class _LoggerSpy:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, tuple[object, ...]]] = []
        self.warning_calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.info_calls.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_calls.append((message, args))


@pytest.fixture
async def indexed(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> AsyncIterator[_IndexedFixture]:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    store = SQLiteFTS5Store()
    await store.open(vault_root / ".datacron" / "index" / "datacron.db")

    note_one = note_factory(
        id=_NOTE_ID_1,
        path=vault_root / "alpha.md",
        rel_path="alpha.md",
        title="Alpha",
    )
    note_two = note_factory(
        id=_NOTE_ID_2,
        path=vault_root / "folder" / "beta.md",
        rel_path="folder/beta.md",
        title="Beta",
    )
    chunks = {
        "alpha_intro": chunk_factory(
            note=note_one,
            chunk_id=f"{note_one.id}::::0000",
            content="Alpha intro",
            line_start=1,
            line_end=3,
        ),
        "alpha_later": chunk_factory(
            note=note_one,
            chunk_id=f"{note_one.id}::::0001",
            content="Alpha later",
            ordinal=1,
            line_start=4,
            line_end=6,
        ),
        "beta": chunk_factory(
            note=note_two,
            chunk_id=f"{note_two.id}::::0000",
            content="Beta intro",
            line_start=1,
            line_end=4,
        ),
    }
    await store.upsert_note(note_one, [chunks["alpha_intro"], chunks["alpha_later"]])
    await store.upsert_note(note_two, [chunks["beta"]])

    try:
        yield _IndexedFixture(vault_root=vault_root, store=store, chunks=chunks)
    finally:
        await store.close()


def _json_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8") + b"\n"


def _begin(path: Path) -> bytes:
    return _json_line({"type": "begin", "data": {"path": {"text": str(path)}}})


def _match(path: Path, line_number: int, line: str, spans: list[tuple[int, int]]) -> bytes:
    return _json_line(
        {
            "type": "match",
            "data": {
                "path": {"text": str(path)},
                "lines": {"text": line},
                "line_number": line_number,
                "absolute_offset": 0,
                "submatches": [
                    {
                        "match": {"text": line.encode("utf-8")[start:end].decode("utf-8")},
                        "start": start,
                        "end": end,
                    }
                    for start, end in spans
                ],
            },
        }
    )


def _end(path: Path) -> bytes:
    return _json_line({"type": "end", "data": {"path": {"text": str(path)}, "stats": {}}})


def _install_process(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
) -> list[tuple[tuple[str, ...], dict[str, object]]]:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def _create(*args: str, **kwargs: object) -> _FakeProcess:
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    return calls


def test_build_command_inserts_separator_before_dash_pattern() -> None:
    vault_root = Path("/v")

    command = _build_command("rg", "-foo", vault_root, glob=None, limit=20)

    assert command == ["rg", "--json", "--max-count", "20", "--", "-foo", str(vault_root)]


def test_build_command_places_separator_after_glob_options() -> None:
    vault_root = Path("/v")

    command = _build_command("rg", "-foo", vault_root, glob="*.md", limit=20)
    separator_index = command.index("--")

    assert command[separator_index - 2 : separator_index] == ["--glob", "*.md"]
    assert command[separator_index + 1 :] == ["-foo", str(vault_root)]


async def test_happy_path_resolves_three_matches_across_two_files(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = indexed.vault_root / "alpha.md"
    beta = indexed.vault_root / "folder" / "beta.md"
    process = _FakeProcess(
        [
            _begin(alpha),
            _match(alpha, 2, "first kafka line\n", [(6, 11)]),
            _match(alpha, 5, "later kafka line\n", [(6, 11)]),
            _match(beta, 1, "beta kafka line\n", [(5, 10)]),
            _end(beta),
        ]
    )
    _install_process(monkeypatch, process)

    results = await RipgrepWrapper().search(
        "kafka", indexed.vault_root, limit=20, store=indexed.store
    )

    assert [result.chunk for result in results] == [
        indexed.chunks["alpha_intro"],
        indexed.chunks["alpha_later"],
        indexed.chunks["beta"],
    ]
    assert [result.snippet for result in results] == [
        "first **kafka** line",
        "later **kafka** line",
        "beta **kafka** line",
    ]
    assert [result.score for result in results] == [1.0, 0.5, pytest.approx(1.0 / 3.0)]


async def test_limit_enforcement_kills_process_after_limit(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = indexed.vault_root / "alpha.md"
    process = _FakeProcess([_match(alpha, 2, f"kafka {i}\n", [(0, 5)]) for i in range(10)])
    _install_process(monkeypatch, process)

    results = await RipgrepWrapper().search(
        "kafka", indexed.vault_root, limit=3, store=indexed.store
    )

    assert len(results) == 3
    assert process.killed is True
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once()


async def test_process_and_stderr_task_are_cleaned_up_when_collection_raises(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    process = _FakeProcess([])
    pending_stderr = _PendingStderr()
    process.stderr.read = pending_stderr.read
    _install_process(monkeypatch, process)

    async def _raise_after_stderr_task_starts(**_kwargs: object) -> tuple[list[object], bool]:
        await asyncio.sleep(0)
        raise RuntimeError("collect failed")

    monkeypatch.setattr(ripgrep_module, "_collect_results", _raise_after_stderr_task_starts)

    with pytest.raises(RuntimeError, match="collect failed"):
        await RipgrepWrapper().search("kafka", indexed.vault_root, store=indexed.store)

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once()
    assert process.returncode is not None
    assert pending_stderr.cancelled is True
    assert pending_stderr.finished is True
    assert pending_stderr.task is not None
    assert pending_stderr.task.done()


async def test_no_matches_exit_code_one_returns_empty(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess([], returncode=1)
    _install_process(monkeypatch, process)

    assert await RipgrepWrapper().search("missing", indexed.vault_root, store=indexed.store) == []
    assert process.killed is False


async def test_missing_binary_falls_back_to_indexed_regex_scan(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)

    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    results = await RipgrepWrapper().search(
        "later",
        indexed.vault_root,
        store=indexed.store,
        rg_path="missing-rg",
    )

    assert len(results) == 1
    assert results[0].chunk == indexed.chunks["alpha_later"]
    assert results[0].score == 1.0
    assert results[0].snippet == "Alpha **later**"
    assert any("falling back" in message for message, _args in logger.warning_calls)


async def test_fallback_honors_glob_limit_score_and_snippet(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    limited = await RipgrepWrapper().search(
        "intro",
        indexed.vault_root,
        limit=1,
        store=indexed.store,
        rg_path="missing-rg",
    )
    scoped = await RipgrepWrapper().search(
        "intro",
        indexed.vault_root,
        glob="folder/*.md",
        limit=5,
        store=indexed.store,
        rg_path="missing-rg",
    )

    assert [result.chunk for result in limited] == [indexed.chunks["alpha_intro"]]
    assert limited[0].score == 1.0
    assert limited[0].snippet == "Alpha **intro**"
    assert [result.chunk for result in scoped] == [indexed.chunks["beta"]]
    assert scoped[0].snippet == "Beta **intro**"


async def test_rg_error_exit_raises_typed_error_and_logs_warning(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)
    alpha = indexed.vault_root / "alpha.md"
    process = _FakeProcess(
        [_match(alpha, 2, "kafka\n", [(0, 5)])],
        returncode=2,
        stderr=b"regex parse error",
    )
    _install_process(monkeypatch, process)

    with pytest.raises(RipgrepError) as exc_info:
        await RipgrepWrapper().search("(", indexed.vault_root, store=indexed.store)

    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == "regex parse error"
    assert str(exc_info.value) == "ripgrep exited with status 2: regex parse error"
    assert logger.warning_calls
    assert logger.warning_calls[0][1][0] == 2
    assert logger.warning_calls[0][1][1] == "regex parse error"


async def test_match_outside_chunk_ranges_is_dropped_with_info_log(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)
    alpha = indexed.vault_root / "alpha.md"
    process = _FakeProcess([_match(alpha, 99, "kafka\n", [(0, 5)])])
    _install_process(monkeypatch, process)

    assert await RipgrepWrapper().search("kafka", indexed.vault_root, store=indexed.store) == []
    assert any("no chunk covers" in message for message, _args in logger.info_calls)


async def test_match_for_unindexed_file_is_dropped_with_info_log(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)
    unknown = indexed.vault_root / "unknown.md"
    process = _FakeProcess([_match(unknown, 1, "kafka\n", [(0, 5)])])
    _install_process(monkeypatch, process)

    assert await RipgrepWrapper().search("kafka", indexed.vault_root, store=indexed.store) == []
    assert any("no note_id mapping" in message for message, _args in logger.info_calls)


async def test_submatch_highlighting_wraps_each_submatch(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = indexed.vault_root / "alpha.md"
    process = _FakeProcess([_match(alpha, 2, "word and word\n", [(0, 4), (9, 13)])])
    _install_process(monkeypatch, process)

    results = await RipgrepWrapper().search("word", indexed.vault_root, store=indexed.store)

    assert results[0].snippet == "**word** and **word**"


async def test_glob_filter_is_passed_to_subprocess(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess([])
    calls = _install_process(monkeypatch, process)

    await RipgrepWrapper().search(
        "kafka",
        indexed.vault_root,
        glob="*.md",
        limit=7,
        store=indexed.store,
    )

    command = calls[0][0]
    assert command[:7] == ("rg", "--json", "--max-count", "7", "--glob", "*.md", "--")
    assert command[7] == "kafka"
    assert command[8] == str(indexed.vault_root)


async def test_invalid_utf8_json_line_is_skipped(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)
    alpha = indexed.vault_root / "alpha.md"
    process = _FakeProcess([b"\xff\n", _match(alpha, 2, "kafka\n", [(0, 5)])])
    _install_process(monkeypatch, process)

    results = await RipgrepWrapper().search("kafka", indexed.vault_root, store=indexed.store)

    assert len(results) == 1
    assert any("undecodable" in message for message, _args in logger.info_calls)
