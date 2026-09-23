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
"""Small user-facing defects: each answered wrongly or crashed on an ordinary input."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datacron import setup_wizard
from datacron.cli import app as cli_app
from datacron.core.config import LOG_FILENAME_PATTERN, Settings, reset_settings_cache
from datacron.core.frontmatter import normalize_tag_filter, serialize
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.installers.claude_desktop import ClaudeDesktopConfigError
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import _audit_query_impl, _create_note_ai_impl, _search_text_impl

_runner = CliRunner()


@pytest.fixture
async def writable_app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    settings = Settings(read_paths=[tmp_path], write_paths=[tmp_path], vault_root=tmp_path)
    store = SQLiteFTS5Store()
    await store.open(tmp_path / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(
            settings=settings, vault_root=tmp_path, chunker=MarkdownChunker(), store=store
        )
    finally:
        await store.close()


def test_a_tag_filter_accepts_the_hash_a_note_writes() -> None:
    assert normalize_tag_filter(["#Kafka", " memory ", "#", ""]) == ["kafka", "memory"]


async def test_search_filters_by_a_tag_written_with_its_hash(writable_app: DatacronApp) -> None:
    """tags=["#kafka"] matched no note and returned a clean empty result."""
    (writable_app.vault_root / "note.md").write_text(
        serialize({"id": "01J00000000000000000000171", "tags": ["kafka"]}, "# N\n\nbrokers\n"),
        encoding="utf-8",
    )

    result = await _search_text_impl(writable_app, query="brokers", limit=5, tags=["#kafka"])

    assert [row["note_rel_path"] for row in result["results"]] == ["note.md"]


async def test_audit_query_redacts_a_secret_shaped_note_path(writable_app: DatacronApp) -> None:
    """Search already concealed this path; the journal readers returned it in full."""
    created = await _create_note_ai_impl(
        writable_app,
        rel_path="_memory/password-hunter2.md",
        title="Rotation",
        body="body",
        origin="ai",
        confidence="high",
        tags=["memory"],
    )
    assert "error" not in created, created

    audit = await _audit_query_impl(
        writable_app, start=None, end=None, tool=None, note=None, limit=10
    )

    paths = [operation["rel_path"] for operation in audit["operations"]]
    assert paths
    assert all("hunter2" not in path for path in paths)


def test_status_names_the_log_file_the_logger_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("DATACRON_LOG_DIR", str(log_dir))
    reset_settings_cache()
    try:
        result = _runner.invoke(cli_app, ["status", "--vault", str(tmp_path / "vault")])
    finally:
        reset_settings_cache()

    assert result.exit_code == 0, result.output
    log_line = next(line for line in result.stdout.splitlines() if "log file:" in line)
    assert str(log_dir) in log_line
    assert LOG_FILENAME_PATTERN.split("{", 1)[0] in log_line


def test_setup_reports_a_missing_server_command_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> None:
        raise ClaudeDesktopConfigError("datacron-mcp not found")

    monkeypatch.setattr(setup_wizard, "resolve_mcp_invocation", missing)

    result = _runner.invoke(
        cli_app,
        ["setup", "--vault", str(tmp_path), "--client", "claude-code", "--yes", "--no-index"],
    )

    assert result.exit_code == 1, result.output
    assert "datacron-mcp not found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
