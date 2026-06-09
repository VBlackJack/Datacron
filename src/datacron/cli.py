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
"""Datacron command-line entry point.

The :data:`app` Typer instance is the ``datacron`` console script declared in
``pyproject.toml``. The Phase-0 Sem-1 scope only ships ``init`` and ``status``
as fully wired commands; the remaining subcommands are registered but raise
``typer.Exit`` until the relevant modules land.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn

import typer
import yaml
from ulid import ULID

from datacron import __version__
from datacron.core.config import (
    DEFAULT_EXCLUDED_FOLDERS,
    INDEX_DB_FILENAME,
    LOG_FILENAME_PATTERN,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.logger import configure_logging, get_logger
from datacron.core.paths import (
    sidecar_dir,
    sidecar_index_dir,
    sidecar_vault_config,
)
from datacron.core.vault import build_configured_reader

__all__ = ["app", "mcp_entry"]

_LOGGER = get_logger(__name__)

DEFAULT_DRAFTS_FOLDER: Final[str] = "_drafts"
DEFAULT_JOURNAL_FOLDER: Final[str] = "_journal"
ENCODING_UTF8: Final[str] = "utf-8"
LINE_ENDINGS_LF: Final[str] = "lf"

app = typer.Typer(
    name="datacron",
    help="Datacron — local-first MCP server for Markdown vaults.",
    no_args_is_help=True,
    add_completion=False,
)
mcp_app = typer.Typer(
    name="mcp",
    help="MCP server lifecycle commands.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(mcp_app, name="mcp")


def _print(message: str) -> None:
    """Write to stdout via Typer (testable and stream-safe)."""
    typer.echo(message)


def _error(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _resolve_vault_root(explicit: Path | None, settings: Settings) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    if settings.vault_root is not None:
        return settings.vault_root
    cwd = Path.cwd().resolve()
    if sidecar_vault_config(cwd).exists():
        return cwd
    _error(
        "No vault root provided. Pass --vault or set DATACRON_VAULT_ROOT, "
        "or run from a directory containing .datacron/VAULT.yaml."
    )


def _format_vault_yaml(vault_id: str, created: datetime) -> str:
    payload = {
        "datacron_version": __version__,
        "vault_id": vault_id,
        "created": created.isoformat(),
        "encoding": ENCODING_UTF8,
        "line_endings": LINE_ENDINGS_LF,
        "folders": {
            "drafts": DEFAULT_DRAFTS_FOLDER,
            "journal": DEFAULT_JOURNAL_FOLDER,
        },
        "excluded_folders": list(DEFAULT_EXCLUDED_FOLDERS),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _load_vault_yaml(vault_root: Path) -> VaultConfig | None:
    return load_vault_config(sidecar_vault_config(vault_root))


def _log_invocation(name: str, **details: object) -> float:
    started = time.perf_counter()
    _LOGGER.info("cli.%s started %s", name, details)
    return started


def _log_completion(name: str, started: float) -> None:
    duration_ms = (time.perf_counter() - started) * 1000
    _LOGGER.info("cli.%s completed in %.1fms", name, duration_ms)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def init(
    vault_path: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=False,
        resolve_path=True,
        help="Path to the Markdown vault to initialize.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing .datacron/VAULT.yaml.",
    ),
) -> None:
    """Initialize the ``.datacron/`` sidecar in a Markdown vault."""
    configure_logging()
    started = _log_invocation("init", vault_path=str(vault_path), force=force)

    if not vault_path.exists():
        vault_path.mkdir(parents=True, exist_ok=True)
        _LOGGER.info("Created vault directory %s", vault_path)
    elif not vault_path.is_dir():
        _error(f"{vault_path} exists and is not a directory.")

    sidecar = sidecar_dir(vault_path)
    sidecar.mkdir(parents=True, exist_ok=True)
    sidecar_index_dir(vault_path).mkdir(parents=True, exist_ok=True)
    (sidecar / "logs").mkdir(parents=True, exist_ok=True)

    config_path = sidecar_vault_config(vault_path)
    if config_path.exists() and not force:
        _print(f"VAULT.yaml already present at {config_path}; use --force to overwrite.")
        _log_completion("init", started)
        return

    vault_id = str(ULID())
    now = datetime.now(tz=UTC)
    config_path.write_text(_format_vault_yaml(vault_id, now), encoding=ENCODING_UTF8)

    _print(f"Initialized Datacron vault at {vault_path}")
    _print(f"  sidecar:    {sidecar}")
    _print(f"  config:     {config_path}")
    _print(f"  vault_id:   {vault_id}")
    _log_completion("init", started)


@app.command()
def status(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help="Vault root. Defaults to DATACRON_VAULT_ROOT or current directory.",
    ),
) -> None:
    """Print vault metadata, note count, and index freshness."""
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    started = _log_invocation("status", vault=str(vault_root))

    config = _load_vault_yaml(vault_root)
    initialized = config is not None

    if config is not None:
        reader = build_configured_reader(vault_root)
        notes = asyncio.run(reader.list_notes())
        note_count = len(notes)
    else:
        note_count = 0

    db_path = sidecar_index_dir(vault_root) / INDEX_DB_FILENAME
    log_dir = sidecar_dir(vault_root) / "logs"
    today_log = LOG_FILENAME_PATTERN.format(date=datetime.now().strftime("%Y%m%d"))

    _print(f"Datacron {__version__}")
    _print(f"  vault_root: {vault_root}")
    _print(f"  initialized: {'yes' if initialized else 'no (run `datacron init`)'}")
    if config is not None:
        _print(f"  vault_id:   {config.vault_id or '<unknown>'}")
        _print(f"  created:    {config.created or '<unknown>'}")
    _print(f"  notes:      {note_count}")
    _print(f"  index:      {'built' if db_path.exists() else 'not built'} ({db_path})")
    _print(f"  log file:   {log_dir / today_log}")
    _log_completion("status", started)


def _not_implemented(command: str, since: str) -> NoReturn:
    _error(
        f"`datacron {command}` is not implemented yet — planned for {since}. "
        "Run `datacron --help` for available commands."
    )


@app.command()
def index(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Build or refresh the FTS5 index for the vault."""
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=False))


