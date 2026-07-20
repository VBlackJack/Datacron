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
"""Guided end-to-end setup for a Datacron vault.

:func:`run_setup` is the non-interactive orchestration behind the
``datacron setup`` command: it initializes the sidecar, builds the search
index, and (optionally) wires the vault into an MCP client. Keeping the logic
here - as a pure function over a :class:`SetupPlan` - makes the whole flow unit
testable without simulating terminal prompts; the CLI layer only gathers the
plan (from flags or interactive questions) and renders the result.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import shutil
import stat
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from datacron.bootstrap import BootstrapResult, initialize_vault
from datacron.core.config import (
    INDEX_DIR_NAME,
    SIDECAR_DIR_NAME,
    VALID_DURABILITY_MODES,
    VAULT_CONFIG_FILENAME,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.logger import get_logger
from datacron.core.paths import sidecar_index_db, sidecar_index_dir, sidecar_vault_config
from datacron.core.vault import build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.installers.claude_desktop import (
    ClaudeDesktopConfigError,
    install_claude_desktop_config,
    resolve_mcp_invocation,
)
from datacron.installers.mcp_clients import (
    ALL_CLIENT_IDS,
    SCOPE_PROJECT,
    SCOPE_USER,
    InstallOutcome,
    discover_targets,
    install_targets,
)

__all__ = [
    "CLIENT_ALL",
    "CLIENT_CHOICES",
    "CLIENT_CLAUDE_CODE",
    "CLIENT_CLAUDE_DESKTOP",
    "CLIENT_NONE",
    "DEFAULT_WRITE_SUBFOLDERS",
    "INSTALL_SCOPE_CHOICES",
    "MachineWriteEnvResult",
    "ResetExecutionError",
    "ResetGuardError",
    "ResetResult",
    "SetupPlan",
    "SetupResult",
    "claude_code_stdio_config",
    "configure_user_write_env",
    "get_user_write_env",
    "reset_user_state",
    "run_setup",
]

_LOGGER = get_logger(__name__)

CLIENT_ALL: Final[str] = "all"
CLIENT_CLAUDE_DESKTOP: Final[str] = "claude-desktop"
CLIENT_CLAUDE_CODE: Final[str] = "claude-code"
CLIENT_NONE: Final[str] = "none"
CLIENT_CHOICES: Final[tuple[str, ...]] = (
    CLIENT_ALL,
    *ALL_CLIENT_IDS,
    CLIENT_NONE,
)

INSTALL_SCOPE_USER: Final[str] = "user"
INSTALL_SCOPE_PROJECT: Final[str] = "project"
INSTALL_SCOPE_BOTH: Final[str] = "both"
INSTALL_SCOPE_CHOICES: Final[tuple[str, ...]] = (
    INSTALL_SCOPE_BOTH,
    INSTALL_SCOPE_USER,
    INSTALL_SCOPE_PROJECT,
)

# Default write-enabled subfolders. The vault root itself is deliberately never
# allowlisted by setup.
DEFAULT_WRITE_SUBFOLDERS: Final[tuple[str, ...]] = ("_memory", "_drafts", "_journal")

# Runtime environment keys embedded into the MCP client config. Datacron's
# Settings derive these from the ``DATACRON_`` prefix; they are named
# explicitly here because the client config is plain JSON, not a Settings load.
_ENV_VAULT_ROOT: Final[str] = "DATACRON_VAULT_ROOT"
_ENV_READ_PATHS: Final[str] = "DATACRON_READ_PATHS"
_ENV_WRITE_PATHS: Final[str] = "DATACRON_WRITE_PATHS"
_ENV_DURABILITY: Final[str] = "DATACRON_DURABILITY"
_ENV_READ_ONLY: Final[str] = "DATACRON_READ_ONLY"
_ENV_TRUE: Final[str] = "true"

_WINDOWS_ENVIRONMENT_KEY: Final[str] = "Environment"
_WINDOWS_ENVIRONMENT_BROADCAST: Final[str] = "Environment"
_HWND_BROADCAST: Final[int] = 0xFFFF
_WM_SETTINGCHANGE: Final[int] = 0x001A
_SMTO_ABORTIFHUNG: Final[int] = 0x0002
_BROADCAST_TIMEOUT_MS: Final[int] = 5000

_KNOWN_SYNC_PATH_PARTS: Final[frozenset[str]] = frozenset(
    {"dropbox", "google drive", "icloud drive", "onedrive", "syncthing"}
)

_MCP_SERVER_KEY: Final[str] = "datacron"

# Win32 FILE_ATTRIBUTE_REPARSE_POINT. The stdlib exposes the stat constant only
# on Windows, so cross-platform type checking uses the portable numeric value.
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400


class ResetGuardError(RuntimeError):
    """Raised when a reset target fails validation before any deletion."""


class ResetExecutionError(RuntimeError):
    """Raised when deletion of a validated reset target fails."""


class _WinregModule(Protocol):
    """Subset of :mod:`winreg` used for the user environment allowlist."""

    HKEY_CURRENT_USER: object
    KEY_READ: int
    KEY_SET_VALUE: int
    REG_EXPAND_SZ: int
    REG_SZ: int
    OpenKey: Callable[[object, str, int, int], AbstractContextManager[object]]
    CreateKeyEx: Callable[[object, str, int, int], AbstractContextManager[object]]
    QueryValueEx: Callable[[object, str], tuple[object, int]]
    SetValueEx: Callable[[object, str, int, int, str], None]


@dataclass(frozen=True)
class ResetResult:
    """Files removed by one surgical reset."""

    config_removed: bool
    index_removed: bool


@dataclass(frozen=True)
class MachineWriteEnvResult:
    """Outcome of the explicit user-wide write-allowlist step."""

    requested_value: str
    effective_value: str
    previous_value: str | None
    action: Literal["created", "replaced", "unchanged", "preserved", "manual-export"]
    export_command: str | None = None


@dataclass(frozen=True)
class SetupPlan:
    """Fully-resolved choices for a setup run.

    All paths are taken as-is; :func:`run_setup` resolves them. This object is
    what the CLI produces from flags and/or interactive answers.

    Attributes:
        vault_path: Markdown vault root to initialize and serve.
        build_index: Whether to build the FTS5 index during setup.
        enable_write: Whether to enable the confined write tools.
        write_paths: Write-allowlisted directories when ``enable_write`` is set.
            An empty list falls back to the three default vault subfolders.
        machine_wide_write: Apply the allowlist to the user environment after
            explicit opt-in.
        replace_existing_write_env: Replace a different existing user value.
            False preserves it; setup never merges values silently.
        client: Target MCP client (:data:`CLIENT_CHOICES`). ``all`` auto-detects
            every installed client and registers Datacron with each.
        install_scope: Which config scopes to write for detected clients
            (:data:`INSTALL_SCOPE_CHOICES`).
        durability: Durability mode (:data:`VALID_DURABILITY_MODES`).
        read_only: Whether to run the server in certified read-only mode.
        force: Overwrite an existing ``VAULT.yaml``.
        reset: Remove the generated config and index before setup.
    """

    vault_path: Path
    build_index: bool = True
    enable_write: bool = False
    write_paths: list[Path] = field(default_factory=list)
    machine_wide_write: bool = False
    replace_existing_write_env: bool = False
    client: str = CLIENT_ALL
    install_scope: str = INSTALL_SCOPE_BOTH
    durability: str = "best-effort"
    read_only: bool = False
    force: bool = False
    reset: bool = False


@dataclass(frozen=True)
class SetupResult:
    """Outcome of :func:`run_setup`, for rendering a summary.

    Attributes:
        bootstrap: The vault initialization result.
        indexed_notes: Notes indexed during setup, or ``None`` if skipped.
        index_error: Index failure details, or ``None`` when built or intentionally skipped.
        client_config_path: Written MCP client config, or ``None`` when no
            file was written (``claude-code`` and ``none``).
        stdio_config: A ready-to-paste stdio MCP config snippet for clients that
            Datacron does not write directly (``claude-code``), else ``None``.
        write_paths: The write-allowlisted directories; empty when writing stays
            disabled.
        machine_write_env: Result of the explicit user-wide environment step.
        read_only: Whether certified read-only mode was selected.
        durability: The selected durability mode.
        client_installs: Per-client registration outcomes for ``client=all``.
        reset_result: Surgical reset outcome, when reset was requested.
        warnings: Non-fatal messages surfaced to the operator.
    """

    bootstrap: BootstrapResult
    indexed_notes: int | None
    index_error: str | None
    client_config_path: Path | None
    write_paths: list[Path]
    read_only: bool
    durability: str
    machine_write_env: MachineWriteEnvResult | None = None
    stdio_config: str | None = None
    client_installs: list[InstallOutcome] = field(default_factory=list)
    reset_result: ResetResult | None = None
    warnings: list[str] = field(default_factory=list)


def _validate_plan(plan: SetupPlan) -> None:
    """Reject a plan with out-of-range enumerated values before any I/O."""
    if plan.client not in CLIENT_CHOICES:
        raise ValueError(f"Unknown client {plan.client!r}. Expected one of {list(CLIENT_CHOICES)}.")
    if plan.install_scope not in INSTALL_SCOPE_CHOICES:
        raise ValueError(
            f"Unknown install scope {plan.install_scope!r}. "
            f"Expected one of {list(INSTALL_SCOPE_CHOICES)}."
        )
    if plan.durability not in VALID_DURABILITY_MODES:
        raise ValueError(
            f"Unknown durability {plan.durability!r}. "
            f"Expected one of {sorted(VALID_DURABILITY_MODES)}."
        )
    if plan.machine_wide_write and not plan.enable_write:
        raise ValueError("Machine-wide write configuration requires enable_write.")


def _scopes_for(install_scope: str) -> tuple[str, ...]:
    """Map an install-scope selection to concrete client config scopes."""
    if install_scope == INSTALL_SCOPE_USER:
        return (SCOPE_USER,)
    if install_scope == INSTALL_SCOPE_PROJECT:
        return (SCOPE_PROJECT,)
    return (SCOPE_USER, SCOPE_PROJECT)


def _is_linked_path(path: Path) -> bool:
    """Return whether ``path`` is a symlink or Windows reparse point.

    Inspection is deliberately fail-closed: only a missing path is considered
    safe. Any other inspection failure prevents the reset.
    """
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ResetGuardError(f"Cannot inspect reset target '{path}': {exc}") from exc

    if stat.S_ISLNK(path_stat.st_mode):
        return True
    if sys.platform.casefold() == "win32":
        attributes = getattr(path_stat, "st_file_attributes", None)
        if attributes is None:
            raise ResetGuardError(f"Cannot read file attributes for '{path}' on Windows.")
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _guard_reset_paths(
    vault_root: Path,
    config_path: Path,
    index_dir: Path,
) -> tuple[Path, Path]:
    """Validate the two allowlisted reset targets without following links."""
    vault = Path(os.path.abspath(vault_root))
    sidecar = Path(os.path.abspath(vault / SIDECAR_DIR_NAME))
    config = Path(os.path.abspath(config_path))
    index = Path(os.path.abspath(index_dir))

    if sidecar.name != SIDECAR_DIR_NAME or sidecar.parent != vault:
        raise ResetGuardError(f"Invalid Datacron sidecar path for reset: '{sidecar}'.")
    if config.name != VAULT_CONFIG_FILENAME or config.parent != sidecar:
        raise ResetGuardError(f"Invalid Datacron config path for reset: '{config}'.")
    if index.name != INDEX_DIR_NAME or index.parent != sidecar or index == index.parent:
        raise ResetGuardError(f"Invalid Datacron index path for reset: '{index}'.")

    inspected_paths = (
        ("vault", vault),
        ("sidecar", sidecar),
        ("config", config),
        ("index", index),
    )
    for label, path in inspected_paths:
        if _is_linked_path(path):
            raise ResetGuardError(
                f"Datacron reset refused: {label} path is a symlink or reparse point: '{path}'."
            )

    directory_paths = (("vault", vault), ("sidecar", sidecar), ("index", index))
    for label, path in directory_paths:
        if path.exists() and not path.is_dir():
            raise ResetGuardError(
                f"Datacron reset refused: {label} path must be a directory: '{path}'."
            )
    if config.exists() and not config.is_file():
        raise ResetGuardError(f"Datacron reset refused: config path must be a file: '{config}'.")

    return config, index


def reset_user_state(
    vault_root: Path,
    *,
    config_path: Path | None = None,
    index_dir: Path | None = None,
) -> ResetResult:
    """Remove only the allowlisted Datacron config and generated index."""
    config_candidate = sidecar_vault_config(vault_root) if config_path is None else config_path
    index_candidate = sidecar_index_dir(vault_root) if index_dir is None else index_dir
    config, index = _guard_reset_paths(vault_root, config_candidate, index_candidate)

    index_removed = False
    if index.exists():
        try:
            shutil.rmtree(index)
        except OSError as exc:
            raise ResetExecutionError(
                "Could not reset Datacron config and index. Close all AI clients using "
                f"Datacron, then retry. Failed path: '{index}'."
            ) from exc
        index_removed = True

    config_removed = False
    if config.exists() or config.is_symlink():
        try:
            config.unlink()
        except OSError as exc:
            raise ResetExecutionError(
                "Could not reset Datacron config and index. Close all AI clients using "
                f"Datacron, then retry. Failed path: '{config}'."
            ) from exc
        config_removed = True

    _LOGGER.info(
        "cli.setup reset completed (vault=%s, config_removed=%s, index_removed=%s)",
        vault_root,
        config_removed,
        index_removed,
    )
    return ResetResult(config_removed=config_removed, index_removed=index_removed)


async def _build_index(vault_root: Path, settings: Settings) -> int:
    """Reconcile the FTS5 index for ``vault_root`` and return notes indexed.

    Mirrors the incremental path of ``datacron index``: unchanged notes are
    skipped, changed ones re-chunked, vanished ones deleted.
    """
    config = load_vault_config(sidecar_vault_config(vault_root)) or VaultConfig()
    reader = build_configured_reader(vault_root)
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    await store.open(sidecar_index_db(vault_root))
    try:
        stats = await reconcile(store, reader, chunker, mtime_gate=True)
    finally:
        await store.close()
    return int(stats["reindexed_notes"])


def _resolve_write_paths(plan: SetupPlan, vault_root: Path) -> list[Path]:
    """Resolve explicit write roots or the three confined defaults."""
    candidates = plan.write_paths or [vault_root / name for name in DEFAULT_WRITE_SUBFOLDERS]
    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path not in seen:
            resolved.append(path)
            seen.add(path)
    return resolved


def _serialize_write_paths(write_paths: list[Path]) -> str:
    """Serialize a write allowlist with the current platform's path separator."""
    return os.pathsep.join(str(path) for path in write_paths)


