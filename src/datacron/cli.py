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
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import click
import typer
import yaml
from rich.console import Console
from rich.progress import Progress, TaskID, TextColumn

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
from datacron.core.frontmatter import FrontmatterError
from datacron.core.logger import configure_logging, get_logger
from datacron.core.models import EvalPipeline, EvalTransport
from datacron.core.operation_log import (
    HistoryUnavailableError,
    OperationContext,
    OperationLogError,
)
from datacron.core.paths import (
    read_ulid_mappings,
    sidecar_dir,
    sidecar_index_db,
    sidecar_vault_config,
)
from datacron.core.scope import SingleTenantVaultScope
from datacron.core.vault import build_configured_reader
from datacron.core.vault_writer import (
    FilesystemVaultWriter,
    VaultLockBusyError,
    WriteConflictError,
)
from datacron.installers.mcp_clients import (
    ALL_CLIENT_IDS,
    SCOPE_PROJECT,
    SCOPE_USER,
    discover_unregistration_targets,
    unregister_targets,
)
from datacron.installers.protocol import (
    PROTOCOL_ALL,
    PROTOCOL_CLIENT_IDS,
    ProtocolInstallOutcome,
    install_memory_protocol,
    uninstall_memory_protocol,
)
from datacron.scrubber import CanaryInitializationError, ScrubState, initialize_canaries
from datacron.setup_wizard import (
    CLIENT_ALL,
    CLIENT_CHOICES,
    DEFAULT_WRITE_SUBFOLDERS,
    INSTALL_SCOPE_BOTH,
    INSTALL_SCOPE_CHOICES,
    ResetExecutionError,
    ResetGuardError,
    SetupPlan,
    SetupResult,
    _scopes_for,
    get_user_write_env,
    run_setup,
)

if TYPE_CHECKING:
    from datacron.eval.baseline import BaselineComparison
    from datacron.reliability import ReliabilityScan, ReliabilityViolation

__all__ = ["app", "mcp_entry"]

_LOGGER = get_logger(__name__)
_VAULT_ROOT_HELP: Final[str] = (
    "Vault root. Fallback: DATACRON_VAULT_ROOT, then cwd containing VAULT.yaml under .datacron."
)


class _SetupPrompt(StrEnum):
    """Stable identifiers for setup's centralized interactive guidance."""

    VAULT = "vault"
    RESET = "reset"
    CLIENT = "client"
    SCOPE = "scope"
    DURABILITY = "durability"
    WRITE = "write"
    WRITE_PATHS = "write-paths"
    MACHINE_WIDE_WRITE = "machine-wide-write"
    REPLACE_WRITE_ENV = "replace-write-env"
    READ_ONLY = "read-only"
    PROTOCOL = "protocol"


class _OpsRepairAction(StrEnum):
    """Explicit operator choices for one blocked operation."""

    RESTORE_BEFORE = "restore-before"
    ADOPT_DISK = "adopt-disk"


class _OpsRepairIdAction(StrEnum):
    """Explicit operator choices for one divergent note identity."""

    ADOPT_INDEX = "adopt-index"
    ADOPT_FRONTMATTER = "adopt-frontmatter"


