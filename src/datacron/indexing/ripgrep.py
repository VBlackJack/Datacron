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
"""Async ripgrep wrapper with a best-effort indexed Python regex fallback.

Ripgrep is the supported regex path. If its binary is absent, the fallback
rejects known catastrophic shapes before touching the index, then streams the
indexed chunks in bounded batches and stops at the first ``limit`` admitted
matches. This is not a complete ReDoS sandbox: the deadline is observed between
batches, so it cannot preempt ``re`` while it holds the GIL inside one batch, and
cancelling the await does not stop that batch's worker thread.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import json
import os
import re
import shutil
from asyncio.subprocess import PIPE, Process
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, Final, final

from datacron.core.config import (
    DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH,
    DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS,
    DEFAULT_REGEX_MAX_FRAME_BYTES,
    DEFAULT_RIPGREP_PATH,
    REGEX_FALLBACK_SCAN_BATCH_CHUNKS,
    REGEX_STREAM_READ_BYTES,
)
from datacron.core.logger import get_logger
from datacron.core.models import Chunk, SearchResult
from datacron.core.protocols import FTS5Store

__all__ = ["RegexFallbackError", "RipgrepError", "RipgrepWrapper", "ripgrep_available"]

_LOGGER = get_logger(__name__)
_RIPGREP_PATH_ENV: Final[str] = "DATACRON_RIPGREP_PATH"
_NO_MATCH_RETURN_CODE: Final[int] = 1
# Diagnostic text, not a payload: enough to name a bad pattern or a
# permission problem, and far short of one line per file in the vault.
_MAX_STDERR_BYTES: Final[int] = 8192
_STDERR_READ_CHUNK_BYTES: Final[int] = 65536
_STDERR_TRUNCATION_MARKER: Final[str] = "\n... (ripgrep diagnostics truncated)"
_RISKY_REPETITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\([^)]*(?:\||[+*])[^)]*\)(?:[+*]|\{)"
)


class RipgrepError(RuntimeError):
    """Raised when ripgrep exits with an error status."""

    def __init__(self, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        message = stderr.strip() or "(no stderr)"
        super().__init__(f"ripgrep exited with status {returncode}: {message}")


class RegexFallbackError(RuntimeError):
    """Raised when the best-effort Python regex fallback declines or times out."""


class RegexGlobError(ValueError):
    """An invalid glob or an empty admitted file selection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def matches_vault_glob(rel_path: str, glob: str) -> bool:
    """Match case-sensitive path segments; only a complete ** crosses directories."""
    if not glob or glob.startswith("!") or any(char in glob for char in "{}\\"):
        raise RegexGlobError(
            "regex_glob_invalid", "Use a positive vault-relative glob with / separators"
        )
    pattern = glob.removeprefix("./").removeprefix("/")
    parts = tuple(pattern.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RegexGlobError(
            "regex_glob_invalid", "Glob contains an empty or relative path segment"
        )
    path = tuple(rel_path.split("/"))
    if len(parts) == 1:
        return fnmatch.fnmatchcase(path[-1], parts[0])

    @cache
    def match(i: int, j: int) -> bool:
        if j == len(parts):
            return i == len(path)
        if parts[j] == "**":
            return match(i, j + 1) or (i < len(path) and match(i + 1, j))
        return i < len(path) and fnmatch.fnmatchcase(path[i], parts[j]) and match(i + 1, j + 1)

    return match(0, 0)


class RipgrepOutputError(RuntimeError):
    """A bounded subprocess frame was refused before JSON decoding."""

    code = "regex_frame_too_large"


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
        fallback_max_pattern_length: int | None = None,
        fallback_timeout_seconds: float | None = None,
        admit: Callable[[str], bool] | None = None,
        max_frame_bytes: int | None = None,
    ) -> list[SearchResult]:
        """Search with ripgrep, falling back to indexed chunks if the binary is absent.

        Ripgrep is the supported path. The fallback scans indexed chunk bodies only,
        applies a best-effort ReDoS guard before any I/O, and has an advisory deadline
        observed between batches, so it cannot preempt Python ``re`` inside one batch.
        Installing ripgrep avoids this fallback entirely. The indexed scan excludes
        frontmatter and depends on index freshness; MCP ``search_regex`` repairs the
        index before calling this wrapper.

        Globs are case-sensitive and vault-relative on both paths. A single star
        stays within a path segment; a complete double-star segment crosses folders.
        """
        if limit <= 0:
            return []
        if store is None:
            _LOGGER.info("ripgrep search skipped: no FTS5Store supplied for chunk resolution")
            return []

        resolved_rg_path = _resolve_ripgrep_path(rg_path)
        command = _build_command(resolved_rg_path, pattern, glob)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=PIPE, stderr=PIPE, cwd=vault_root
            )
        except OSError as exc:
            _LOGGER.warning(
                "ripgrep is unusable (%s: errno=%s winerror=%s %s); falling back to "
                "best-effort indexed Python regex scan",
                resolved_rg_path,
                exc.errno,
                getattr(exc, "winerror", None),
                exc.strerror,
            )
            return await _fallback_indexed_regex_search(
                pattern=pattern,
                glob=glob,
                limit=limit,
                store=store,
                max_pattern_length=(
                    DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH
                    if fallback_max_pattern_length is None
                    else fallback_max_pattern_length
                ),
                timeout_seconds=(
                    DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS
                    if fallback_timeout_seconds is None
                    else fallback_timeout_seconds
                ),
                admit=admit,
            )

        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("ripgrep subprocess was not created with stdout/stderr pipes")

        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, _MAX_STDERR_BYTES))
        killed_for_limit = False
        stderr = ""

        try:
            results, killed_for_limit = await _collect_results(
                stdout=proc.stdout,
                vault_root=vault_root,
                store=store,
                limit=limit,
                admit=admit,
                max_frame_bytes=max_frame_bytes or DEFAULT_REGEX_MAX_FRAME_BYTES,
                glob=glob,
            )
            if killed_for_limit and proc.returncode is None:
                await _terminate_ripgrep(proc)
            else:
                await proc.wait()
            stderr = await _read_stderr(stderr_task)
        finally:
            if proc.returncode is None:
                await _terminate_ripgrep(proc)
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
            if results:
                # ripgrep exits 2 when any file could not be read, which on Windows
                # is routine: one note held by the editor, one placeholder that does
                # not hydrate, one folder without rights. Every match it printed
                # first is valid, and discarding them told the caller its pattern was
                # rejected when the message underneath was an access error, so an
                # agent rewrote a correct regex indefinitely. A run that reached the
                # end and matched nothing is the only one that can be a pattern
                # problem, and that one still raises.
                return results
            raise RipgrepError(proc.returncode, stderr)
        return results


