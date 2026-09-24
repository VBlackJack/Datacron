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
"""Register and unregister the Datacron server in AI MCP client configs.

Datacron speaks MCP over stdio, so any MCP-capable client can use it once its
configuration references the ``datacron-mcp`` command. Each client stores that
configuration differently (path, file format, top-level key), so this module
models one adapter per client:

- Claude Desktop, Claude Code, Cursor, Gemini CLI, Antigravity, LM Studio,
  Windsurf -
  JSON with a top-level ``mcpServers`` object.
- VS Code - JSON with a top-level ``servers`` object and an explicit
  ``type: "stdio"`` per server.
- Codex CLI - TOML with ``[mcp_servers.<name>]`` tables.

Every writer reads the existing file, changes only the Datacron entry, preserves
all other content, and writes back atomically.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import tomli_w

from datacron.core.logger import get_logger
from datacron.installers.claude_desktop import (
    ClaudeDesktopConfigError,
    config_path_for_platform,
)
from datacron.installers.foreign_files import replace_foreign_file

__all__ = [
    "ALL_CLIENT_IDS",
    "SCOPE_PROJECT",
    "SCOPE_USER",
    "ClientTarget",
    "InstallOutcome",
    "UnregisterOutcome",
    "client_display_name",
    "detect_clients",
    "discover_targets",
    "discover_unregistration_targets",
    "install_targets",
    "unregister_targets",
]

_LOGGER = get_logger(__name__)

_SERVER_NAME: Final[str] = "datacron"

SCOPE_USER: Final[str] = "user"
SCOPE_PROJECT: Final[str] = "project"

# Client identifiers.
CLAUDE_DESKTOP: Final[str] = "claude-desktop"
CLAUDE_CODE: Final[str] = "claude-code"
CURSOR: Final[str] = "cursor"
GEMINI_CLI: Final[str] = "gemini-cli"
ANTIGRAVITY: Final[str] = "antigravity"
LMSTUDIO: Final[str] = "lmstudio"
CODEX_CLI: Final[str] = "codex-cli"
WINDSURF: Final[str] = "windsurf"
VS_CODE: Final[str] = "vscode"

ALL_CLIENT_IDS: Final[tuple[str, ...]] = (
    CLAUDE_DESKTOP,
    CLAUDE_CODE,
    CURSOR,
    GEMINI_CLI,
    ANTIGRAVITY,
    LMSTUDIO,
    CODEX_CLI,
    WINDSURF,
    VS_CODE,
)

# Config file formats.
_FMT_JSON_MCPSERVERS: Final[str] = "json-mcpservers"
_FMT_JSON_SERVERS: Final[str] = "json-servers"
_FMT_TOML: Final[str] = "toml"

_LMSTUDIO_PROFILE_RELATIVE_PATH: Final[Path] = Path(".lmstudio")
_LMSTUDIO_CONFIG_RELATIVE_PATH: Final[Path] = _LMSTUDIO_PROFILE_RELATIVE_PATH / "mcp.json"

_DISPLAY_NAMES: Final[dict[str, str]] = {
    CLAUDE_DESKTOP: "Claude Desktop",
    CLAUDE_CODE: "Claude Code",
    CURSOR: "Cursor",
    GEMINI_CLI: "Gemini CLI",
    ANTIGRAVITY: "Antigravity",
    LMSTUDIO: "LM Studio",
    CODEX_CLI: "Codex CLI",
    WINDSURF: "Windsurf",
    VS_CODE: "VS Code",
}


class MCPClientError(RuntimeError):
    """Raised when a client's configuration cannot be parsed or written."""


@dataclass(frozen=True)
class ClientTarget:
    """A concrete config file to install Datacron into.

    Attributes:
        client_id: Stable client identifier (see :data:`ALL_CLIENT_IDS`).
        display_name: Human-readable client name.
        scope: :data:`SCOPE_USER` or :data:`SCOPE_PROJECT`.
        config_path: The configuration file to write.
        fmt: Internal format tag driving the writer selection.
    """

    client_id: str
    display_name: str
    scope: str
    config_path: Path
    fmt: str