_SETUP_PROMPT_EXPLANATIONS: Final[dict[_SetupPrompt, tuple[str, ...]]] = {
    _SetupPrompt.VAULT: (
        "Choose the dedicated Markdown folder Datacron will serve as this vault.",
        "Default: the current directory, which is right when setup runs from the intended vault.",
        "A different path serves notes and stores the Datacron sidecar there instead.",
    ),
    _SetupPrompt.RESET: (
        "Reset removes this vault's Datacron configuration and generated index before setup.",
        "Default: no, which protects the current configuration and index.",
        "Choosing yes still preserves Markdown notes, identities, audit data, and logs.",
    ),
    _SetupPrompt.CLIENT: (
        "Choose which MCP client configuration receives the Datacron server entry.",
        "Default: all, which registers every supported client detected on this machine.",
        "Choose one client to limit registration, or none to leave client configs unchanged.",
    ),
    _SetupPrompt.SCOPE: (
        "Choose where detected MCP clients receive their Datacron entry.",
        "Default: both, so user-wide and project-local configurations are covered.",
        "Choose user for all projects or project to confine registration to this vault.",
    ),
    _SetupPrompt.DURABILITY: (
        "Durability controls whether writes require proof that directory metadata reached storage.",
        "Default: best-effort, which works across common filesystems and logs degraded guarantees.",
        "Choose strict to refuse writes when the filesystem cannot prove directory durability.",
    ),
    _SetupPrompt.WRITE: (
        "By default, your AI assistants can read your notes but never change them.",
        "Default: no, which leaves every note protected from AI changes.",
        "Choose yes to let them create and update notes only in _memory, _drafts, and "
        "_journal. You can enable this later by running setup again.",
    ),
    _SetupPrompt.WRITE_PATHS: (
        "Choose the only directories where Datacron write tools may change notes.",
        "Default: {default_paths} under this vault, the standard confined work areas.",
        "Changing the list moves or narrows the write boundary; other paths stay read-only.",
    ),
    _SetupPrompt.MACHINE_WIDE_WRITE: (
        "This remembers the same note-writing permission for AI assistants installed later.",
        "Default: no, which applies the permission only to assistants configured by this setup.",
        "Choose yes to reuse the same 3-subfolder permission automatically for future "
        "assistants; leave no to decide again later.",
    ),
    _SetupPrompt.REPLACE_WRITE_ENV: (
        "The existing user-wide write allowlist differs from this setup's directories.",
        "Default: no, which preserves the existing user environment unchanged.",
        "Choose yes to replace it with the allowlist selected for this vault.",
    ),
    _SetupPrompt.READ_ONLY: (
        "Certified read-only mode blocks every MCP write operation.",
        "Default: no, which keeps normal operation while writes still require a separate opt-in.",
        "Choose yes to block writes even when write directories were allowlisted.",
    ),
    _SetupPrompt.PROTOCOL: (
        "The memory protocol adds Datacron workflow instructions to supported client files.",
        "Default: no, which leaves all client instruction files unchanged.",
        "Choose yes to add or update only Datacron's marked instruction blocks.",
    ),
}

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
protocol_app = typer.Typer(
    name="protocol",
    help="Install or remove Datacron memory instructions in supported clients.",
    no_args_is_help=True,
    add_completion=False,
)
ops_app = typer.Typer(
    name="ops",
    help="Inspect or explicitly repair blocked operation recovery.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(mcp_app, name="mcp")
app.add_typer(protocol_app, name="protocol")
app.add_typer(ops_app, name="ops")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"datacron {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Configure process logging except for the strictly read-only planner."""
    if ctx.invoked_subcommand != "reorganize":
        configure_logging()


def _print(message: str) -> None:
    """Write to stdout via Typer (testable and stream-safe)."""
    typer.echo(message)


def _explain(prompt: _SetupPrompt, **values: str) -> None:
    """Render centralized guidance immediately before one interactive setup prompt."""
    for line in _SETUP_PROMPT_EXPLANATIONS[prompt]:
        _print(f"  {line.format_map(values)}")


def _error(message: str, *, exit_code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=exit_code)


@contextmanager
def _index_progress() -> Iterator[Callable[[int, int], None]]:
    """Render a plain note counter without a decorative progress bar."""
    console = Console()
    with Progress(
        TextColumn("indexed {task.completed:.0f}/{task.total:.0f} notes"),
        console=console,
        auto_refresh=False,
    ) as display:
        task_id: TaskID = display.add_task("index", total=0)

        def update(completed: int, total: int) -> None:
            display.update(task_id, completed=completed, total=total, refresh=True)

        yield update


def _resolve_vault_root(
    explicit: Path | None,
    settings: Settings,
    *,
    error_exit_code: int = 1,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    if settings.vault_root is not None:
        return settings.vault_root
    cwd = Path.cwd().resolve()
    if sidecar_vault_config(cwd).exists():
        return cwd
    _error(
        "No vault root provided. Pass --vault or set DATACRON_VAULT_ROOT, "
        "or run from a directory containing .datacron/VAULT.yaml.",
        exit_code=error_exit_code,
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
        help=_VAULT_ROOT_HELP,
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


def _maintenance_settings(vault_root: Path, settings: Settings) -> Settings:
    """Widen the write scope to the vault root for explicit local maintenance.

    The content write scope exists to bound *agent* writes to note folders, so it
    deliberately excludes ``.datacron/`` -- where Datacron keeps its own state. A
    maintenance command writing its own checkpoint is not an agent write, and
    refusing it means the command cannot run at all.

    Shared on purpose: ``ops inspect``, ``ops repair`` and ``scrub`` all need this
    exact widening, and having each build it separately is how ``scrub`` came to be
    the one that never got it.
    """
    return settings.model_copy(update={"write_paths": [vault_root]})


def _ops_writer(vault_root: Path, settings: Settings) -> FilesystemVaultWriter:
    """Build a vault-confined writer for explicit local maintenance."""
    return FilesystemVaultWriter(
        vault_root,
        _maintenance_settings(vault_root, settings),
        _load_vault_yaml(vault_root) or VaultConfig(),
    )


@ops_app.command(name="inspect")
def ops_inspect(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help=_VAULT_ROOT_HELP,
    ),
) -> None:
    """Inspect blocked operation manifests without changing durable state."""
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    started = _log_invocation("ops.inspect", vault=str(vault_root))
    try:
        blocked = asyncio.run(_ops_writer(vault_root, settings).inspect_recovery())
    except (OSError, OperationLogError, ValueError, VaultLockBusyError) as exc:
        _error(f"Recovery inspection failed: {exc}")
    if not blocked:
        _print("Recovery inspection: no blocked operations.")
        _print("No changes made.")
        _log_completion("ops.inspect", started)
        return
    noun = "operation" if len(blocked) == 1 else "operations"
    _print(f"Recovery inspection: {len(blocked)} blocked {noun}")
    for item in blocked:
        _print(f"  operation_id: {item.operation_id}")
        _print(f"  rel_path: {item.rel_path}")
        _print(f"  reason: {item.reason}")
        _print(f"  expected_before_hash: {item.expected_before_hash or '<absent>'}")
        _print(f"  expected_after_hash: {item.expected_after_hash}")
        _print(f"  disk_hash: {item.disk_hash or '<absent>'}")
        restore_status = "available" if item.restore_before_available else "unavailable"
        adopt_status = "available" if item.adopt_disk_available else "unavailable"
        _print(f"  restore-before: {restore_status}")
        _print(f"  adopt-disk: {adopt_status}")
    _print("No changes made.")
    _log_completion("ops.inspect", started)


@ops_app.command(name="repair")
def ops_repair(
    operation_id: str = typer.Option(..., "--operation-id", help="Blocked operation ID."),
    action: _OpsRepairAction = typer.Option(..., "--action", help="Exact repair action."),
    expected_disk_hash: str = typer.Option(
        ...,
        "--expected-disk-hash",
        help="Exact disk SHA-256 copied from `datacron ops inspect`.",
    ),
    confirm: str | None = typer.Option(
        None,
        "--confirm",
        help="Repeat the exact operation ID to authorize the repair.",
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help=_VAULT_ROOT_HELP,
    ),
) -> None:
    """Repair one blocked operation under exact ID and disk-hash confirmation."""
    if confirm != operation_id:
        _error(f"Repair not confirmed. Pass --confirm {operation_id}")
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    started = _log_invocation(
        "ops.repair",
        vault=str(vault_root),
        operation_id=operation_id,
        action=action.value,
    )
    try:
        repaired = asyncio.run(
            _ops_writer(vault_root, settings).repair_recovery(
                operation_id,
                action.value,
                expected_disk_hash=expected_disk_hash,
                actor="cli:local-operator",
            )
        )
    except (
        FileNotFoundError,
        HistoryUnavailableError,
        OSError,
        OperationLogError,
        ValueError,
        VaultLockBusyError,
        WriteConflictError,
    ) as exc:
        _error(f"Recovery repair refused: {exc}")
    if repaired.action == "restore-before":
        _print("Repair complete: restored exact before bytes.")
    else:
        _print("Repair complete: adopted current disk bytes.")
    _print(f"  operation_id: {repaired.operation_id}")
    _print(f"  repair_operation_id: {repaired.repair_operation_id}")
    _print(f"  rel_path: {repaired.rel_path}")
    _print(f"  before_hash: {repaired.before_hash}")
    _print(f"  after_hash: {repaired.after_hash}")
    _log_completion("ops.repair", started)


_ID_SOURCES: Final[tuple[str, ...]] = ("frontmatter", "sidecar", "sqlite")


@dataclass(frozen=True)
class _IdRepairOutcome:
    """Durable evidence of one applied note-identity repair."""

    rel_path: str
    action: str
    note_id: str
    previous: tuple[tuple[str, str], ...]
    content_hash: str
    note_rewritten: bool
    sidecar_realigned: bool
    index_before: str | None
    index_after: str | None
    mismatches_before: int
    mismatches_after: int


def _id_claimants(scan: ReliabilityScan, rel_path: str, note_id: str) -> tuple[str, ...]:
    """Return other divergent paths whose recorded sources carry ``note_id``."""
    return tuple(
        sorted(
            other.rel_path
            for other in scan.id_violations
            if other.rel_path != rel_path and note_id in dict(other.details).values()
        )
    )


def _recommended_id_action(
    vault_root: Path,
    scan: ReliabilityScan,
    violation: ReliabilityViolation,
) -> str:
    """Return the safest identity-source action the repair preflight accepts.

    The canonical-ULID check is replayed here on purpose. Without it the command
    recommends `adopt-index` for an index that holds a malformed 26-character ID,
    and then refuses that exact action -- the reader accepts any 26-character
    string, so a malformed canonical ID is reachable in practice.
    """
    from datacron.mcp.tools.write_validation import is_canonical_ulid  # noqa: PLC0415

    if violation.classification != "mismatch":
        return "none -- duplicate IDs are reported, never repaired automatically"
    sources = dict(violation.details)
    canonical = sources.get("sqlite") or sources.get("sidecar")
    frontmatter_id = sources.get("frontmatter")
    candidates: list[tuple[str, str]] = []
    if canonical and is_canonical_ulid(canonical):
        candidates.append(("adopt-index", canonical))
    if frontmatter_id and is_canonical_ulid(frontmatter_id):
        candidates.append(("adopt-frontmatter", frontmatter_id))
    if not candidates:
        return "none -- no source carries a canonical ULID"

    refusals: list[str] = []
    for action, note_id in candidates:
        claimants = _id_claimants(scan, violation.rel_path, note_id)
        if claimants:
            refusals.append(f"{note_id} is already carried by {', '.join(claimants)}")
            continue
        try:
            _assert_migrated_sidecar_agrees(vault_root, violation.rel_path, note_id)
        except ValueError as exc:
            refusals.append(str(exc))
            continue
        return action
    return f"none -- {'; '.join(refusals)}"


@ops_app.command(name="inspect-id")
def ops_inspect_id(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help=_VAULT_ROOT_HELP,
    ),
) -> None:
    """Inspect note-identity divergences without changing durable state."""
    from datacron.reliability import scan_vault_read_only  # noqa: PLC0415

    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    started = _log_invocation("ops.inspect-id", vault=str(vault_root))
    try:
        scan = scan_vault_read_only(vault_root)
    except OSError as exc:
        _error(f"Identity inspection failed: {exc}")
    if not scan.id_violations:
        _print("Identity inspection: no ID divergences.")
        _print("No changes made.")
        _log_completion("ops.inspect-id", started)
        return
    content_hashes = dict(scan.content_hashes)
    noun = "divergence" if len(scan.id_violations) == 1 else "divergences"
    _print(f"Identity inspection: {len(scan.id_violations)} ID {noun}")
    for item in scan.id_violations:
        sources = dict(item.details)
        _print(f"  rel_path: {item.rel_path}")
        for source in _ID_SOURCES:
            _print(f"  {source}: {sources.get(source, '<absent>')}")
        _print(f"  classification: {item.classification}")
        _print(f"  content_hash: {content_hashes.get(item.rel_path, '<absent>')}")
        _print(f"  recommended action: {_recommended_id_action(vault_root, scan, item)}")
    _print("No changes made.")
    _log_completion("ops.inspect-id", started)


@ops_app.command(name="repair-id")
def ops_repair_id(
    rel_path: str = typer.Option(..., "--rel-path", help="Vault-relative note path."),
    action: _OpsRepairIdAction = typer.Option(..., "--action", help="Exact repair action."),
    expected_hash: str = typer.Option(
        ...,
        "--expected-hash",
        help="Exact note SHA-256 copied from `datacron ops inspect-id`.",
    ),
    confirm: str | None = typer.Option(
        None,
        "--confirm",
        help="Repeat the exact note path to authorize the repair.",
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help=_VAULT_ROOT_HELP,
    ),
) -> None:
    """Repair one divergent note identity under exact path and hash confirmation."""
    if confirm != rel_path:
        _error(f"Repair not confirmed. Pass --confirm {rel_path}")
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    started = _log_invocation(
        "ops.repair-id",
        vault=str(vault_root),
        rel_path=rel_path,
        action=action.value,
    )
    try:
        outcome = asyncio.run(
            _repair_note_id(vault_root, settings, rel_path, action, expected_hash)
        )
    except _PartialIdRepairError as exc:
        # Reached only after durable state has already changed. Calling this a
        # refusal would send the operator away believing the vault is untouched.
        _error(f"Identity repair applied in part: {exc}")
    except (
        FrontmatterError,
        HistoryUnavailableError,
        OSError,
        OperationLogError,
        ValueError,
        VaultLockBusyError,
        WriteConflictError,
        sqlite3.Error,
    ) as exc:
        _error(f"Identity repair refused: {exc}")
    _print(f"Identity repair complete: adopted the {outcome.action.removeprefix('adopt-')} ID.")
    _print(f"  rel_path: {outcome.rel_path}")
    _print(f"  note_id: {outcome.note_id}")
    for source, value in outcome.previous:
        _print(f"  previous {source}: {value}")
    _print(f"  content_hash: {outcome.content_hash}")
    _print(f"  note rewritten: {'yes' if outcome.note_rewritten else 'no'}")
    _print(f"  sidecar realigned: {'yes' if outcome.sidecar_realigned else 'no'}")
    _print(
        f"  indexed id: {outcome.index_before or '<absent>'} -> {outcome.index_after or '<absent>'}"
    )
    _print(f"  id_mismatches: {outcome.mismatches_before} -> {outcome.mismatches_after}")
    if outcome.mismatches_after >= outcome.mismatches_before:
        _error(
            "Identity repair did not clear the divergence; "
            "re-run `datacron ops inspect-id` before any further write."
        )
    _log_completion("ops.repair-id", started)


def _resolve_id_violation(
    scan: ReliabilityScan,
    rel_path: str,
) -> tuple[ReliabilityViolation, dict[str, str]]:
    """Return the divergence recorded for ``rel_path`` or refuse explicitly."""
    for violation in scan.id_violations:
        if violation.rel_path == rel_path:
            if violation.classification != "mismatch":
                raise ValueError(
                    f"{rel_path} carries a duplicate ID, not a mismatch; "
                    "duplicates are reported, never repaired automatically"
                )
            return violation, dict(violation.details)
    raise ValueError(f"no ID divergence recorded for {rel_path}; nothing to repair")


def _canonical_id_for_action(
    action: _OpsRepairIdAction,
    sources: dict[str, str],
) -> tuple[str, str]:
    """Return the source label and the ID the requested action adopts."""
    if action is _OpsRepairIdAction.ADOPT_FRONTMATTER:
        target = sources.get("frontmatter")
        if target is None:
            raise ValueError("the note carries no frontmatter ID to adopt")
        return "frontmatter", target
    target = sources.get("sqlite") or sources.get("sidecar")
    if target is None:
        raise ValueError("no canonical index or sidecar ID is available to adopt")
    return "canonical", target


def _assert_id_is_unclaimed(
    scan: ReliabilityScan,
    rel_path: str,
    note_id: str,
) -> None:
    """Refuse to adopt an ID another note already carries.

    A note that is both a duplicate and a mismatch is classified ``mismatch``, so
    the duplicate refusal alone does not catch this. Adopting the ID anyway makes
    the index upsert overwrite the other note's row by ``note_id``: the collided
    note stays on disk but vanishes from search, listing and backlinks.
    """
    claimants = _id_claimants(scan, rel_path, note_id)
    if claimants:
        raise ValueError(
            f"{note_id} is already carried by {', '.join(claimants)}; adopting it here "
            "would evict that note from the index. Resolve the duplicate first."
        )


def _assert_migrated_sidecar_agrees(vault_root: Path, rel_path: str, note_id: str) -> None:
    """Refuse when the migrated sidecar would re-impose the old ID after repair.

    ``JsonIdStore`` merges ``ulids.json.migrated`` over ``ulids.json`` and writes
    the result back, so a stale entry there can resurface as the effective mapping.
    The reliability scan and the index migration resolve the other way round, which
    is exactly why a disagreement between the two files must be settled by a human
    rather than guessed at here.
    """
    migrated = sidecar_dir(vault_root) / "ulids.json.migrated"
    if not migrated.is_file():
        return
    try:
        payload = read_ulid_mappings(
            migrated,
            require_string_pairs=True,
            invalid_object_is_empty=True,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read migrated ID sidecar {migrated}: {exc}") from exc
    recorded = payload.get(rel_path)
    if recorded is not None and recorded != note_id:
        raise ValueError(
            f"migrated sidecar {migrated} still maps {rel_path} to {recorded}; "
            "resolve or remove that entry before repairing the identity"
        )


class _PartialIdRepairError(Exception):
    """Raised when a step fails after the note or the sidecar was already written."""


def _realign_sidecar_entry(vault_root: Path, rel_path: str, note_id: str) -> None:
    """Rewrite one mapping in ``ulids.json`` and nothing else.

    ``JsonIdStore`` cannot be used here: loading it merges ``ulids.json.migrated``
    over the primary file and writes the merged result back, so repairing one note
    would silently stamp every stale migrated mapping onto unrelated notes.
    """
    path = sidecar_dir(vault_root) / "ulids.json"
    payload: dict[str, str] = {}
    if path.is_file():
        payload = dict(
            read_ulid_mappings(path, require_string_pairs=True, invalid_object_is_empty=True)
        )
    payload[rel_path] = note_id
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, path)


async def _realign_index_identity(
    vault_root: Path,
    rel_path: str,
    note_id: str,
) -> tuple[str | None, str | None]:
    """Realign the live index and report the indexed ID before and after.

    The stale row is dropped first on purpose: reconcile skips a note whose content
    hash is unchanged, so ``adopt-frontmatter`` -- which never rewrites the note --
    would otherwise leave the old ``note_id`` indexed forever. Both identity tables
    are consulted, because ``notes`` gates reconcile while ``ulid_paths`` can hold a
    mapping of its own, and dropping only one leaves the other stale.

    Reconcile scans the whole vault and drops vanished paths, but its mtime gate
    trusts other unchanged-mtime rows without rereading or hashing them. Unrelated
    index-only drift can therefore remain after this targeted identity repair.
    """
    from datacron.indexing.chunker import MarkdownChunker  # noqa: PLC0415
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415
    from datacron.indexing.reconcile import reconcile  # noqa: PLC0415

    db_path = sidecar_index_db(vault_root)
    if not db_path.exists():
        return None, None
    settings = get_settings()
    config = _load_vault_yaml(vault_root) or VaultConfig()
    reader = build_configured_reader(vault_root)
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    await store.open(db_path)
    try:
        indexed = await store.list_indexed_notes_with_mtime()
        entry = indexed.get(rel_path)
        before = entry[0] if entry is not None else await store.get_note_id(rel_path)
        for stale in {entry[0] if entry is not None else None, await store.get_note_id(rel_path)}:
            if stale is not None and stale != note_id:
                await store.delete_note(stale)
        await reconcile(store, reader, chunker, mtime_gate=True)
        refreshed = await store.list_indexed_notes_with_mtime()
        after_entry = refreshed.get(rel_path)
        after = after_entry[0] if after_entry is not None else await store.get_note_id(rel_path)
    finally:
        await store.close()
    return before, after


async def _apply_id_source_realignments(
    vault_root: Path,
    rel_path: str,
    note_id: str,
    sources: dict[str, str],
) -> tuple[bool, str | None, str | None]:
    """Realign sidecar and index sources after the note plan is validated."""
    sidecar_realigned = "sidecar" in sources and sources["sidecar"] != note_id
    if sidecar_realigned:
        try:
            _realign_sidecar_entry(vault_root, rel_path, note_id)
        except OSError as exc:
            raise _PartialIdRepairError(
                f"the note now carries {note_id}, but the sidecar could not be "
                f"updated: {exc}. Re-run this command to finish the repair."
            ) from exc

    try:
        index_before, index_after = await _realign_index_identity(vault_root, rel_path, note_id)
    except (OSError, sqlite3.Error) as exc:
        raise _PartialIdRepairError(
            f"the note repair and any required sidecar realignment completed for {note_id}, "
            f"but the index could not be realigned: {exc}. Re-run this command once the index "
            "is free, or run `datacron reindex` with MCP servers stopped."
        ) from exc
    return sidecar_realigned, index_before, index_after


async def _repair_note_id(
    vault_root: Path,
    settings: Settings,
    rel_path: str,
    action: _OpsRepairIdAction,
    expected_hash: str,
) -> _IdRepairOutcome:
    """Apply one identity repair and report the divergence count it cleared."""
    from datacron.mcp.tools.write_validation import (  # noqa: PLC0415
        _validate_expected_hash,
        is_canonical_ulid,
        replace_frontmatter_id,
    )
    from datacron.reliability import scan_vault_read_only  # noqa: PLC0415

    cleaned_hash = _validate_expected_hash(expected_hash)
    if cleaned_hash is None:
        raise ValueError("--expected-hash is required")
    rel_path = rel_path.strip().replace("\\", "/")
    scan = scan_vault_read_only(vault_root)
    mismatches_before = len(scan.id_violations)
    _violation, sources = _resolve_id_violation(scan, rel_path)
    label, note_id = _canonical_id_for_action(action, sources)
    if not is_canonical_ulid(note_id):
        raise ValueError(
            f"{label} ID {note_id} is not a canonical 26-character Crockford ULID; "
            "adopting it would propagate a malformed identity"
        )
    _assert_id_is_unclaimed(scan, rel_path, note_id)
    _assert_migrated_sidecar_agrees(vault_root, rel_path, note_id)

    content_hash = dict(scan.content_hashes).get(rel_path)
    if content_hash != cleaned_hash:
        raise WriteConflictError("note changed since inspection (hash mismatch); re-read and retry")

    writer = _ops_writer(vault_root, settings)
    note_rewritten = sources.get("frontmatter") != note_id
    if note_rewritten:
        content_hash = await writer.mutate_note_atomic(
            rel_path,
            lambda raw: replace_frontmatter_id(raw, note_id),
            expected_hash=cleaned_hash,
            operation=OperationContext(
                op="repair_id",
                tool="datacron_ops_repair_id",
                actor="cli:local-operator",
                parameters={"action": action.value, "note_id": note_id},
            ),
        )
        try:
            with writer.lock_note_identity(rel_path, expected_hash=content_hash):
                locked_scan = scan_vault_read_only(vault_root)
                _assert_id_is_unclaimed(locked_scan, rel_path, note_id)
                _assert_migrated_sidecar_agrees(vault_root, rel_path, note_id)
                sidecar_realigned, index_before, index_after = await _apply_id_source_realignments(
                    vault_root,
                    rel_path,
                    note_id,
                    sources,
                )
                mismatches_after = len(scan_vault_read_only(vault_root).id_violations)
        except _PartialIdRepairError:
            raise
        except (
            HistoryUnavailableError,
            OSError,
            OperationLogError,
            ValueError,
            VaultLockBusyError,
            sqlite3.Error,
        ) as exc:
            raise _PartialIdRepairError(
                "the note rewrite completed, but sidecar and index realignment stopped: "
                f"{exc}. Re-run `datacron ops inspect-id` before any further repair."
            ) from exc
    else:
        with writer.lock_note_identity(rel_path, expected_hash=cleaned_hash):
            locked_scan = scan_vault_read_only(vault_root)
            _locked_violation, locked_sources = _resolve_id_violation(locked_scan, rel_path)
            locked_label, locked_note_id = _canonical_id_for_action(action, locked_sources)
            if locked_sources != sources or locked_label != label or locked_note_id != note_id:
                raise WriteConflictError(
                    "identity sources changed since inspection; re-read and retry"
                )
            _assert_id_is_unclaimed(locked_scan, rel_path, note_id)
            _assert_migrated_sidecar_agrees(vault_root, rel_path, note_id)
            sources = locked_sources
            sidecar_realigned, index_before, index_after = await _apply_id_source_realignments(
                vault_root,
                rel_path,
                note_id,
                sources,
            )
            mismatches_after = len(scan_vault_read_only(vault_root).id_violations)

    return _IdRepairOutcome(
        rel_path=rel_path,
        action=action.value,
        note_id=note_id,
        previous=tuple(sorted(sources.items())),
        content_hash=content_hash,
        note_rewritten=note_rewritten,
        sidecar_realigned=sidecar_realigned,
        index_before=index_before,
        index_after=index_after,
        mismatches_before=mismatches_before,
        mismatches_after=mismatches_after,
    )


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
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
) -> None:
    """Build or refresh the FTS5 index for the vault."""
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=False))