@app.command()
def reindex(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Drop the FTS5 database and rebuild from scratch."""
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=True))


async def _run_index(vault_root: Path, *, drop_first: bool) -> None:
    """Orchestrate VaultReader + MarkdownChunker + SQLiteFTS5Store."""
    from datacron.core.paths import sidecar_index_db  # noqa: PLC0415
    from datacron.indexing.chunker import MarkdownChunker  # noqa: PLC0415
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415

    db_path = sidecar_index_db(vault_root)
    if drop_first and db_path.exists():
        _LOGGER.info("Dropping existing index at %s before rebuild", db_path)
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            db_path.with_suffix(db_path.suffix + suffix).unlink(missing_ok=True)

    reader = build_configured_reader(vault_root)
    chunker = MarkdownChunker()
    store = SQLiteFTS5Store()
    await store.open(db_path)
    started = time.perf_counter()
    note_count = 0
    chunk_count = 0
    try:
        notes = await reader.list_notes()
        for note in notes:
            chunks = chunker.chunk(note)
            await store.upsert_note(note, chunks)
            note_count += 1
            chunk_count += len(chunks)
    finally:
        await store.close()

    duration_ms = (time.perf_counter() - started) * 1000.0
    _print(
        f"Indexed {note_count} notes / {chunk_count} chunks into {db_path} ({duration_ms:.0f} ms)"
    )
    _LOGGER.info(
        "cli.index completed (vault=%s notes=%d chunks=%d drop_first=%s duration_ms=%.1f)",
        vault_root,
        note_count,
        chunk_count,
        drop_first,
        duration_ms,
    )


@app.command(name="ask")
def ask(
    question: str = typer.Argument(..., help="Question to send through the local tools."),
) -> None:
    """CLI fallback that exercises the MCP tool surface (Phase 0 Sem 3-4)."""
    _ = question
    _not_implemented("ask", since="Sem 3-4 (depends on mcp/tools.py)")


@app.command(name="eval")
def eval_(
    questions: Path = typer.Option(
        ...,
        "--questions",
        exists=True,
        help="Path to an eval-questions YAML file.",
    ),
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Run the eval harness against the configured vault (Phase 0 Sem 4)."""
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_eval(vault_root, questions))