@dataclass(frozen=True)
class InstallOutcome:
    """Result of installing Datacron into one :class:`ClientTarget`.

    Attributes:
        client_id: The client identifier.
        display_name: Human-readable client name.
        scope: The installation scope.
        config_path: The configuration file that was targeted.
        installed: ``True`` on success, ``False`` when an error occurred.
        detail: Error message when ``installed`` is ``False``, else ``""``.
    """

    client_id: str
    display_name: str
    scope: str
    config_path: Path
    installed: bool
    detail: str = ""


@dataclass(frozen=True)
class UnregisterOutcome:
    """Result of removing Datacron from one :class:`ClientTarget`."""

    client_id: str
    display_name: str
    scope: str
    config_path: Path
    successful: bool
    changed: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Path resolution (per client, per scope)
# ---------------------------------------------------------------------------


def _vscode_user_dir() -> Path:
    """Return the VS Code user-data ``User`` directory for the current OS."""
    # Bind to a local ``str`` so mypy does not treat the branches as
    # platform-specific dead code on a single-OS check.
    platform = sys.platform
    home = Path.home()
    if platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Code" / "User"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    return base / "Code" / "User"


def _user_config_path(client_id: str) -> Path | None:
    """Return the user-scope config path for ``client_id`` (``None`` if N/A).

    The table is built whole on every call, so asking for Cursor's path used to
    raise on any platform where Claude Desktop has none. A client with no path
    here is exactly what ``None`` means.
    """
    home = Path.home()
    claude_desktop_config = _claude_desktop_config_path()
    paths: dict[str, Path] = {
        CLAUDE_CODE: home / ".claude.json",
        CURSOR: home / ".cursor" / "mcp.json",
        GEMINI_CLI: home / ".gemini" / "settings.json",
        ANTIGRAVITY: home / ".gemini" / "config" / "mcp_config.json",
        LMSTUDIO: home / _LMSTUDIO_CONFIG_RELATIVE_PATH,
        CODEX_CLI: home / ".codex" / "config.toml",
        WINDSURF: home / ".codeium" / "windsurf" / "mcp_config.json",
        VS_CODE: _vscode_user_dir() / "mcp.json",
    }
    if claude_desktop_config is not None:
        paths[CLAUDE_DESKTOP] = claude_desktop_config
    return paths.get(client_id)


def _project_config_path(client_id: str, project_dir: Path) -> Path | None:
    """Return the project-scope config path for ``client_id`` (``None`` if N/A)."""
    relative_paths: dict[str, Path] = {
        CLAUDE_CODE: Path(".mcp.json"),
        CURSOR: Path(".cursor") / "mcp.json",
        GEMINI_CLI: Path(".gemini") / "settings.json",
        ANTIGRAVITY: Path(".agents") / "mcp_config.json",
        CODEX_CLI: Path(".codex") / "config.toml",
        VS_CODE: Path(".vscode") / "mcp.json",
    }
    relative_path = relative_paths.get(client_id)
    # Claude Desktop, LM Studio, and Windsurf have no documented project scope.
    return None if relative_path is None else project_dir / relative_path


def _client_format(client_id: str) -> str:
    if client_id == VS_CODE:
        return _FMT_JSON_SERVERS
    if client_id == CODEX_CLI:
        return _FMT_TOML
    return _FMT_JSON_MCPSERVERS


def _claude_desktop_config_path() -> Path | None:
    """Return the Claude Desktop config path, or None where it has none.

    Every entry of the detection table is evaluated on each lookup, including
    when the caller is detecting Cursor or Codex, so this call decided the fate
    of the whole function. It raises on any platform outside darwin, win32 and
    linux - freebsd, openbsd, aix, cygwin - and on Windows when APPDATA is
    unset, which is the case for service accounts and minimal containers. The
    error subclasses RuntimeError and no caller catches it, so `datacron setup`
    aborted with a traceback after the reset had already removed the config and
    the index, for a user who never installed Claude Desktop. The VS Code
    sibling handles the identical question with a fallback.
    """
    try:
        return config_path_for_platform()
    except ClaudeDesktopConfigError:
        return None