@app.command()
def reindex(
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
) -> None:
    """Build, validate, and atomically publish a complete FTS5 replacement."""
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    asyncio.run(_run_index(vault_root, drop_first=True))


_REORGANIZE_DRY_RUN_HELP: Final[str] = (
    "Required. Report only: this release never moves, renames or rewrites a note."
)
_REORGANIZE_JSON_HELP: Final[str] = "Emit the stable machine-readable report instead of text."
_REORGANIZE_KIND_HELP: Final[str] = "Restrict the report to one deviation kind."
_REORGANIZE_REQUIRES_DRY_RUN: Final[str] = (
    "datacron reorganize currently supports --dry-run only. Pass --dry-run explicitly: "
    "no other mode exists yet, and the flag must never become implicit."
)
_REORGANIZE_NO_CONFIG: Final[str] = "No .datacron/VAULT.yaml found under {vault_root}."
_REORGANIZE_BAD_KIND: Final[str] = "Unknown --kind {value!r}. Expected one of: {allowed}."
_REORGANIZE_BAD_VAULT: Final[str] = "Vault root is not a readable directory: {vault_root}."
_REORGANIZE_BAD_CONFIG: Final[str] = "Invalid organization configuration: {detail}"
_EXIT_DEVIATIONS_FOUND: Final[int] = 1
_EXIT_CONFIGURATION_ERROR: Final[int] = 2