async def _terminate_ripgrep(proc: Process) -> None:
    """Stop the child and let its pipes close, which is what ``wait`` waits for.

    :meth:`asyncio.subprocess.Process.wait` returns once the child has exited *and*
    every pipe transport it owns has closed. Killing a child whose stdout still holds
    output nobody has read leaves that transport open, and the wait never returns.

    That is the ordinary path here, not an error path: the search stops reading the
    moment it has enough results, so any pattern matching more than a pipe buffer of
    output beyond the limit left the tool hung. Reading the rest is bounded, because
    the child is already dead and cannot produce more.
    """
    if proc.returncode is None:
        proc.kill()
    if proc.stdout is not None:
        with contextlib.suppress(Exception):
            await proc.stdout.read()
    await proc.wait()


async def _collect_results(
    *,
    stdout: asyncio.StreamReader,
    vault_root: Path,
    store: FTS5Store,
    limit: int,
    admit: Callable[[str], bool] | None = None,
    max_frame_bytes: int = DEFAULT_REGEX_MAX_FRAME_BYTES,
    glob: str | None = None,
) -> tuple[list[SearchResult], bool]:
    results: list[SearchResult] = []
    # One note's chunks are resolved once per search, not once per line that matched
    # in it. ``chunks_fts`` declares ``note_id`` UNINDEXED, so each of those lookups
    # scans the table, and a pattern matching twenty lines of one reference note used
    # to scan it twenty times. The caches live for this call only.
    resolver = _ChunkResolver(store)
    async for raw_line in _read_frames(stdout, max_frame_bytes):
        parsed = _parse_json_line(raw_line)
        if parsed is None or parsed.get("type") != "match":
            continue
        match = _match_fields(parsed, vault_root=vault_root)
        if match is None:
            continue
        # Filter on the path before resolving: the glob and the admission check need
        # nothing the index provides, and a match they reject costs a table scan.
        if glob is not None and not matches_vault_glob(match.rel_path, glob):
            continue
        if admit is not None and not admit(match.rel_path):
            continue

        chunk = await resolver.covering(match.rel_path, match.line_number)
        if chunk is not None:
            results.append(
                SearchResult(
                    chunk=chunk,
                    score=1.0 / (1.0 + len(results)),
                    snippet=_highlight_submatches(match.line, match.submatches),
                    redaction_source=match.line,
                )
            )
        if len(results) >= limit:
            return results, True
    return results, False