def _claude_desktop_dirs() -> tuple[Path, ...]:
    """Return the directory to look in, or nothing on a platform without one."""
    config = _claude_desktop_config_path()
    return () if config is None else (config.parent,)


def _is_present(client_id: str) -> bool:
    """Best-effort detection of an installed client via config dir or binary."""
    home = Path.home()
    checks: dict[str, tuple[tuple[Path, ...], tuple[str, ...]]] = {
        CLAUDE_DESKTOP: (_claude_desktop_dirs(), ()),
        CLAUDE_CODE: ((home / ".claude.json", home / ".claude"), ("claude",)),
        CURSOR: ((home / ".cursor",), ("cursor",)),
        GEMINI_CLI: ((home / ".gemini",), ("gemini",)),
        ANTIGRAVITY: ((home / ".gemini" / "antigravity",), ()),
        LMSTUDIO: ((home / _LMSTUDIO_PROFILE_RELATIVE_PATH,), ()),
        CODEX_CLI: ((home / ".codex",), ("codex",)),
        WINDSURF: ((home / ".codeium" / "windsurf",), ("windsurf",)),
        VS_CODE: ((_vscode_user_dir().parent,), ("code",)),
    }
    paths, binaries = checks[client_id]
    if client_id in {ANTIGRAVITY, LMSTUDIO}:
        return any(path.is_dir() for path in paths)
    if any(path.exists() for path in paths):
        return True
    return any(shutil.which(binary) is not None for binary in binaries)


