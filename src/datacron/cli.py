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
``pyproject.toml``. It exposes vault lifecycle, indexing, integrity, evaluation,
and MCP server management commands.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import click
import typer

from datacron import __version__
from datacron.bootstrap import initialize_vault
from datacron.core.config import (
    DEFAULT_DURABILITY_MODE,
    LOG_FILENAME_PATTERN,
    VALID_DURABILITY_MODES,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.durability import WritePolicy, probe_directory_durability
from datacron.core.logger import configure_logging, get_logger
from datacron.core.paths import (
    sidecar_dir,
    sidecar_index_db,
    sidecar_vault_config,
)
from datacron.core.scope import SingleTenantVaultScope
from datacron.core.vault import build_configured_reader
from datacron.scrubber import CanaryInitializationError, ScrubState, initialize_canaries
from datacron.setup_wizard import (
    CLIENT_CHOICES,
    CLIENT_CLAUDE_DESKTOP,
    DEFAULT_WRITE_SUBFOLDER,
    SetupPlan,
    SetupResult,
    run_setup,
)

__all__ = ["app", "mcp_entry"]

_LOGGER = get_logger(__name__)

app = typer.Typer(
    name="datacron",
    help="Datacron -- local-first MCP server for Markdown vaults.",
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


@app.callback()
def main() -> None:
    """Configure process logging once at the CLI execution boundary."""
    configure_logging()


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


def _settings_for_cli_vault(settings: Settings, vault_root: Path) -> Settings:
    """Bind an explicit CLI vault without widening configured non-empty scopes."""
    updates: dict[str, object] = {"vault_root": vault_root}
    if not settings.read_paths:
        updates["read_paths"] = [vault_root]
    if not settings.write_paths:
        updates["write_paths"] = [vault_root]
    return settings.model_copy(update=updates)


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
    started = _log_invocation("init", vault_path=str(vault_path), force=force)

    try:
        result = initialize_vault(vault_path, force=force)
    except NotADirectoryError as exc:
        _error(str(exc))

    if not result.created:
        _print(f"VAULT.yaml already present at {result.config_path}; use --force to overwrite.")
        _log_completion("init", started)
        return

    _print(f"Initialized Datacron vault at {result.vault_path}")
    _print(f"  sidecar:    {result.sidecar_path}")
    _print(f"  config:     {result.config_path}")
    _print(f"  vault_id:   {result.vault_id}")
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

    db_path = sidecar_index_db(vault_root)
    index_status = asyncio.run(_index_status_label(db_path))
    log_dir = sidecar_dir(vault_root) / "logs"
    today_log = LOG_FILENAME_PATTERN.format(date=datetime.now().strftime("%Y%m%d"))

    _print(f"Datacron {__version__}")
    _print(f"  vault_root: {vault_root}")
    _print(f"  initialized: {'yes' if initialized else 'no (run `datacron init`)'}")
    if config is not None:
        _print(f"  vault_id:   {config.vault_id or '<unknown>'}")
        _print(f"  created:    {config.created or '<unknown>'}")
    _print(f"  notes:      {note_count}")
    _print(f"  index:      {index_status} ({db_path})")
    _print(f"  log file:   {log_dir / today_log}")
    _log_completion("status", started)


async def _index_status_label(db_path: Path) -> str:
    if not db_path.exists():
        return "not built"

    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415

    store = SQLiteFTS5Store()
    try:
        await store.open(db_path)
        stats = await store.stats()
    except Exception as exc:
        _LOGGER.warning("Unable to read index stats from %s: %s", db_path, exc)
        return "unreadable -- run `datacron reindex`"
    finally:
        await store.close()

    if stats.note_count > 0:
        return f"built ({stats.note_count} notes, {stats.chunk_count} chunks)"
    return "empty -- run `datacron index`"


@app.command()
def index(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Build or refresh the FTS5 index for the vault."""
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=False))


@app.command()
def reindex(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Build, validate, and atomically publish a complete FTS5 replacement."""
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=True))


@app.command(name="scrub-init")
def scrub_init(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Explicitly create configured integrity canaries without overwriting any."""
    base_settings = get_settings()
    vault_root = _resolve_vault_root(vault, base_settings)
    settings = _settings_for_cli_vault(base_settings, vault_root)
    scope = SingleTenantVaultScope(vault_root, settings)
    write_policy = WritePolicy(settings, probe_directory_durability(vault_root))
    try:
        result = initialize_canaries(vault_root, settings, scope, write_policy)
    except CanaryInitializationError as exc:
        _error(str(exc))
    _print(
        f"Integrity canaries ready at {vault_root}: "
        f"{result['created']} created, {result['existing']} unchanged"
    )


@app.command()
def scrub(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Run one configured, resumable, alert-only integrity scrub window."""
    base_settings = get_settings()
    vault_root = _resolve_vault_root(vault, base_settings)
    settings = _settings_for_cli_vault(base_settings, vault_root)
    state = asyncio.run(_run_scrub(vault_root, settings))
    status = "critical" if state.anomalies else ("complete" if state.completed else "running")
    _print(
        f"Integrity scrub {status}: {state.checked_notes}/{state.total_notes} notes, "
        f"{len(state.anomalies)} anomalies, pass {state.pass_id}"
    )
    if state.anomalies:
        raise typer.Exit(code=2)


async def _run_scrub(vault_root: Path, settings: Settings) -> ScrubState:
    """Open the completed index immutably and run one scrub window."""
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415
    from datacron.scrubber import run_integrity_scrub  # noqa: PLC0415

    store = SQLiteFTS5Store()
    await store.open(sidecar_index_db(vault_root), read_only=True)
    try:
        scope = SingleTenantVaultScope(vault_root, settings)
        write_policy = WritePolicy(settings, probe_directory_durability(vault_root))
        return await run_integrity_scrub(
            vault_root,
            settings,
            scope,
            write_policy,
            store,
        )
    finally:
        await store.close()


async def _run_index(vault_root: Path, *, drop_first: bool) -> None:
    """Reconcile the FTS5 index with the vault (incremental unless ``drop_first``).

    Both ``datacron index`` and the MCP read-repair go through the shared
    :func:`reconcile`, so the CLI gets the same mtime-gated incremental behavior:
    unchanged notes are skipped, changed ones re-chunked, vanished ones deleted.
    ``reindex`` (``drop_first``) builds a separate complete database and only
    swaps it over the live index after byte-hash and SQLite validation.
    """
    from datacron.core.paths import sidecar_index_db  # noqa: PLC0415
    from datacron.indexing.chunker import MarkdownChunker  # noqa: PLC0415
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415
    from datacron.indexing.rebuild import rebuild_index_atomic  # noqa: PLC0415
    from datacron.indexing.reconcile import reconcile  # noqa: PLC0415

    db_path = sidecar_index_db(vault_root)
    settings = get_settings()
    config = _load_vault_yaml(vault_root) or VaultConfig()
    if drop_first:
        started = time.perf_counter()
        rebuilt = await rebuild_index_atomic(vault_root, settings, config)
        duration_ms = (time.perf_counter() - started) * 1000.0
        _print(
            f"Reindexed {rebuilt['reindexed_notes']} notes "
            f"into generation {rebuilt['generation']} at {db_path} ({duration_ms:.0f} ms)"
        )
        _LOGGER.info(
            "cli.reindex completed (vault=%s notes=%d chunks=%d generation=%d duration_ms=%.1f)",
            vault_root,
            rebuilt["reindexed_notes"],
            rebuilt["chunk_count"],
            rebuilt["generation"],
            duration_ms,
        )
        return

    reader = build_configured_reader(vault_root)
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    await store.open(db_path)
    started = time.perf_counter()
    try:
        stats = await reconcile(store, reader, chunker, mtime_gate=True)
    finally:
        await store.close()

    duration_ms = (time.perf_counter() - started) * 1000.0
    _print(
        f"Indexed {stats['reindexed_notes']} notes "
        f"({stats['skipped_notes']} unchanged, {stats['deleted_notes']} removed) "
        f"into {db_path} ({duration_ms:.0f} ms)"
    )
    _LOGGER.info(
        "cli.index completed (vault=%s reindexed=%d skipped=%d deleted=%d "
        "drop_first=%s duration_ms=%.1f)",
        vault_root,
        stats["reindexed_notes"],
        stats["skipped_notes"],
        stats["deleted_notes"],
        drop_first,
        duration_ms,
    )


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
    config = _load_vault_yaml(vault_root) or VaultConfig()
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    await store.open(db_path)
    try:
        await LocalEvalHarness().run(questions, store, RipgrepWrapper())
    finally:
        await store.close()


@app.command()
def setup(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help="Vault root to set up. Prompted interactively if omitted.",
    ),
    client: str | None = typer.Option(
        None,
        "--client",
        help=f"MCP client to configure ({', '.join(CLIENT_CHOICES)}).",
    ),
    enable_write: bool = typer.Option(
        False,
        "--enable-write",
        help="Enable the confined write tools on a subfolder.",
    ),
    write_path: Path | None = typer.Option(
        None,
        "--write-path",
        help="Write-allowlisted directory (implies --enable-write).",
    ),
    durability: str | None = typer.Option(
        None,
        "--durability",
        help="Durability mode (best-effort or strict).",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Configure the server for certified read-only mode.",
    ),
    build_index: bool = typer.Option(
        True,
        "--index/--no-index",
        help="Build the search index during setup.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing .datacron/VAULT.yaml.",
    ),
    assume_yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Accept defaults for every unspecified option; no prompts.",
    ),
) -> None:
    """Guided end-to-end setup: initialize, index, and wire an MCP client.

    Runs interactively by default, asking for the vault location and each
    option not supplied as a flag. Pass ``--yes`` for an unattended run that
    accepts the defaults, or provide flags to script the whole setup.
    """
    started = _log_invocation("setup", assume_yes=assume_yes)

    resolved_vault = _prompt_vault(vault, assume_yes)
    resolved_client = _prompt_client(client, assume_yes)
    resolved_durability = _prompt_durability(durability, assume_yes)
    resolved_enable_write, resolved_write_path = _prompt_write(
        enable_write, write_path, resolved_vault, assume_yes
    )
    resolved_read_only = read_only or (
        not assume_yes and typer.confirm("Configure certified read-only mode?", default=False)
    )

    plan = SetupPlan(
        vault_path=resolved_vault,
        build_index=build_index,
        enable_write=resolved_enable_write,
        write_path=resolved_write_path,
        client=resolved_client,
        durability=resolved_durability,
        read_only=resolved_read_only,
        force=force,
    )

    try:
        result = asyncio.run(run_setup(plan))
    except (ValueError, NotADirectoryError) as exc:
        _error(str(exc))

    _render_setup_result(result)
    _log_completion("setup", started)


