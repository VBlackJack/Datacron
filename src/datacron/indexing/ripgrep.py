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
"""Async ripgrep wrapper with JSON output parsing."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import json
import os
import re
from asyncio.subprocess import PIPE
from pathlib import Path, PurePosixPath
from typing import Any, Final, final

from datacron.core.config import DEFAULT_RIPGREP_PATH
from datacron.core.logger import get_logger
from datacron.core.models import Chunk, SearchResult
from datacron.core.protocols import FTS5Store
from datacron.indexing.fts5_store import SQLiteFTS5Store

__all__ = ["RipgrepError", "RipgrepWrapper"]

_LOGGER = get_logger(__name__)
_RIPGREP_PATH_ENV: Final[str] = "DATACRON_RIPGREP_PATH"
_NO_MATCH_RETURN_CODE: Final[int] = 1


class RipgrepError(RuntimeError):
    """Raised when ripgrep exits with an error status."""

    def __init__(self, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        message = stderr.strip() or "(no stderr)"
        super().__init__(f"ripgrep exited with status {returncode}: {message}")


@final
class RipgrepWrapper:
    """Run ``rg --json`` and resolve matches to indexed chunks."""

    async def search(
        self,
        pattern: str,
        vault_root: Path,
        glob: str | None = None,
        limit: int = 20,
        store: FTS5Store | None = None,
        rg_path: str | None = None,
    ) -> list[SearchResult]:
        """Search with ripgrep, falling back to indexed chunks if the binary is absent.

        The fallback scans indexed chunk bodies only. That excludes frontmatter and
        depends on index freshness; MCP ``search_regex`` repairs the index before
        calling this wrapper.
        """
        if limit <= 0:
            return []
        if store is None:
            _LOGGER.info("ripgrep search skipped: no FTS5Store supplied for chunk resolution")
            return []

        resolved_rg_path = os.environ.get(_RIPGREP_PATH_ENV, rg_path or DEFAULT_RIPGREP_PATH)
        command = _build_command(resolved_rg_path, pattern, vault_root, glob, limit)
        try:
            proc = await asyncio.create_subprocess_exec(*command, stdout=PIPE, stderr=PIPE)
        except FileNotFoundError as exc:
            _LOGGER.warning(
                "ripgrep binary not found (%s); falling back to indexed Python regex scan: %s",
                resolved_rg_path,
                exc,
            )
            return await _fallback_indexed_regex_search(
                pattern=pattern,
                glob=glob,
                limit=limit,
                store=store,
            )

        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("ripgrep subprocess was not created with stdout/stderr pipes")

        stderr_task = asyncio.create_task(proc.stderr.read())
        killed_for_limit = False
        stderr = ""

        try:
            results, killed_for_limit = await _collect_results(
                stdout=proc.stdout,
                vault_root=vault_root,
                store=store,
                limit=limit,
            )
            if killed_for_limit and proc.returncode is None:
                proc.kill()
            await proc.wait()
            stderr = await _read_stderr(stderr_task)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
        if killed_for_limit:
            return results
        if proc.returncode not in (0, _NO_MATCH_RETURN_CODE):
            _LOGGER.warning(
                "ripgrep exited with status %s: %s",
                proc.returncode,
                stderr.strip() or "(no stderr)",
            )
            raise RipgrepError(proc.returncode, stderr)
        return results


async def _collect_results(
    *,
    stdout: asyncio.StreamReader,
    vault_root: Path,
    store: FTS5Store,
    limit: int,
) -> tuple[list[SearchResult], bool]:
    results: list[SearchResult] = []
    match_count = 0
    async for raw_line in stdout:
        parsed = _parse_json_line(raw_line)
        if parsed is None or parsed.get("type") != "match":
            continue

        result = await _result_from_match(
            parsed,
            vault_root=vault_root,
            store=store,
            rank_index=match_count,
        )
        match_count += 1
        if result is not None:
            results.append(result)
        if match_count >= limit:
            return results, True
    return results, False


def _build_command(
    rg_path: str,
    pattern: str,
    vault_root: Path,
    glob: str | None,
    limit: int,
) -> list[str]:
    command = [rg_path, "--json", "--max-count", str(limit)]
    if glob:
        command.extend(["--glob", glob])
    command.extend(["--", pattern, str(vault_root)])
    return command


async def _fallback_indexed_regex_search(
    *,
    pattern: str,
    glob: str | None,
    limit: int,
    store: FTS5Store,
) -> list[SearchResult]:
    compiled = re.compile(pattern)
    results: list[SearchResult] = []
    async for chunk in store.iter_all_chunks():
        if glob and not fnmatch.fnmatch(chunk.note_rel_path, glob):
            continue
        snippet = _first_matching_line_snippet(chunk.content, compiled)
        if snippet is None:
            continue
        rank_index = len(results)
        results.append(
            SearchResult(
                chunk=chunk,
                score=1.0 / (1.0 + rank_index),
                snippet=snippet,
            )
        )
        if len(results) >= limit:
            break
    return results


def _first_matching_line_snippet(content: str, pattern: re.Pattern[str]) -> str | None:
    lines = content.splitlines() or [content]
    for line in lines:
        match = pattern.search(line)
        if match is not None:
            return _highlight_text_span(line, match.start(), match.end())
    return None


def _highlight_text_span(line: str, start: int, end: int) -> str:
    return f"{line[:start]}**{line[start:end]}**{line[end:]}".rstrip("\r\n")


def _parse_json_line(raw_line: bytes) -> dict[str, Any] | None:
    try:
        decoded = raw_line.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _LOGGER.info("Skipping undecodable ripgrep JSON line: %s", exc)
        return None
    if not isinstance(parsed, dict):
        _LOGGER.info("Skipping non-object ripgrep JSON line")
        return None
    return parsed


async def _result_from_match(
    event: dict[str, Any],
    *,
    vault_root: Path,
    store: FTS5Store,
    rank_index: int,
) -> SearchResult | None:
    data = event.get("data")
    if not isinstance(data, dict):
        _LOGGER.info("Skipping ripgrep match with invalid data payload")
        return None

    rel_path = _relative_path_from_match(data.get("path"), vault_root)
    line_number = data.get("line_number")
    line = _text_from_data(data.get("lines"))
    submatches = data.get("submatches")
    if rel_path is None or not isinstance(line_number, int) or line is None:
        _LOGGER.info("Skipping ripgrep match with missing path, line number, or line text")
        return None
    if not isinstance(submatches, list):
        _LOGGER.info("Skipping ripgrep match with invalid submatches")
        return None

    chunk = await _resolve_chunk(store, rel_path, line_number)
    if chunk is None:
        return None

    return SearchResult(
        chunk=chunk,
        score=1.0 / (1.0 + rank_index),
        snippet=_highlight_submatches(line, submatches),
    )


async def _resolve_chunk(store: FTS5Store, rel_path: str, line_number: int) -> Chunk | None:
    note_id = await _note_id_for_rel_path(store, rel_path)
    if note_id is None:
        _LOGGER.info("ripgrep match dropped: no note_id mapping for %s", rel_path)
        return None

    chunks = await store.list_chunks_for_note(note_id)
    for chunk in chunks:
        if chunk.line_start <= line_number <= chunk.line_end:
            return chunk
    _LOGGER.info(
        "ripgrep match dropped: no chunk covers %s:%s",
        rel_path,
        line_number,
    )
    return None


async def _note_id_for_rel_path(store: FTS5Store, rel_path: str) -> str | None:
    if not isinstance(store, SQLiteFTS5Store):
        _LOGGER.info(
            "ripgrep match dropped: store does not expose ulid_paths lookup for %s",
            rel_path,
        )
        return None

    connection = store._require_connection()
    async with connection.execute(
        "SELECT note_id FROM ulid_paths WHERE rel_path = ? LIMIT 1;",
        (rel_path,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


def _relative_path_from_match(path_payload: object, vault_root: Path) -> str | None:
    raw_path = _text_from_data(path_payload)
    if raw_path is None:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            return None
    return str(PurePosixPath(*path.parts))


def _text_from_data(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if isinstance(text, str):
        return text
    raw_bytes = payload.get("bytes")
    if not isinstance(raw_bytes, str):
        return None
    try:
        return base64.b64decode(raw_bytes).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _highlight_submatches(line: str, submatches: list[object]) -> str:
    line_bytes = line.encode("utf-8")
    rendered = bytearray()
    cursor = 0
    for submatch in submatches:
        if not isinstance(submatch, dict):
            continue
        start = submatch.get("start")
        end = submatch.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        clamped_start = min(max(start, cursor), len(line_bytes))
        clamped_end = min(max(end, clamped_start), len(line_bytes))
        rendered.extend(line_bytes[cursor:clamped_start])
        rendered.extend(b"**")
        rendered.extend(line_bytes[clamped_start:clamped_end])
        rendered.extend(b"**")
        cursor = clamped_end
    rendered.extend(line_bytes[cursor:])
    return bytes(rendered).decode("utf-8", errors="replace").rstrip("\r\n")


async def _read_stderr(stderr_task: asyncio.Task[bytes]) -> str:
    stderr = await stderr_task
    return stderr.decode("utf-8", errors="replace")


from datacron.core.protocols import RipgrepWrapper as _RipgrepWrapperProtocol  # noqa: E402


def _conformance_check(_: _RipgrepWrapperProtocol) -> None:
    """Mypy structural conformance check."""


_conformance_check(RipgrepWrapper())