def detect_clients(*, include: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Return installed client identifiers using the shared best-effort detection."""
    candidates = ALL_CLIENT_IDS if include is None else include
    unknown = sorted(set(candidates) - set(ALL_CLIENT_IDS))
    if unknown:
        raise ValueError(f"Unknown client identifiers: {unknown}")
    return tuple(client_id for client_id in candidates if _is_present(client_id))


def client_display_name(client_id: str) -> str:
    """Return the human-readable name for a known client identifier."""
    try:
        return _DISPLAY_NAMES[client_id]
    except KeyError as exc:
        raise ValueError(f"Unknown client identifier: {client_id!r}") from exc


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_targets(
    *,
    scopes: tuple[str, ...],
    project_dir: Path,
    include: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] = (),
) -> list[ClientTarget]:
    """Return install targets for detected clients across the given scopes.

    Args:
        scopes: Which scopes to consider (:data:`SCOPE_USER`, :data:`SCOPE_PROJECT`).
        project_dir: Directory used for project-scope config files.
        include: If set, only these client ids are considered.
        exclude: Client ids to skip.

    Returns:
        One :class:`ClientTarget` per (present client, applicable scope) pair.
    """
    targets: list[ClientTarget] = []
    for client_id in ALL_CLIENT_IDS:
        if include is not None and client_id not in include:
            continue
        if client_id in exclude:
            continue
        if not _is_present(client_id):
            continue
        fmt = _client_format(client_id)
        display = _DISPLAY_NAMES[client_id]
        for scope in scopes:
            path = (
                _user_config_path(client_id)
                if scope == SCOPE_USER
                else _project_config_path(client_id, project_dir)
            )
            if path is None:
                continue
            targets.append(ClientTarget(client_id, display, scope, path, fmt))
    return targets


def discover_unregistration_targets(
    *,
    scopes: tuple[str, ...],
    project_dir: Path | None = None,
    include: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] = (),
) -> list[ClientTarget]:
    """Return existing client configs that may contain a Datacron entry.

    Unlike :func:`discover_targets`, this discovery does not require the client
    application to still be installed. Missing config files are ignored and
    project targets are skipped when no project directory is available.
    """
    targets: list[ClientTarget] = []
    for client_id in ALL_CLIENT_IDS:
        if include is not None and client_id not in include:
            continue
        if client_id in exclude:
            continue
        fmt = _client_format(client_id)
        display = _DISPLAY_NAMES[client_id]
        for scope in scopes:
            if scope == SCOPE_USER:
                path = _user_config_path(client_id)
            elif scope == SCOPE_PROJECT and project_dir is not None:
                path = _project_config_path(client_id, project_dir)
            else:
                continue
            if path is None or not path.exists():
                continue
            targets.append(ClientTarget(client_id, display, scope, path, fmt))
    return targets


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_targets(
    targets: list[ClientTarget],
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> list[InstallOutcome]:
    """Write the Datacron server entry into each target, collecting outcomes.

    A failure on one target is recorded as a failed :class:`InstallOutcome`
    rather than aborting the remaining installs.
    """
    outcomes: list[InstallOutcome] = []
    for target in targets:
        try:
            _install_one(target, command=command, args=args, env=env)
            outcomes.append(
                InstallOutcome(
                    target.client_id,
                    target.display_name,
                    target.scope,
                    target.config_path,
                    installed=True,
                )
            )
            _LOGGER.info("Registered Datacron with %s (%s)", target.display_name, target.scope)
        except (OSError, MCPClientError, ValueError) as exc:
            outcomes.append(
                InstallOutcome(
                    target.client_id,
                    target.display_name,
                    target.scope,
                    target.config_path,
                    installed=False,
                    detail=str(exc),
                )
            )
            _LOGGER.warning("Failed to register with %s: %s", target.display_name, exc)
    return outcomes


def unregister_targets(targets: list[ClientTarget]) -> list[UnregisterOutcome]:
    """Remove Datacron from each target while collecting every outcome."""
    outcomes: list[UnregisterOutcome] = []
    for target in targets:
        try:
            changed = _unregister_one(target)
            detail = "" if changed else "already unregistered"
            outcomes.append(
                UnregisterOutcome(
                    target.client_id,
                    target.display_name,
                    target.scope,
                    target.config_path,
                    successful=True,
                    changed=changed,
                    detail=detail,
                )
            )
            if changed:
                _LOGGER.info(
                    "Unregistered Datacron from %s (%s)", target.display_name, target.scope
                )
            else:
                _LOGGER.info(
                    "Datacron already unregistered from %s (%s)",
                    target.display_name,
                    target.scope,
                )
        except (OSError, MCPClientError, ValueError) as exc:
            outcomes.append(
                UnregisterOutcome(
                    target.client_id,
                    target.display_name,
                    target.scope,
                    target.config_path,
                    successful=False,
                    changed=False,
                    detail=str(exc),
                )
            )
            _LOGGER.warning("Failed to unregister from %s: %s", target.display_name, exc)
    return outcomes


def _install_one(
    target: ClientTarget,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> None:
    if target.fmt == _FMT_JSON_MCPSERVERS:
        _merge_json(target.config_path, "mcpServers", _stdio_entry(command, args, env))
    elif target.fmt == _FMT_JSON_SERVERS:
        _merge_json(target.config_path, "servers", _stdio_entry(command, args, env, with_type=True))
    elif target.fmt == _FMT_TOML:
        _merge_toml(target.config_path, command=command, args=args, env=env)
    else:  # pragma: no cover - guarded by _client_format
        raise MCPClientError(f"Unknown config format: {target.fmt!r}")


def _unregister_one(target: ClientTarget) -> bool:
    if target.fmt == _FMT_JSON_MCPSERVERS:
        return _remove_json_entry(target.config_path, "mcpServers")
    if target.fmt == _FMT_JSON_SERVERS:
        return _remove_json_entry(target.config_path, "servers")
    if target.fmt == _FMT_TOML:
        return _remove_toml_entry(target.config_path)
    raise MCPClientError(f"Unknown config format: {target.fmt!r}")


def _stdio_entry(
    command: str,
    args: list[str],
    env: dict[str, str],
    *,
    with_type: bool = False,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": command, "args": list(args), "env": dict(env)}
    if with_type:
        entry = {"type": "stdio", **entry}
    return entry


def _merged_entry(existing: object, entry: dict[str, Any]) -> dict[str, Any]:
    """Return ``entry`` laid over the Datacron entry the client already holds.

    The entry was replaced wholesale, so re-running setup, which the Windows
    installer does on every upgrade with its write box unchecked by default,
    dropped DATACRON_WRITE_PATHS and turned a writable vault read-only without a
    word, together with any proxy variable, ``disabled`` flag or ``autoApprove``
    list the user had added. What this call supplies still wins; every other key
    and environment variable is kept. The Claude Desktop writer already kept the
    DATACRON_* variables for the same reason.
    """
    if not isinstance(existing, dict):
        return entry
    merged: dict[str, Any] = {**existing, **{k: v for k, v in entry.items() if k != "env"}}
    existing_env = existing.get("env")
    env = dict(existing_env) if isinstance(existing_env, dict) else {}
    env.update(entry.get("env", {}))
    if env:
        merged["env"] = env
    return merged


def _merge_json(path: Path, servers_key: str, entry: dict[str, Any]) -> None:
    config = _load_json(path)
    servers = config.setdefault(servers_key, {})
    if not isinstance(servers, dict):
        raise MCPClientError(
            f"{path}: existing {servers_key!r} is not an object; refusing to edit."
        )
    servers[_SERVER_NAME] = _merged_entry(servers.get(_SERVER_NAME), entry)
    serialized = json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write(path, serialized.encode("utf-8"))


def _merge_toml(path: Path, *, command: str, args: list[str], env: dict[str, str]) -> None:
    config = _load_toml(path)
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise MCPClientError(f"{path}: existing 'mcp_servers' is not a table; refusing to edit.")
    entry: dict[str, Any] = {"command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    servers[_SERVER_NAME] = _merged_entry(servers.get(_SERVER_NAME), entry)
    _atomic_write(path, tomli_w.dumps(config).encode("utf-8"))


def _remove_json_entry(path: Path, servers_key: str) -> bool:
    config = _load_json(path)
    if servers_key not in config:
        return False
    servers = config[servers_key]
    if not isinstance(servers, dict):
        raise MCPClientError(
            f"{path}: existing {servers_key!r} is not an object; refusing to edit."
        )
    if _SERVER_NAME not in servers:
        return False
    del servers[_SERVER_NAME]
    serialized = json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write(path, serialized.encode("utf-8"))
    return True


def _remove_toml_entry(path: Path) -> bool:
    config = _load_toml(path)
    if "mcp_servers" not in config:
        return False
    servers = config["mcp_servers"]
    if not isinstance(servers, dict):
        raise MCPClientError(f"{path}: existing 'mcp_servers' is not a table; refusing to edit.")
    if _SERVER_NAME not in servers:
        return False
    del servers[_SERVER_NAME]
    _atomic_write(path, tomli_w.dumps(config).encode("utf-8"))
    return True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig")  # an editor may save a BOM
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MCPClientError(f"{path} is not valid JSON: {exc}; refusing to overwrite.") from exc
    if not isinstance(loaded, dict):
        raise MCPClientError(f"{path} is not a JSON object (found {type(loaded).__name__}).")
    return loaded


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if not raw.strip():
        return {}
    try:
        return dict(tomllib.loads(raw.decode("utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise MCPClientError(f"{path} is not valid TOML: {exc}; refusing to overwrite.") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace a client config through the shared writer for foreign files.

    It refuses a link, copies the previous content aside before a real change
    only, and leaves an unchanged file alone, so a second identical setup run no
    longer overwrites the backup of a config whose comments the first run dropped.
    """
    replace_foreign_file(path, payload, error=MCPClientError)