@app.command()
def reorganize(
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help=_REORGANIZE_DRY_RUN_HELP),
    as_json: bool = typer.Option(False, "--json", help=_REORGANIZE_JSON_HELP),
    kind: str | None = typer.Option(None, "--kind", help=_REORGANIZE_KIND_HELP),
) -> None:
    """Measure how far the vault has drifted from the organization it declares.

    Read-only by construction. Exit code 0 means no deviation, 1 means the
    report is not empty, and 2 means the vault or its configuration could not
    be read -- so a non-empty report is detectable in CI without being an error.
    """
    from datacron.organization.planner import (  # noqa: PLC0415
        DeviationKind,
        OrganizationConfigurationError,
        plan_organization,
    )
    from datacron.organization.report import render_json, render_text  # noqa: PLC0415

    if not dry_run:
        typer.secho(_REORGANIZE_REQUIRES_DRY_RUN, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=_EXIT_CONFIGURATION_ERROR)

    selected: DeviationKind | None = None
    if kind is not None:
        try:
            selected = DeviationKind(kind.strip().upper())
        except ValueError:
            allowed = ", ".join(item.value for item in DeviationKind)
            typer.secho(
                _REORGANIZE_BAD_KIND.format(value=kind, allowed=allowed),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=_EXIT_CONFIGURATION_ERROR) from None

    try:
        settings = get_settings()
        vault_root = _resolve_vault_root(
            vault,
            settings,
            error_exit_code=_EXIT_CONFIGURATION_ERROR,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _error(
            _REORGANIZE_BAD_CONFIG.format(detail=str(exc)),
            exit_code=_EXIT_CONFIGURATION_ERROR,
        )
    if not vault_root.is_dir():
        _error(
            _REORGANIZE_BAD_VAULT.format(vault_root=vault_root),
            exit_code=_EXIT_CONFIGURATION_ERROR,
        )

    try:
        config = _load_vault_yaml(vault_root)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        _error(
            _REORGANIZE_BAD_CONFIG.format(detail=str(exc)),
            exit_code=_EXIT_CONFIGURATION_ERROR,
        )
    if config is None:
        _error(
            _REORGANIZE_NO_CONFIG.format(vault_root=vault_root),
            exit_code=_EXIT_CONFIGURATION_ERROR,
        )

    try:
        plan = plan_organization(vault_root, config, settings=settings)
    except (OrganizationConfigurationError, OSError) as exc:
        _error(
            _REORGANIZE_BAD_CONFIG.format(detail=str(exc)),
            exit_code=_EXIT_CONFIGURATION_ERROR,
        )
    if selected is not None:
        plan = replace(
            plan,
            deviations=tuple(item for item in plan.deviations if item.kind is selected),
        )

    _print(render_json(plan) if as_json else render_text(plan))
    if plan.has_deviations:
        raise typer.Exit(code=_EXIT_DEVIATIONS_FOUND)


@app.command(name="scrub-init")
def scrub_init(
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
) -> None:
    """Explicitly create configured integrity canaries without overwriting any."""
    base_settings = get_settings()
    vault_root = _resolve_vault_root(vault, base_settings)
    settings = _maintenance_settings(vault_root, _settings_for_cli_vault(base_settings, vault_root))
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
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
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

    maintenance = _maintenance_settings(vault_root, settings)
    store = SQLiteFTS5Store()
    await store.open(sidecar_index_db(vault_root), read_only=True)
    try:
        scope = SingleTenantVaultScope(vault_root, maintenance)
        write_policy = WritePolicy(maintenance, probe_directory_durability(vault_root))
        return await run_integrity_scrub(
            vault_root,
            maintenance,
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
        with _index_progress() as progress:
            rebuilt = await rebuild_index_atomic(
                vault_root,
                settings,
                config,
                progress=progress,
            )
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
        with _index_progress() as progress:
            stats = await reconcile(
                store,
                reader,
                chunker,
                mtime_gate=True,
                progress=progress,
            )
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
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
    pipeline: EvalPipeline = typer.Option(
        EvalPipeline.TOOL,
        "--pipeline",
        help="Retrieval layer to evaluate: tool or store.",
        case_sensitive=False,
    ),
    transport: EvalTransport = typer.Option(
        EvalTransport.IMPL,
        "--transport",
        help="Tool invocation transport: impl or e2e.",
        case_sensitive=False,
    ),
    save_baseline: bool = typer.Option(
        False,
        "--save-baseline",
        help="Save aggregate metrics as this vault's local baseline.",
    ),
    compare: bool = typer.Option(
        False,
        "--compare",
        help="Compare against the vault-local baseline and enforce regression gates.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable JSON report instead of the Rich table.",
    ),
) -> None:
    """Evaluate retrieval quality against the real MCP search pipeline."""
    if pipeline is EvalPipeline.STORE and transport is EvalTransport.E2E:
        raise typer.BadParameter("--transport e2e requires --pipeline tool")
    if save_baseline and compare:
        raise typer.BadParameter("--save-baseline and --compare are mutually exclusive")
    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings)
    exit_code = asyncio.run(
        _run_eval(
            vault_root,
            questions,
            settings,
            pipeline,
            transport,
            save_baseline_requested=save_baseline,
            compare=compare,
            json_output=json_output,
        )
    )
    if exit_code:
        raise typer.Exit(code=exit_code)