def _load_winreg() -> _WinregModule:
    """Load the Windows-only registry module without breaking Unix imports."""
    return cast("_WinregModule", importlib.import_module("winreg"))


def _is_windows() -> bool:
    """Return whether user environment persistence uses the Windows registry."""
    return os.name == "nt"


def _read_windows_user_write_env(winreg: _WinregModule) -> tuple[str | None, int | None]:
    """Read the current HKCU write allowlist and its registry value type."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _WINDOWS_ENVIRONMENT_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            raw_value, value_type = winreg.QueryValueEx(key, _ENV_WRITE_PATHS)
    except FileNotFoundError:
        return None, None
    return str(raw_value), value_type


def get_user_write_env() -> str | None:
    """Return the current user-level write allowlist, if one is visible."""
    if not _is_windows():
        return os.environ.get(_ENV_WRITE_PATHS)
    value, _value_type = _read_windows_user_write_env(_load_winreg())
    return value


def _write_windows_user_write_env(
    winreg: _WinregModule,
    value: str,
    existing_type: int | None,
) -> None:
    """Write ``DATACRON_WRITE_PATHS`` under ``HKCU\\Environment``."""
    value_type = (
        existing_type if existing_type in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} else winreg.REG_SZ
    )
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        _WINDOWS_ENVIRONMENT_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, _ENV_WRITE_PATHS, 0, value_type, value)


def _broadcast_environment_change() -> None:
    """Notify Windows applications that the user environment changed."""
    import ctypes  # noqa: PLC0415

    result = ctypes.c_size_t()
    windll_attribute = "windll"
    windll = getattr(ctypes, windll_attribute)
    sent = windll.user32.SendMessageTimeoutW(
        _HWND_BROADCAST,
        _WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p(_WINDOWS_ENVIRONMENT_BROADCAST),
        _SMTO_ABORTIFHUNG,
        _BROADCAST_TIMEOUT_MS,
        ctypes.byref(result),
    )
    if sent == 0:
        get_last_error_attribute = "get_last_error"
        last_error = getattr(ctypes, get_last_error_attribute)()
        raise OSError(last_error, "Could not broadcast the user environment change")


def configure_user_write_env(
    write_paths: list[Path],
    *,
    replace_existing: bool = False,
) -> MachineWriteEnvResult:
    """Apply or render the explicit user-wide write allowlist without silent merges."""
    requested = _serialize_write_paths(write_paths)
    if _is_windows():
        winreg = _load_winreg()
        previous, existing_type = _read_windows_user_write_env(winreg)
        if previous == requested:
            return MachineWriteEnvResult(requested, requested, previous, "unchanged")
        if previous is not None and not replace_existing:
            return MachineWriteEnvResult(requested, previous, previous, "preserved")
        _write_windows_user_write_env(winreg, requested, existing_type)
        _broadcast_environment_change()
        action: Literal["created", "replaced"] = "created" if previous is None else "replaced"
        return MachineWriteEnvResult(requested, requested, previous, action)

    previous = os.environ.get(_ENV_WRITE_PATHS)
    if previous is not None and previous != requested and not replace_existing:
        export = f"export {_ENV_WRITE_PATHS}={shlex.quote(previous)}"
        return MachineWriteEnvResult(requested, previous, previous, "preserved", export)
    export = f"export {_ENV_WRITE_PATHS}={shlex.quote(requested)}"
    return MachineWriteEnvResult(requested, requested, previous, "manual-export", export)


def _single_writer_warning(vault_root: Path) -> str | None:
    """Warn heuristically for network and well-known synchronized vault paths."""
    path_text = str(vault_root)
    is_unc = path_text.startswith("\\\\")
    parts = {part.casefold() for part in vault_root.parts}
    is_synced = any(part.startswith(known) for part in parts for known in _KNOWN_SYNC_PATH_PARTS)
    if not is_unc and not is_synced:
        return None
    return (
        "The vault appears to use a network or synchronized path. Keep exactly one active "
        "writer; concurrent multi-machine writes are unsupported."
    )


def _prepare_write_setup(
    plan: SetupPlan,
    vault_root: Path,
    warnings: list[str],
) -> tuple[list[Path], MachineWriteEnvResult | None]:
    """Create opted-in write roots and optionally configure the user environment."""
    if not plan.enable_write:
        return [], None

    write_paths = _resolve_write_paths(plan, vault_root)
    for write_path in write_paths:
        write_path.mkdir(parents=True, exist_ok=True)
    single_writer_warning = _single_writer_warning(vault_root)
    if single_writer_warning is not None:
        warnings.append(single_writer_warning)
    if not plan.machine_wide_write:
        return write_paths, None

    try:
        machine_write_env = configure_user_write_env(
            write_paths,
            replace_existing=plan.replace_existing_write_env,
        )
    except OSError as exc:
        warnings.append(f"User environment not updated: {exc}")
        _LOGGER.warning("cli.setup user environment update failed: %s", exc)
        return write_paths, None
    _LOGGER.info("cli.setup user environment action=%s", machine_write_env.action)
    return write_paths, machine_write_env


async def run_setup(plan: SetupPlan) -> SetupResult:
    """Run the full setup sequence for ``plan`` and return a summary.

    Steps: reset the generated config and index (optional), initialize the
    sidecar, resolve write settings, configure the selected MCP client, then build
    the index (optional). Index failures are deferred so they never roll back a
    successful client configuration. Client configuration failures are captured as
    warnings rather than aborting an otherwise-successful vault initialization.

    Args:
        plan: The resolved setup choices.

    Returns:
        A :class:`SetupResult` describing everything that was done.

    Raises:
        ValueError: If the plan carries an invalid enumerated value.
        NotADirectoryError: If the vault path exists but is not a directory.
        ResetGuardError: If a reset target fails safety validation.
        ResetExecutionError: If a validated reset target cannot be removed.
    """
    _validate_plan(plan)
    warnings: list[str] = []
    settings = get_settings()

    _LOGGER.info("cli.setup started (vault=%s, client=%s)", plan.vault_path, plan.client)
    reset_result: ResetResult | None = None
    if plan.reset:
        reset_result = reset_user_state(plan.vault_path)
    bootstrap = initialize_vault(plan.vault_path, force=plan.force)
    vault_root = bootstrap.vault_path

    write_paths, machine_write_env = _prepare_write_setup(plan, vault_root, warnings)

    extra_env = _build_extra_env(plan, write_paths)
    client_config_path: Path | None = None
    stdio_config: str | None = None
    client_installs: list[InstallOutcome] = []
    if plan.client == CLIENT_ALL:
        client_installs = _install_detected_clients(
            plan,
            vault_root,
            extra_env,
            warnings,
            include=None,
        )
    elif plan.client == CLIENT_CLAUDE_DESKTOP:
        try:
            client_config_path = install_claude_desktop_config(vault_root, extra_env=extra_env)
        except ClaudeDesktopConfigError as exc:
            warnings.append(f"Claude Desktop config not written: {exc}")
            _LOGGER.warning("cli.setup client config failed: %s", exc)
    elif plan.client == CLIENT_CLAUDE_CODE:
        stdio_config = claude_code_stdio_config(vault_root, extra_env)
    elif plan.client != CLIENT_NONE:
        client_installs = _install_detected_clients(
            plan,
            vault_root,
            extra_env,
            warnings,
            include=(plan.client,),
        )

    indexed_notes: int | None = None
    index_error: str | None = None
    if plan.build_index:
        try:
            indexed_notes = await _build_index(vault_root, settings)
            _LOGGER.info("cli.setup indexed %d notes for %s", indexed_notes, vault_root)
        except Exception as exc:
            index_error = f"{type(exc).__name__}: {exc}"

    _LOGGER.info("cli.setup completed for %s", vault_root)
    return SetupResult(
        bootstrap=bootstrap,
        indexed_notes=indexed_notes,
        index_error=index_error,
        client_config_path=client_config_path,
        write_paths=write_paths,
        read_only=plan.read_only,
        durability=plan.durability,
        machine_write_env=machine_write_env,
        stdio_config=stdio_config,
        client_installs=client_installs,
        reset_result=reset_result,
        warnings=warnings,
    )


def _install_detected_clients(
    plan: SetupPlan,
    vault_root: Path,
    extra_env: dict[str, str],
    warnings: list[str],
    *,
    include: tuple[str, ...] | None,
) -> list[InstallOutcome]:
    """Register Datacron with detected MCP clients in the requested scope.

    The vault root doubles as the project directory for project-scope config, so
    opening the vault as a project in an editor picks up Datacron automatically.
    """
    try:
        invocation = resolve_mcp_invocation()
    except ClaudeDesktopConfigError as exc:
        warnings.append(f"Client auto-install skipped: {exc}")
        _LOGGER.warning("cli.setup could not resolve MCP command: %s", exc)
        return []

    env: dict[str, str] = {
        _ENV_VAULT_ROOT: str(vault_root),
        _ENV_READ_PATHS: str(vault_root),
        **extra_env,
    }
    targets = discover_targets(
        scopes=_scopes_for(plan.install_scope),
        project_dir=vault_root,
        include=include,
    )
    if not targets:
        if include is None:
            warnings.append("No MCP clients detected; nothing to auto-install.")
        else:
            warnings.append(
                f"MCP client {include[0]!r} was not detected for scope "
                f"{plan.install_scope!r}; no config written."
            )
        return []
    return install_targets(
        targets,
        command=invocation.command,
        args=list(invocation.args),
        env=env,
    )


def claude_code_stdio_config(vault_root: Path, extra_env: dict[str, str]) -> str:
    """Return a ready-to-paste stdio MCP config snippet for Claude Code.

    Datacron does not write Claude Code's configuration directly; instead the
    operator pastes this JSON into their MCP client settings. The snippet uses
    the launch form for the current installation and embeds the vault root, the
    read allowlist, and any write/durability/read-only options selected during setup.

    Args:
        vault_root: The resolved vault root to serve.
        extra_env: Additional environment produced by :func:`_build_extra_env`.

    Returns:
        A pretty-printed JSON string.
    """
    env: dict[str, str] = {
        _ENV_VAULT_ROOT: str(vault_root),
        _ENV_READ_PATHS: str(vault_root),
        **extra_env,
    }
    invocation = resolve_mcp_invocation()
    snippet = {
        "mcpServers": {
            _MCP_SERVER_KEY: {
                "command": invocation.command,
                "args": list(invocation.args),
                "env": env,
            }
        }
    }
    return json.dumps(snippet, indent=2, sort_keys=True, ensure_ascii=False)


def _build_extra_env(plan: SetupPlan, write_paths: list[Path]) -> dict[str, str]:
    """Build the extra client-config environment from the plan's options."""
    env: dict[str, str] = {_ENV_DURABILITY: plan.durability}
    if write_paths:
        env[_ENV_WRITE_PATHS] = _serialize_write_paths(write_paths)
    if plan.read_only:
        env[_ENV_READ_ONLY] = _ENV_TRUE
    return env