@dataclass(frozen=True)
class _MatchFields:
    """The parts of one ripgrep match frame the search reads."""

    rel_path: str
    line_number: int
    line: str
    submatches: list[object]


def _match_fields(event: dict[str, Any], *, vault_root: Path) -> _MatchFields | None:
    """Read one match frame, or report why it cannot be used."""
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
    return _MatchFields(
        rel_path=rel_path, line_number=line_number, line=line, submatches=submatches
    )


class _ChunkResolver:
    """Map a note path and line number to its chunk, remembering what it looked up.

    Both lookups behind this are table scans: ``chunks_fts`` declares ``note_id``
    UNINDEXED, and the note path lookup goes to the index as well. Repeating them per
    matching line made the cost of a search the number of matches times the size of
    the notes they fell in, rather than the number of distinct notes.
    """

    def __init__(self, store: FTS5Store) -> None:
        self._store = store
        self._note_ids: dict[str, str | None] = {}
        self._chunks: dict[str, list[Chunk]] = {}

    async def covering(self, rel_path: str, line_number: int) -> Chunk | None:
        """Return the chunk whose line span covers ``line_number``, if any."""
        note_id = await self._note_id(rel_path)
        if note_id is None:
            _LOGGER.info("ripgrep match dropped: no note_id mapping for %s", rel_path)
            return None
        for chunk in await self._chunks_for(note_id):
            if chunk.line_start <= line_number <= chunk.line_end:
                return chunk
        _LOGGER.info("ripgrep match dropped: no chunk covers %s:%s", rel_path, line_number)
        return None

    async def _note_id(self, rel_path: str) -> str | None:
        if rel_path not in self._note_ids:
            self._note_ids[rel_path] = await self._store.get_note_id(rel_path)
        return self._note_ids[rel_path]

    async def _chunks_for(self, note_id: str) -> list[Chunk]:
        if note_id not in self._chunks:
            self._chunks[note_id] = await self._store.list_chunks_for_note(note_id)
        return self._chunks[note_id]


async def _read_frames(stdout: asyncio.StreamReader, maximum: int) -> AsyncIterator[bytes]:
    """Read JSON frames without StreamReader.readline's implicit 64 KiB ceiling."""
    pending = bytearray()
    while block := await stdout.read(REGEX_STREAM_READ_BYTES):
        for part in block.splitlines(keepends=True):
            pending.extend(part)
            if len(pending) > maximum:
                raise RipgrepOutputError(
                    f"ripgrep JSON frame exceeds {maximum} bytes; narrow the glob or "
                    "increase DATACRON_REGEX_MAX_FRAME_BYTES"
                )
            if pending.endswith(b"\n"):
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def ripgrep_available(rg_path: str | None = None) -> bool:
    """Report whether the configured ripgrep binary can actually be launched.

    An interactive shell can resolve ``rg`` through an alias or a shell function
    that a spawned child process cannot see, and an MCP client does not pass its
    own PATH to the server it starts. This probes the way the subprocess launch
    will, so the answer matches what ``search_regex`` will experience.
    """
    return shutil.which(_resolve_ripgrep_path(rg_path)) is not None