async def _run_eval(  # noqa: PLR0912 -- command orchestration covers optional output modes
    vault_root: Path,
    questions_path: Path,
    settings: Settings,
    pipeline: EvalPipeline,
    transport: EvalTransport,
    *,
    save_baseline_requested: bool,
    compare: bool,
    json_output: bool,
) -> int:
    """Open the existing index and run the local eval harness."""
    from datacron.core.paths import sidecar_index_db  # noqa: PLC0415
    from datacron.eval.baseline import (  # noqa: PLC0415
        baseline_path,
        compare_with_baseline,
        eval_config_hash,
        load_baseline,
        save_baseline,
    )
    from datacron.eval.harness import LocalEvalHarness, load_eval_questions  # noqa: PLC0415
    from datacron.eval.transport import e2e_search_transport  # noqa: PLC0415
    from datacron.indexing.fts5_store import SQLiteFTS5Store  # noqa: PLC0415
    from datacron.indexing.ripgrep import RipgrepWrapper  # noqa: PLC0415
    from datacron.mcp.server import build_app  # noqa: PLC0415

    db_path = sidecar_index_db(vault_root)
    if not db_path.exists():
        _print("No index found. Run `datacron index` first.")
        return 1

    questions = load_eval_questions(questions_path)
    config = _load_vault_yaml(vault_root) or VaultConfig()
    baseline = None
    if compare:
        try:
            baseline = load_baseline(vault_root)
        except FileNotFoundError:
            message = f"No eval baseline found at {baseline_path(vault_root)}."
            _print(json.dumps({"error": message}) if json_output else message)
            return 1
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    ripgrep = RipgrepWrapper()
    datacron_app = build_app(
        settings=settings,
        vault_root=vault_root,
        store=store,
        ripgrep=ripgrep,
    )

    if transport is EvalTransport.E2E:
        async with e2e_search_transport(vault_root, settings) as search:
            report = await LocalEvalHarness(tool_search=search).run(
                questions,
                datacron_app,
                pipeline=pipeline,
                transport=transport,
                render=not json_output,
            )
    else:
        await store.open(db_path, read_only=not datacron_app.write_policy.writes_allowed)
        try:
            report = await LocalEvalHarness().run(
                questions,
                datacron_app,
                pipeline=pipeline,
                transport=transport,
                render=not json_output,
            )
        finally:
            await store.close()

    comparison = None
    if baseline is not None:
        comparison = compare_with_baseline(
            report,
            baseline,
            tolerance=settings.eval_regression_tolerance,
            config_hash=eval_config_hash(settings, config),
        )
    saved_path = None
    if save_baseline_requested:
        save_baseline(report, vault_root, settings, config)
        saved_path = baseline_path(vault_root)

    if json_output:
        output = report.model_dump(mode="json")
        if comparison is not None:
            output["baseline_comparison"] = comparison.model_dump(mode="json")
        if saved_path is not None:
            output["baseline_saved_to"] = str(saved_path)
        _print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if comparison is not None:
            _print_baseline_comparison(comparison)
        if saved_path is not None:
            _print(f"Baseline saved to {saved_path}")
    return 1 if comparison is not None and not comparison.passed else 0