def _prompt_vault(vault: Path | None, assume_yes: bool) -> Path:
    if vault is not None:
        return vault.expanduser().resolve()
    if assume_yes:
        return Path.cwd().resolve()
    answer = typer.prompt("Vault path", default=str(Path.cwd()))
    return Path(answer).expanduser().resolve()


def _prompt_client(client: str | None, assume_yes: bool) -> str:
    if client is not None:
        if client not in CLIENT_CHOICES:
            _error(f"Unknown client {client!r}. Expected one of {list(CLIENT_CHOICES)}.")
        return client
    if assume_yes:
        return CLIENT_CLAUDE_DESKTOP
    answer: str = typer.prompt(
        "MCP client",
        default=CLIENT_CLAUDE_DESKTOP,
        type=click.Choice(list(CLIENT_CHOICES)),
    )
    return answer


def _prompt_durability(durability: str | None, assume_yes: bool) -> str:
    if durability is not None:
        return durability
    if assume_yes:
        return DEFAULT_DURABILITY_MODE
    answer: str = typer.prompt(
        "Durability mode",
        default=DEFAULT_DURABILITY_MODE,
        type=click.Choice(sorted(VALID_DURABILITY_MODES)),
    )
    return answer


def _prompt_write(
    enable_write: bool,
    write_path: Path | None,
    vault_root: Path,
    assume_yes: bool,
) -> tuple[bool, Path | None]:
    if write_path is not None:
        return True, write_path.expanduser().resolve()
    if enable_write:
        resolved = _prompt_write_path(vault_root, assume_yes)
        return True, resolved
    if assume_yes:
        return False, None
    if not typer.confirm("Enable the confined write tools?", default=False):
        return False, None
    return True, _prompt_write_path(vault_root, assume_yes)