def _resolve_ripgrep_path(rg_path: str | None) -> str:
    """Resolve the ripgrep binary, with an explicit argument outranking the environment.

    ``DATACRON_RIPGREP_PATH`` already reaches ``Settings.ripgrep_path`` through the
    settings env prefix, so reading it here is a second, lower-precedence chance for
    a caller that builds the wrapper without settings. An argument always wins, which
    matches pydantic-settings, where init keyword arguments outrank environment values.
    """
    if rg_path:
        return rg_path
    from_environment = os.environ.get(_RIPGREP_PATH_ENV, "").strip()
    return from_environment or DEFAULT_RIPGREP_PATH


def _build_command(rg_path: str, pattern: str, glob: str | None) -> list[str]:
    """Build the ripgrep argument list for one search.

    It took a vault root and a limit and used neither, so the signature
    promised a subprocess rooted at the vault and bounded by the caller's
    limit while the search root is the literal "." and there is no
    --max-count. Both promises are kept elsewhere and differently: the root by
    the cwd the caller passes to create_subprocess_exec, and the limit by the
    collection loop, which stops reading and kills the process. --max-count
    would not express it anyway, being per file rather than per search.
    """
    command = [rg_path, "--json", "--crlf"]
    if glob:
        command.extend(["--glob", glob])
    command.extend(["--", pattern, "."])
    return command


async def _fallback_indexed_regex_search(
    *,
    pattern: str,
    glob: str | None,
    limit: int,
    store: FTS5Store,
    max_pattern_length: int,
    timeout_seconds: float,
    admit: Callable[[str], bool] | None = None,
) -> list[SearchResult]:
    """Run the best-effort indexed fallback when supported ripgrep is unavailable.

    The pattern is refused or compiled before the index is touched, so a rejected
    pattern costs no I/O. The scan then streams the index in bounded batches and
    stops at the first ``limit`` admitted matches, so the deadline is only reached
    by a query that matches nothing. The deadline is observed between batches: it
    cannot preempt ``re`` while it holds the GIL inside one batch, which is what
    bounds a batch rather than the whole scan. Installing ripgrep avoids this
    fallback entirely.

    Filter order is deliberate and load-bearing. ``glob`` is lexical and free, the
    regex is the selective step, and ``admit`` is a filesystem call costing
    hundreds of microseconds, so it runs last, only on chunks whose body already
    matched, and at most once per distinct note.
    """
    if len(pattern) > max_pattern_length:
        raise RegexFallbackError(
            "best-effort regex fallback pattern exceeds "
            f"{max_pattern_length} characters -- install ripgrep"
        )
    compiled = _compile_guarded_pattern(pattern)
    results: list[SearchResult] = []
    admissions: dict[str, bool] = {}
    scanned = 0
    batch: list[Chunk] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in store.iter_all_chunks():
                scanned += 1
                if glob and not matches_vault_glob(chunk.note_rel_path, glob):
                    continue
                batch.append(chunk)
                if len(batch) < REGEX_FALLBACK_SCAN_BATCH_CHUNKS:
                    continue
                if await _drain_batch(batch, compiled, limit, results, admissions, admit):
                    return results
                batch = []
            if batch:
                await _drain_batch(batch, compiled, limit, results, admissions, admit)
            return results
    except TimeoutError:
        raise RegexFallbackError(
            "regex fallback exceeded its advisory timeout after scanning "
            f"{scanned} indexed chunks; narrow the glob, lower the limit, raise "
            "DATACRON_REGEX_FALLBACK_TIMEOUT_SECONDS, or install ripgrep"
        ) from None