def _print_baseline_comparison(comparison: BaselineComparison) -> None:
    """Print baseline deltas without importing eval models at CLI startup."""
    _print("Baseline deltas (current - baseline):")
    for metric, delta in comparison.deltas.items():
        _print(f"  {metric}: {delta:+.4f}")
    if not comparison.config_hash_matches:
        _print("Warning: baseline and current retrieval config hashes differ.")
    if not comparison.mode_matches:
        _print("Warning: baseline and current pipeline/transport modes differ.")
    if comparison.passed:
        _print(f"Regression check: PASS (tolerance {comparison.tolerance:.3f})")
    else:
        _print(
            "Regression check: FAIL - "
            f"{', '.join(comparison.regressions)} exceeded tolerance "
            f"{comparison.tolerance:.3f}"
        )


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
        help=f"MCP client to configure ({', '.join(CLIENT_CHOICES)}). 'all' auto-detects.",
    ),
    scope: str | None = typer.Option(
        None,
        "--scope",
        help=f"For --client all, config scope ({', '.join(INSTALL_SCOPE_CHOICES)}).",
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
    machine_wide_write: bool = typer.Option(
        False,
        "--machine-wide-write",
        help="Apply the write allowlist to the user environment for future clients.",
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
    reset: bool = typer.Option(
        False,
        "--reset",
        help=(
            "Reset all VAULT.yaml settings and the generated index before setup; "
            "notes, identities, and audit data are preserved."
        ),
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
    protocol_enabled: bool = typer.Option(
        False,
        "--protocol",
        help="Install the Datacron memory protocol into detected client instructions.",
    ),
) -> None:
    """Guided end-to-end setup: initialize, wire an MCP client, and index.

    Runs interactively by default, asking for the vault location and each
    option not supplied as a flag. With ``--reset``, the generated index and
    all custom ``VAULT.yaml`` settings are removed, then setup recreates them.
    The index is rebuilt unless ``--no-index`` leaves its recreated directory
    empty. Note identities, audit data, sidecar logs, and Markdown files remain
    unchanged. Pass ``--yes`` for an unattended run that accepts the defaults,
    or provide flags to script the whole setup.
    """
    started = _log_invocation("setup", assume_yes=assume_yes)

    resolved_vault = _prompt_vault(vault, assume_yes)
    if reset and not assume_yes:
        _explain(_SetupPrompt.RESET)
        if not typer.confirm(
            "Reset Datacron configuration and generated index for this vault?",
            default=False,
        ):
            _print("Reset cancelled; nothing changed.")
            _log_completion("setup", started)
            return
    resolved_client = _prompt_client(client, assume_yes)
    resolved_scope = _prompt_scope(scope, resolved_client, assume_yes)
    resolved_durability = _prompt_durability(durability, assume_yes)
    resolved_enable_write, resolved_write_paths = _prompt_write(
        enable_write, write_path, resolved_vault, assume_yes
    )
    resolved_machine_wide_write, replace_existing_write_env = _prompt_machine_wide_write(
        machine_wide_write,
        resolved_enable_write,
        resolved_write_paths,
        assume_yes,
    )
    if read_only or assume_yes:
        resolved_read_only = read_only
    else:
        _explain(_SetupPrompt.READ_ONLY)
        resolved_read_only = typer.confirm("Configure certified read-only mode?", default=False)
    if protocol_enabled or assume_yes:
        resolved_protocol = protocol_enabled
    else:
        _explain(_SetupPrompt.PROTOCOL)
        resolved_protocol = typer.confirm(
            "Install the Datacron memory protocol in detected client instructions?",
            default=False,
        )

    plan = SetupPlan(
        vault_path=resolved_vault,
        build_index=build_index,
        enable_write=resolved_enable_write,
        write_paths=resolved_write_paths,
        machine_wide_write=resolved_machine_wide_write,
        replace_existing_write_env=replace_existing_write_env,
        client=resolved_client,
        install_scope=resolved_scope,
        durability=resolved_durability,
        read_only=resolved_read_only,
        force=force,
        reset=reset,
    )

    try:
        result = asyncio.run(run_setup(plan))
    except (ResetGuardError, ResetExecutionError) as exc:
        _error(str(exc))
    except (ValueError, NotADirectoryError) as exc:
        _error(str(exc))

    _render_setup_result(result)
    protocol_failed = False
    if resolved_protocol:
        protocol_outcomes = _install_setup_protocol(
            resolved_client,
            resolved_scope,
            project_dir=Path.cwd().resolve(),
        )
        protocol_failed = _render_protocol_outcomes(protocol_outcomes, operation="install")
    _log_completion("setup", started)
    if protocol_failed:
        raise typer.Exit(code=1)


# Installer reset invocation:
# datacron.exe setup --reset --yes --client all --scope both --vault "<vault>"


def _guard_vault_target(vault_root: Path) -> Path:
    """Reject the user profile root as a vault target, regardless of how it was chosen.

    A vault rooted at the profile directory would put every client config, the
    sidecar index, and the write allowlist on top of the user's entire home
    tree. There is no legitimate setup for it, so this fails closed even when
    the path was passed explicitly.
    """
    if vault_root == Path.home().resolve():
        _error(
            f"Refusing to use the user profile root as a vault ({vault_root}). "
            "Create a dedicated directory and pass it with --vault."
        )
    return vault_root


def _prompt_vault(vault: Path | None, assume_yes: bool) -> Path:
    """Resolve the setup vault root without ever adopting a directory silently.

    Non-interactive runs (``--yes``) require an explicit source: the ``--vault``
    flag, ``DATACRON_VAULT_ROOT``, or an existing ``.datacron/VAULT.yaml`` in
    the current directory. A bare cwd/home fallback is never accepted.
    """
    if vault is not None:
        return _guard_vault_target(vault.expanduser().resolve())
    if assume_yes:
        settings = get_settings()
        if settings.vault_root is not None:
            return _guard_vault_target(settings.vault_root.expanduser().resolve())
        cwd = Path.cwd().resolve()
        if sidecar_vault_config(cwd).exists():
            return _guard_vault_target(cwd)
        _error(
            "Non-interactive setup (--yes) needs an explicit vault. Pass --vault, "
            "set DATACRON_VAULT_ROOT, or run from a directory that already "
            "contains .datacron/VAULT.yaml."
        )
    _explain(_SetupPrompt.VAULT)
    answer = typer.prompt("Vault path", default=str(Path.cwd()))
    return _guard_vault_target(Path(answer).expanduser().resolve())


def _prompt_client(client: str | None, assume_yes: bool) -> str:
    if client is not None:
        if client not in CLIENT_CHOICES:
            _error(f"Unknown client {client!r}. Expected one of {list(CLIENT_CHOICES)}.")
        return client
    if assume_yes:
        return CLIENT_ALL
    _explain(_SetupPrompt.CLIENT)
    answer: str = typer.prompt(
        "MCP client",
        default=CLIENT_ALL,
        type=click.Choice(list(CLIENT_CHOICES)),
    )
    return answer


def _prompt_scope(scope: str | None, client: str, assume_yes: bool) -> str:
    if scope is not None:
        if scope not in INSTALL_SCOPE_CHOICES:
            _error(f"Unknown scope {scope!r}. Expected one of {list(INSTALL_SCOPE_CHOICES)}.")
        return scope
    if client != CLIENT_ALL or assume_yes:
        return INSTALL_SCOPE_BOTH
    _explain(_SetupPrompt.SCOPE)
    answer: str = typer.prompt(
        "Install scope",
        default=INSTALL_SCOPE_BOTH,
        type=click.Choice(list(INSTALL_SCOPE_CHOICES)),
    )
    return answer


def _prompt_durability(durability: str | None, assume_yes: bool) -> str:
    if durability is not None:
        return durability
    if assume_yes:
        return DEFAULT_DURABILITY_MODE
    _explain(_SetupPrompt.DURABILITY)
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
) -> tuple[bool, list[Path]]:
    if write_path is not None:
        return True, [write_path.expanduser().resolve()]
    if enable_write:
        return True, _prompt_write_paths(vault_root, assume_yes)
    if assume_yes:
        return False, []
    _explain(_SetupPrompt.WRITE)
    if not typer.confirm(
        "Let my AI assistants write notes (in 3 dedicated subfolders only)?",
        default=False,
    ):
        return False, []
    return True, _prompt_write_paths(vault_root, assume_yes)


def _prompt_write_paths(vault_root: Path, assume_yes: bool) -> list[Path]:
    default_paths = [vault_root / name for name in DEFAULT_WRITE_SUBFOLDERS]
    if assume_yes:
        return default_paths
    default_value = os.pathsep.join(str(path) for path in default_paths)
    _explain(
        _SetupPrompt.WRITE_PATHS,
        default_paths=", ".join(DEFAULT_WRITE_SUBFOLDERS),
    )
    answer = typer.prompt(
        f"Write-allowlisted directories (separate with {os.pathsep!r})",
        default=default_value,
    )
    paths = [part.strip() for part in answer.split(os.pathsep) if part.strip()]
    if not paths:
        _error("At least one write-allowlisted directory is required.")
    return [Path(path).expanduser().resolve() for path in paths]


def _prompt_machine_wide_write(
    requested: bool,
    enable_write: bool,
    write_paths: list[Path],
    assume_yes: bool,
) -> tuple[bool, bool]:
    """Collect the explicit user-environment opt-in and replacement choice."""
    if not enable_write:
        if requested:
            _error("--machine-wide-write requires --enable-write or --write-path.")
        return False, False
    if not requested:
        if assume_yes:
            return False, False
        _explain(_SetupPrompt.MACHINE_WIDE_WRITE)
        requested = typer.confirm(
            "Remember this permission for AI assistants installed later?",
            default=False,
        )
    if not requested:
        return False, False

    current = get_user_write_env()
    requested_value = os.pathsep.join(str(path) for path in write_paths)
    replace = False
    if current is not None:
        _print(f"Current user write allowlist: {current}")
        if current != requested_value:
            if assume_yes:
                _print("Existing user write allowlist differs; keeping it unchanged.")
            else:
                _explain(_SetupPrompt.REPLACE_WRITE_ENV)
                replace = typer.confirm(
                    "Replace the existing user write allowlist?",
                    default=False,
                )
    return True, replace


def _render_machine_write_env(result: SetupResult) -> None:
    """Render the user-environment outcome without obscuring setup's main summary."""
    env_result = result.machine_write_env
    if env_result is None:
        return
    if env_result.action == "preserved":
        _print(f"  user env:   kept existing -> {env_result.effective_value}")
        if env_result.export_command is not None:
            _print("  user env:   add this line to your shell profile:")
            _print(env_result.export_command)
    elif env_result.action == "unchanged":
        _print(f"  user env:   already set -> {env_result.effective_value}")
    elif env_result.action == "manual-export":
        _print("  user env:   add this line to your shell profile:")
        _print(env_result.export_command or "")
    else:
        _print(f"  user env:   {env_result.action} -> {env_result.effective_value}")


def _render_setup_result(result: SetupResult) -> None:
    _print("")
    _print("Datacron setup complete.")
    _print(f"  vault:      {result.bootstrap.vault_path}")
    if result.reset_result is not None:
        config_status = "removed" if result.reset_result.config_removed else "not present"
        index_status = "removed" if result.reset_result.index_removed else "not present"
        _print(f"  reset:      config {config_status} / index {index_status}")
    if result.indexed_notes is not None:
        _print(f"  indexed:    {result.indexed_notes} notes")
    elif result.index_error is not None:
        _print(f"  index:      deferred - {result.index_error}")
        _print("  action:     run `datacron index` when indexing is available")
    else:
        _print("  index:      skipped (--no-index)")
    if result.write_paths:
        _print(
            f"  writing:    enabled -> {os.pathsep.join(str(path) for path in result.write_paths)}"
        )
    else:
        _print("  writing:    disabled")
    _render_machine_write_env(result)
    _print(f"  durability: {result.durability}")
    _print(f"  read-only:  {'yes' if result.read_only else 'no'}")
    if result.client_config_path is not None:
        _print(f"  client:     {result.client_config_path}")
        _print("Restart Claude Desktop for the change to take effect.")
    if result.stdio_config is not None:
        _print("")
        _print("Add this server to your Claude Code (stdio) MCP config:")
        _print(result.stdio_config)
    if result.client_installs:
        _print("")
        _print("MCP clients registered:")
        for outcome in result.client_installs:
            mark = "ok " if outcome.installed else "err"
            detail = "" if outcome.installed else f" - {outcome.detail}"
            _print(
                f"  [{mark}] {outcome.display_name} ({outcome.scope}): "
                f"{outcome.config_path}{detail}"
            )
    for warning in result.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if result.machine_write_env is not None:
        _print("Restart all already-open MCP clients to inherit the user environment.")
    _print("Verify from your client with get_health, or run `datacron status`.")


@app.command()
def unregister(
    client: str | None = typer.Option(
        CLIENT_ALL,
        "--client",
        help="Client identifier to clean, or all (default).",
    ),
    scope: str | None = typer.Option(
        INSTALL_SCOPE_BOTH,
        "--scope",
        help=f"Config scope ({', '.join(INSTALL_SCOPE_CHOICES)}); defaults to both.",
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        "-v",
        help=_VAULT_ROOT_HELP,
    ),
    assume_yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Remove entries without confirmation.",
    ),
) -> None:
    """Remove Datacron's MCP entry from existing client configs."""
    resolved_client = client or CLIENT_ALL
    resolved_scope = scope or INSTALL_SCOPE_BOTH
    started = _log_invocation(
        "unregister",
        client=resolved_client,
        scope=resolved_scope,
        assume_yes=assume_yes,
    )

    if resolved_client != CLIENT_ALL and resolved_client not in ALL_CLIENT_IDS:
        _error(
            f"Unknown client {resolved_client!r}. Expected 'all' or one of {list(ALL_CLIENT_IDS)}."
        )
    if resolved_scope not in INSTALL_SCOPE_CHOICES:
        _error(f"Unknown scope {resolved_scope!r}. Expected one of {list(INSTALL_SCOPE_CHOICES)}.")

    scopes = _scopes_for(resolved_scope)
    project_dir: Path | None = None
    if SCOPE_PROJECT in scopes:
        project_dir = _resolve_vault_root(vault, get_settings())

    include = None if resolved_client == CLIENT_ALL else (resolved_client,)
    targets = discover_unregistration_targets(
        scopes=scopes,
        project_dir=project_dir,
        include=include,
    )
    if not targets:
        _print("No client config found; nothing to unregister.")
        _log_completion("unregister", started)
        return

    if not assume_yes and not typer.confirm(
        f"Remove Datacron from {len(targets)} client config(s)?",
        default=True,
    ):
        _print("Aborted; nothing changed.")
        _log_completion("unregister", started)
        return

    outcomes = unregister_targets(targets)
    _print("MCP client configs:")
    for outcome in outcomes:
        mark = "ok " if outcome.successful else "err"
        state = (
            "removed"
            if outcome.changed
            else "already unregistered"
            if outcome.successful
            else outcome.detail
        )
        message = (
            f"  [{mark}] {outcome.display_name} ({outcome.scope}): {outcome.config_path} - {state}"
        )
        if outcome.successful:
            _print(message)
        else:
            typer.secho(message, fg=typer.colors.YELLOW, err=True)

    failed = any(not outcome.successful for outcome in outcomes)
    _log_completion("unregister", started)
    if failed:
        raise typer.Exit(code=1)


