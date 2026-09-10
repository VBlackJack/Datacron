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
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from datacron.core.config import DEFAULT_RIPGREP_PATH, REGEX_FALLBACK_SCAN_BATCH_CHUNKS
from datacron.core.models import Chunk, Note, SearchResult
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.ripgrep import (
    RegexFallbackError,
    RipgrepError,
    RipgrepOutputError,
    RipgrepWrapper,
    _build_command,
    _read_frames,
    _resolve_ripgrep_path,
)

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

    async def read(self, size: int) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        block, remaining = line[:size], line[size:]
        if remaining:
            self._lines[self._index] = remaining
        else:
            self._index += 1
        return block

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

    assert command == ["rg", "--json", "--", "-foo", "."]


def test_build_command_places_separator_after_glob_options() -> None:
    vault_root = Path("/v")

    command = _build_command("rg", "-foo", vault_root, glob="*.md", limit=20)
    separator_index = command.index("--")

    assert command[separator_index - 2 : separator_index] == ["--glob", "*.md"]
    assert command[separator_index + 1 :] == ["-foo", "."]


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


async def test_unadmitted_match_does_not_consume_limit(
    indexed: _IndexedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(
        [
            _match(indexed.vault_root / "alpha.md", 2, "kafka\n", [(0, 5)]),
            _match(indexed.vault_root / "folder/beta.md", 2, "kafka\n", [(0, 5)]),
        ]
    )
    _install_process(monkeypatch, process)
    results = await RipgrepWrapper().search(
        "kafka",
        indexed.vault_root,
        limit=1,
        store=indexed.store,
        admit=lambda path: path == "folder/beta.md",
    )
    assert [result.chunk for result in results] == [indexed.chunks["beta"]]


@pytest.mark.parametrize("overflow", [False, True])
async def test_frame_byte_ceiling_is_explicit(overflow: bool) -> None:
    frame = ('{"text":"' + "é" * 40000 + '"}\n').encode()
    reader = asyncio.StreamReader()
    reader.feed_data(frame)
    reader.feed_eof()
    if overflow:
        with pytest.raises(RipgrepOutputError, match="DATACRON_REGEX_MAX_FRAME_BYTES"):
            _ = [item async for item in _read_frames(reader, len(frame) - 1)]
    else:
        assert [item async for item in _read_frames(reader, len(frame))] == [frame]


async def test_frame_refusal_terminates_child(
    indexed: _IndexedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess([_match(indexed.vault_root / "alpha.md", 2, "kafka" * 1000, [(0, 5)])])
    _install_process(monkeypatch, process)
    with pytest.raises(RipgrepOutputError):
        await RipgrepWrapper().search(
            "kafka", indexed.vault_root, store=indexed.store, max_frame_bytes=100
        )
    process.kill.assert_called_once()
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


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+$", "(a*)*", "(a|a)*", "(a|aa)+", "(a|ab)*", "(a|a?)+"],
)
async def test_fallback_rejects_known_catastrophic_shapes(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    with pytest.raises(RegexFallbackError, match="potentially catastrophic pattern"):
        await RipgrepWrapper().search(
            pattern,
            indexed.vault_root,
            store=indexed.store,
            rg_path="missing-rg",
        )


@pytest.mark.parametrize(
    "pattern",
    ["DATACRON_WRITE_PATHS", "foo.*bar", r"\bnote\b", "(abc)+", "a|b"],
)
async def test_fallback_allows_supported_benign_shapes(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    results = await RipgrepWrapper().search(
        pattern,
        indexed.vault_root,
        store=indexed.store,
        rg_path="missing-rg",
    )

    assert isinstance(results, list)


async def test_fallback_enforces_global_timeout(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    def _slow_scan(*_args: object) -> list[SearchResult]:
        time.sleep(0.1)
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr(ripgrep_module, "_scan_indexed_chunks", _slow_scan)

    with pytest.raises(RegexFallbackError, match="advisory timeout"):
        await RipgrepWrapper().search(
            "safe-pattern",
            indexed.vault_root,
            store=indexed.store,
            rg_path="missing-rg",
            fallback_timeout_seconds=0.01,
        )


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
    assert command[:5] == ("rg", "--json", "--glob", "*.md", "--")
    assert command[5:] == ("kafka", ".")
    assert calls[0][1]["cwd"] == indexed.vault_root


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


def _missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the wrapper down the indexed fallback, as an absent rg does."""

    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)


def _forbid_index_reads(monkeypatch: pytest.MonkeyPatch, fixture: _IndexedFixture) -> None:
    """Make any index read an outright failure, to prove none happens."""

    def _boom() -> AsyncIterator[Chunk]:
        raise AssertionError("the index was read for a pattern that must be refused first")

    monkeypatch.setattr(fixture.store, "iter_all_chunks", _boom)


def _count_streamed(
    monkeypatch: pytest.MonkeyPatch,
    fixture: _IndexedFixture,
) -> list[str]:
    """Record every chunk the fallback pulls off the index, in order."""
    streamed: list[str] = []
    original = fixture.store.iter_all_chunks

    async def _counting() -> AsyncIterator[Chunk]:
        async for chunk in original():
            streamed.append(chunk.chunk_id)
            yield chunk

    monkeypatch.setattr(fixture.store, "iter_all_chunks", _counting)
    return streamed


async def test_catastrophic_pattern_is_refused_before_the_index_is_read(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_binary(monkeypatch)
    _forbid_index_reads(monkeypatch, indexed)

    with pytest.raises(RegexFallbackError, match="potentially catastrophic pattern"):
        await RipgrepWrapper().search(
            "(a+)+$",
            indexed.vault_root,
            store=indexed.store,
            rg_path="missing-rg",
        )


async def test_over_length_pattern_is_refused_before_the_index_is_read(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_binary(monkeypatch)
    _forbid_index_reads(monkeypatch, indexed)

    with pytest.raises(RegexFallbackError, match="exceeds 8 characters"):
        await RipgrepWrapper().search(
            "a" * 9,
            indexed.vault_root,
            store=indexed.store,
            rg_path="missing-rg",
            fallback_max_pattern_length=8,
        )


async def test_fallback_admits_a_note_once_and_only_after_a_body_match(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_binary(monkeypatch)
    asked: list[str] = []

    def _admit(rel_path: str) -> bool:
        asked.append(rel_path)
        return True

    results = await RipgrepWrapper().search(
        "Alpha",
        indexed.vault_root,
        limit=5,
        store=indexed.store,
        rg_path="missing-rg",
        admit=_admit,
    )

    # Both Alpha chunks match and share one note; Beta never matches.
    assert len(results) == 2
    assert asked == ["alpha.md"]


async def test_fallback_never_admits_a_chunk_excluded_by_glob(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_binary(monkeypatch)
    asked: list[str] = []

    def _admit(rel_path: str) -> bool:
        asked.append(rel_path)
        return True

    results = await RipgrepWrapper().search(
        "intro",
        indexed.vault_root,
        glob="folder/*.md",
        limit=5,
        store=indexed.store,
        rg_path="missing-rg",
        admit=_admit,
    )

    assert [result.chunk for result in results] == [indexed.chunks["beta"]]
    assert asked == ["folder/beta.md"]


async def test_fallback_timeout_message_reports_how_far_the_scan_got(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry message must report real progress, not a fixed apology.

    The stream stalls after a known number of chunks rather than sleeping for a
    measured interval, so the reported count is exact instead of timing-dependent.
    """
    _missing_binary(monkeypatch)
    stalled = list(indexed.chunks.values())

    async def _stalling() -> AsyncIterator[Chunk]:
        for chunk in stalled:
            yield chunk
        await asyncio.Event().wait()

    monkeypatch.setattr(indexed.store, "iter_all_chunks", _stalling)

    with pytest.raises(RegexFallbackError, match=rf"after scanning {len(stalled)} indexed chunks"):
        await RipgrepWrapper().search(
            "safe-pattern",
            indexed.vault_root,
            store=indexed.store,
            rg_path="missing-rg",
            fallback_timeout_seconds=0.05,
        )


async def test_unusable_binary_routes_to_the_fallback_like_an_absent_one(
    indexed: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datacron.indexing.ripgrep as ripgrep_module

    logger = _LoggerSpy()
    monkeypatch.setattr(ripgrep_module, "_LOGGER", logger)

    async def _create(*_args: str, **_kwargs: object) -> _FakeProcess:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    results = await RipgrepWrapper().search(
        "later",
        indexed.vault_root,
        store=indexed.store,
        rg_path="not-an-executable",
    )

    assert [result.chunk for result in results] == [indexed.chunks["alpha_later"]]
    assert any("falling back" in message for message, _args in logger.warning_calls)


def test_explicit_ripgrep_path_outranks_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATACRON_RIPGREP_PATH", "C:/from-environment/rg.exe")

    assert _resolve_ripgrep_path("C:/explicit/rg.exe") == "C:/explicit/rg.exe"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_environment_ripgrep_path_falls_through_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("DATACRON_RIPGREP_PATH", value)

    assert _resolve_ripgrep_path(None) == DEFAULT_RIPGREP_PATH


def test_environment_ripgrep_path_applies_when_no_argument_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATACRON_RIPGREP_PATH", "C:/from-environment/rg.exe")

    assert _resolve_ripgrep_path(None) == "C:/from-environment/rg.exe"


# Every other fallback test runs on three chunks, which is why the glob and the
# limit could both be unreachable in production while the suite stayed green.
# These two run wide enough to cross a scan batch boundary.
_SCALE_NOTE_COUNT = 4
_SCALE_CHUNKS_PER_NOTE = 400
_SCALE_CHUNK_COUNT = _SCALE_NOTE_COUNT * _SCALE_CHUNKS_PER_NOTE


@pytest.fixture
async def indexed_at_scale(
    tmp_path: Path,
    note_factory: NoteFactory,
    chunk_factory: ChunkFactory,
) -> AsyncIterator[_IndexedFixture]:
    """Index many chunks over few notes, the shape a real vault has."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    store = SQLiteFTS5Store()
    await store.open(vault_root / ".datacron" / "index" / "datacron.db")

    for note_index in range(_SCALE_NOTE_COUNT):
        rel_path = f"note-{note_index:03d}.md"
        note = note_factory(
            id=f"01HQXR7K9YZ8M2N3PQRSTV4W{note_index:02d}",
            path=vault_root / rel_path,
            rel_path=rel_path,
            title=f"Note {note_index}",
        )
        chunks = [
            chunk_factory(
                note=note,
                chunk_id=f"{note.id}::::{ordinal:04d}",
                content=f"needle in chunk {ordinal}",
                ordinal=ordinal,
            )
            for ordinal in range(_SCALE_CHUNKS_PER_NOTE)
        ]
        await store.upsert_note(note, chunks)

    try:
        yield _IndexedFixture(vault_root=vault_root, store=store, chunks={})
    finally:
        await store.close()


async def test_fallback_abandons_the_stream_once_the_limit_is_reached(
    indexed_at_scale: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cheap query must not pay for the whole index, nor admit whole notes."""
    _missing_binary(monkeypatch)
    streamed = _count_streamed(monkeypatch, indexed_at_scale)
    asked: list[str] = []

    def _admit(rel_path: str) -> bool:
        asked.append(rel_path)
        return True

    results = await RipgrepWrapper().search(
        "needle",
        indexed_at_scale.vault_root,
        limit=5,
        store=indexed_at_scale.store,
        rg_path="missing-rg",
        admit=_admit,
    )

    assert len(results) == 5
    assert len(streamed) < _SCALE_CHUNK_COUNT
    assert len(streamed) == REGEX_FALLBACK_SCAN_BATCH_CHUNKS
    assert asked == ["note-000.md"]


async def test_fallback_ranks_continuously_across_a_batch_boundary(
    indexed_at_scale: _IndexedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rank is global to the scan, not restarted for every worker batch."""
    _missing_binary(monkeypatch)
    limit = REGEX_FALLBACK_SCAN_BATCH_CHUNKS + 88

    results = await RipgrepWrapper().search(
        "needle",
        indexed_at_scale.vault_root,
        limit=limit,
        store=indexed_at_scale.store,
        rg_path="missing-rg",
    )

    assert len(results) == limit
    assert [result.score for result in results] == [1.0 / (1.0 + rank) for rank in range(limit)]