async def _drain_batch(
    batch: list[Chunk],
    compiled: re.Pattern[str],
    limit: int,
    results: list[SearchResult],
    admissions: dict[str, bool],
    admit: Callable[[str], bool] | None,
) -> bool:
    """Match one batch off the event loop, then admit and rank the survivors.

    Returns whether ``limit`` results have been collected, which ends the scan.
    """
    matches = await asyncio.to_thread(_scan_indexed_chunks, batch, compiled)
    for chunk, snippet in matches:
        if admit is not None and not _is_admitted(chunk.note_rel_path, admissions, admit):
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
            return True
    return False


def _is_admitted(
    rel_path: str,
    admissions: dict[str, bool],
    admit: Callable[[str], bool],
) -> bool:
    """Decide admission once per distinct note, not once per chunk."""
    admitted = admissions.get(rel_path)
    if admitted is None:
        admitted = admit(rel_path)
        admissions[rel_path] = admitted
    return admitted


def _compile_guarded_pattern(pattern: str) -> re.Pattern[str]:
    """Refuse known catastrophic regex shapes, then compile, before any I/O.

    The guard is deliberately conservative and is not a complete ReDoS sandbox.
    Ripgrep remains the supported regex path. Running it ahead of the index scan
    is what makes the refusal reachable: behind a full scan it could never be
    reported, because the deadline expired first.
    """
    if _RISKY_REPETITION_PATTERN.search(pattern):
        raise RegexFallbackError(
            "best-effort regex fallback rejected a potentially catastrophic pattern "
            "-- install ripgrep"
        )
    return re.compile(pattern)


def _scan_indexed_chunks(
    chunks: list[Chunk],
    compiled: re.Pattern[str],
) -> list[tuple[Chunk, str]]:
    """Return every chunk in one batch whose body matches, with its snippet.

    Ranking and admission are the caller's, so this stays a pure CPU step safe to
    hand to a worker thread.
    """
    matches: list[tuple[Chunk, str]] = []
    for chunk in chunks:
        snippet = _first_matching_line_snippet(chunk.content, compiled)
        if snippet is not None:
            matches.append((chunk, snippet))
    return matches


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


async def _drain_stderr(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read ripgrep's stderr to its end, keeping only the first ``limit`` bytes.

    A single bounded ``read`` stopped consuming the pipe once it returned. When
    ripgrep reported more unreadable files than that (routine on Windows: notes
    held by an editor, placeholders that do not hydrate), it blocked writing to a
    full stderr pipe, never closed stdout, and ``search_regex`` never returned.
    The whole stream is drained; only what is kept is bounded.
    """
    kept = bytearray()
    while chunk := await stream.read(_STDERR_READ_CHUNK_BYTES):
        if len(kept) < limit:
            kept += chunk[: limit - len(kept)]
    return bytes(kept)


async def _read_stderr(stderr_task: asyncio.Task[bytes]) -> str:
    """Decode the bounded diagnostic text ripgrep wrote, and say when it was cut.

    This string is returned verbatim in the caller-visible error message, so it
    is the one payload in the server with no bound: stdout beside it is capped
    and every other payload is sized against max_result_tokens. A pattern that
    makes ripgrep complain once per file turns an error response into a
    transcript of the vault.
    """
    stderr = await stderr_task
    text = stderr.decode("utf-8", errors="replace")
    if len(stderr) >= _MAX_STDERR_BYTES:
        return text + _STDERR_TRUNCATION_MARKER
    return text


from datacron.core.protocols import RipgrepWrapper as _RipgrepWrapperProtocol  # noqa: E402


def _conformance_check(_: _RipgrepWrapperProtocol) -> None:
    """Mypy structural conformance check."""


_conformance_check(RipgrepWrapper())