# The Windows uninstaller will invoke this before deleting datacron.exe:
# datacron.exe unregister --yes --client all --scope both --vault "<vault>"


# ---------------------------------------------------------------------------
# `datacron protocol ...`
# ---------------------------------------------------------------------------


@protocol_app.command("install")
def protocol_install(
    client: str = typer.Option(
        PROTOCOL_ALL,
        "--client",
        help=(f"Client identifier ({', '.join(PROTOCOL_CLIENT_IDS)}), or all detected clients."),
    ),
    scope: str = typer.Option(
        SCOPE_USER,
        "--scope",
        help=f"Protocol target scope ({', '.join(INSTALL_SCOPE_CHOICES)}).",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        help="Code project root for project-scope protocol targets; defaults to cwd.",
    ),
) -> None:
    """Install or refresh the marked Datacron memory protocol block."""
    started = _log_invocation("protocol.install", client=client, scope=scope, project=project)
    try:
        scopes, project_dir = _resolve_protocol_scopes(scope, project)
        outcomes: list[ProtocolInstallOutcome] = []
        for concrete_scope in scopes:
            outcomes.extend(
                install_memory_protocol(
                    client,
                    project_dir=project_dir if concrete_scope == SCOPE_PROJECT else None,
                    scope=concrete_scope,
                )
            )
    except ValueError as exc:
        _error(str(exc))
    failed = _render_protocol_outcomes(outcomes, operation="install")
    _log_completion("protocol.install", started)
    if failed:
        raise typer.Exit(code=1)