async def _run_eval(vault_root: Path, questions_path: Path) -> None:
    """Open the existing index and run the local eval harness."""
    from datacron.core.paths import sidecar_index_db  # noqa: PLC0415
    from datacron.eval.harness import LocalEvalHarness, load_eval_questions  # noqa: PLC0415
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415
    from datacron.indexing.ripgrep import RipgrepWrapper  # noqa: PLC0415

    db_path = sidecar_index_db(vault_root)
    if not db_path.exists():
        _print("No index found. Run `datacron index` first.")
        raise typer.Exit(code=1)

    questions = load_eval_questions(questions_path)
    store = SQLiteFTS5Store()
    await store.open(db_path)
    try:
        await LocalEvalHarness().run(questions, store, RipgrepWrapper())
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# `datacron mcp ...`
# ---------------------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Run the FastMCP stdio server.

    Reads MCP JSON-RPC messages from stdin and replies on stdout. The
    server exposes the Sem-2 read-only catalog: ``list_notes``,
    ``get_note``, and the three vault resources. Logs go to the
    configured FileLogger; stdout is reserved for the MCP framing
    protocol.
    """
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    _LOGGER.info("cli.mcp_serve starting (vault=%s)", vault_root)
    from datacron.mcp.server import run_stdio  # noqa: PLC0415

    try:
        asyncio.run(run_stdio(settings=settings, vault_root=vault_root))
    except KeyboardInterrupt:
        _LOGGER.info("cli.mcp_serve received KeyboardInterrupt; exiting cleanly")


@mcp_app.command("install")
def mcp_install(
    client: str = typer.Option(
        ...,
        "--client",
        help="Client identifier (claude-desktop).",
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help="Vault root. Defaults to DATACRON_VAULT_ROOT or current directory.",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config-path",
        help="Override the target config file (for testing or non-standard installs).",
    ),
) -> None:
    """Write the Datacron MCP server entry into a target client's config."""
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)

    if client != "claude-desktop":
        _error(f"Unknown client: {client!r}. Supported: claude-desktop.")

    from datacron.installers.claude_desktop import (  # noqa: PLC0415
        ClaudeDesktopConfigError,
        install_claude_desktop_config,
    )

    try:
        target = install_claude_desktop_config(vault_root, config_path=config_path)
    except ClaudeDesktopConfigError as exc:
        _error(f"Could not write Claude Desktop config: {exc}")

    _print(f"Wrote Datacron MCP entry to {target}")
    _print("Restart Claude Desktop for the change to take effect.")
    _LOGGER.info(
        "cli.mcp_install completed (client=%s vault=%s config=%s)",
        client,
        vault_root,
        target,
    )


def mcp_entry() -> None:
    """``datacron-mcp`` script entry — direct stdio MCP server.

    Used by ``installers/claude_desktop.py`` (Sem 3) so the Claude Desktop
    config can launch the server without going through the ``datacron mcp
    serve`` subcommand. Reads the vault root from ``DATACRON_VAULT_ROOT``
    (set by the installer) or falls back to the current directory.
    """
    configure_logging()
    settings = get_settings()
    vault_root = _resolve_vault_root(None, settings)
    _LOGGER.info("datacron-mcp script entry starting (vault=%s)", vault_root)
    from datacron.mcp.server import run_stdio  # noqa: PLC0415

    try:
        asyncio.run(run_stdio(settings=settings, vault_root=vault_root))
    except KeyboardInterrupt:
        _LOGGER.info("datacron-mcp received KeyboardInterrupt; exiting cleanly")


if __name__ == "__main__":  # pragma: no cover
    app()