def _prompt_write_path(vault_root: Path, assume_yes: bool) -> Path | None:
    default_path = vault_root / DEFAULT_WRITE_SUBFOLDER
    if assume_yes:
        return default_path
    answer = typer.prompt("Write-allowlisted directory", default=str(default_path))
    return Path(answer).expanduser().resolve()


def _render_setup_result(result: SetupResult) -> None:
    _print("")
    _print("Datacron setup complete.")
    _print(f"  vault:      {result.bootstrap.vault_path}")
    if result.indexed_notes is not None:
        _print(f"  indexed:    {result.indexed_notes} notes")
    if result.write_path is not None:
        _print(f"  writing:    enabled -> {result.write_path}")
    else:
        _print("  writing:    disabled")
    _print(f"  durability: {result.durability}")
    _print(f"  read-only:  {'yes' if result.read_only else 'no'}")
    if result.client_config_path is not None:
        _print(f"  client:     {result.client_config_path}")
        _print("Restart Claude Desktop for the change to take effect.")
    if result.stdio_config is not None:
        _print("")
        _print("Add this server to your Claude Code (stdio) MCP config:")
        _print(result.stdio_config)
    for warning in result.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW, err=True)
    _print("Verify from your client with get_health, or run `datacron status`.")


# ---------------------------------------------------------------------------
# `datacron mcp ...`
# ---------------------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve(
    vault: Path | None = typer.Option(None, "--vault", "-v", help="Vault root."),
) -> None:
    """Run the FastMCP stdio server.

    Reads MCP JSON-RPC messages from stdin and replies on stdout. The
    server exposes the registered read, write, and operational tools plus
    the three vault resources. Logs go to the configured FileLogger;
    stdout is reserved for the MCP framing protocol.
    """
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    if not vault_root.is_dir():
        typer.echo(f"Vault not found: {vault_root}", err=True)
        raise typer.Exit(code=1)
    _LOGGER.info("cli.mcp_serve starting (vault=%s)", vault_root)
    from datacron.mcp.server import run_stdio  # noqa: PLC0415

    try:
        asyncio.run(run_stdio(settings=settings, vault_root=vault_root))
    except FileNotFoundError as exc:
        _LOGGER.error("cli.mcp_serve vault unavailable: %s", exc)
        typer.echo(f"Vault not found: {exc}", err=True)
        raise typer.Exit(code=1) from exc
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
    """``datacron-mcp`` script entry -- direct stdio MCP server.

    Used by ``installers/claude_desktop.py`` (Sem 3) so the Claude Desktop
    config can launch the server without going through the ``datacron mcp
    serve`` subcommand. Reads the vault root from ``DATACRON_VAULT_ROOT``
    (set by the installer) or falls back to the current directory.
    """
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