@protocol_app.command("uninstall")
def protocol_uninstall(
    client: str = typer.Option(
        PROTOCOL_ALL,
        "--client",
        help=(f"Client identifier ({', '.join(PROTOCOL_CLIENT_IDS)}), or all relevant clients."),
    ),
    scope: str = typer.Option(
        SCOPE_USER,
        "--scope",
        help=f"Protocol target scope ({', '.join(INSTALL_SCOPE_CHOICES)}).",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        help="Code project root for project-scope protocol targets; defaults to cwd.",
    ),
) -> None:
    """Remove only the marked Datacron memory protocol block."""
    started = _log_invocation("protocol.uninstall", client=client, scope=scope, project=project)
    try:
        scopes, project_dir = _resolve_protocol_scopes(scope, project)
        outcomes: list[ProtocolInstallOutcome] = []
        for concrete_scope in scopes:
            outcomes.extend(
                uninstall_memory_protocol(
                    client,
                    project_dir=project_dir if concrete_scope == SCOPE_PROJECT else None,
                    scope=concrete_scope,
                )
            )
    except ValueError as exc:
        _error(str(exc))
    failed = _render_protocol_outcomes(outcomes, operation="uninstall")
    _log_completion("protocol.uninstall", started)
    if failed:
        raise typer.Exit(code=1)


def _resolve_protocol_scopes(
    scope: str,
    project: Path | None,
) -> tuple[tuple[str, ...], Path | None]:
    """Validate a CLI scope selector and resolve its optional code-project root."""
    if scope not in INSTALL_SCOPE_CHOICES:
        raise ValueError(
            f"Unknown protocol scope {scope!r}. Expected one of {list(INSTALL_SCOPE_CHOICES)}."
        )
    scopes = _scopes_for(scope)
    project_dir = None
    if SCOPE_PROJECT in scopes:
        project_dir = (project or Path.cwd()).expanduser().resolve()
    return scopes, project_dir


def _install_setup_protocol(
    client: str,
    install_scope: str,
    *,
    project_dir: Path,
) -> list[ProtocolInstallOutcome]:
    """Install opted-in setup protocol targets without conflating project and vault roots."""
    outcomes: list[ProtocolInstallOutcome] = []
    for concrete_scope in _scopes_for(install_scope):
        if concrete_scope == SCOPE_USER or client == CLIENT_ALL:
            protocol_client = PROTOCOL_ALL
        elif client in PROTOCOL_CLIENT_IDS:
            protocol_client = client
        else:
            continue
        outcomes.extend(
            install_memory_protocol(
                protocol_client,
                project_dir=project_dir if concrete_scope == SCOPE_PROJECT else None,
                scope=concrete_scope,
            )
        )
    return outcomes


def _render_protocol_outcomes(
    outcomes: list[ProtocolInstallOutcome],
    *,
    operation: str,
) -> bool:
    """Render protocol instruction outcomes and return whether any failed."""
    if not outcomes:
        if operation == "install":
            _print("No supported clients detected; protocol instructions were not installed.")
        else:
            _print("No supported client instruction files found; nothing to uninstall.")
        return False

    _print("Protocol client instructions:")
    for outcome in outcomes:
        if outcome.skipped:
            target = f"{outcome.instruction_path} - " if outcome.instruction_path else ""
            _print(f"  [skip] {outcome.display_name}: {target}{outcome.detail}")
            if outcome.manual_instructions:
                for line in outcome.manual_instructions.splitlines():
                    _print(f"         {line}" if line else "")
            continue
        mark = "ok " if outcome.successful else "err"
        path = outcome.instruction_path or "n/a"
        message = f"  [{mark}] {outcome.display_name}: {path} - {outcome.detail}"
        if outcome.successful:
            _print(message)
        else:
            typer.secho(message, fg=typer.colors.YELLOW, err=True)
    return any(not outcome.successful for outcome in outcomes)


# ---------------------------------------------------------------------------
# `datacron mcp ...`
# ---------------------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve(
    vault: Path | None = typer.Option(None, "--vault", "-v", help=_VAULT_ROOT_HELP),
) -> None:
    """Run the MCPServer stdio server.

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
        help=_VAULT_ROOT_HELP,
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
    (set by the installer), or uses the current directory only when it contains
    ``.datacron/VAULT.yaml``.
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
