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
"""Detect installed AI MCP clients and register the Datacron server with them.

Datacron speaks MCP over stdio, so any MCP-capable client can use it once its
configuration references the ``datacron-mcp`` command. Each client stores that
configuration differently (path, file format, top-level key), so this module
models one adapter per client:

- Claude Desktop, Claude Code, Cursor, Gemini CLI, Windsurf — JSON with a
  top-level ``mcpServers`` object.
- VS Code — JSON with a top-level ``servers`` object and an explicit
  ``type: "stdio"`` per server.
- Codex CLI — TOML with ``[mcp_servers.<name>]`` tables.

Every writer reads the existing file (or starts empty), sets only the Datacron
entry, preserves all other content, and writes back atomically.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import tomli_w

from datacron.core.logger import get_logger
from datacron.installers.claude_desktop import config_path_for_platform

__all__ = [
    "ALL_CLIENT_IDS",
    "SCOPE_PROJECT",
    "SCOPE_USER",
    "ClientTarget",
    "InstallOutcome",
    "discover_targets",
    "install_targets",
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
CODEX_CLI: Final[str] = "codex-cli"
WINDSURF: Final[str] = "windsurf"
VS_CODE: Final[str] = "vscode"

ALL_CLIENT_IDS: Final[tuple[str, ...]] = (
    CLAUDE_DESKTOP,
    CLAUDE_CODE,
    CURSOR,
    GEMINI_CLI,
    CODEX_CLI,
    WINDSURF,
    VS_CODE,
)

# Config file formats.
_FMT_JSON_MCPSERVERS: Final[str] = "json-mcpservers"
_FMT_JSON_SERVERS: Final[str] = "json-servers"
_FMT_TOML: Final[str] = "toml"

_DISPLAY_NAMES: Final[dict[str, str]] = {
    CLAUDE_DESKTOP: "Claude Desktop",
    CLAUDE_CODE: "Claude Code",
    CURSOR: "Cursor",
    GEMINI_CLI: "Gemini CLI",
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
    """Return the user-scope config path for ``client_id`` (``None`` if N/A)."""
    home = Path.home()
    paths: dict[str, Path] = {
        CLAUDE_DESKTOP: config_path_for_platform(),
        CLAUDE_CODE: home / ".claude.json",
        CURSOR: home / ".cursor" / "mcp.json",
        GEMINI_CLI: home / ".gemini" / "settings.json",
        CODEX_CLI: home / ".codex" / "config.toml",
        WINDSURF: home / ".codeium" / "windsurf" / "mcp_config.json",
        VS_CODE: _vscode_user_dir() / "mcp.json",
    }
    return paths.get(client_id)


def _project_config_path(client_id: str, project_dir: Path) -> Path | None:
    """Return the project-scope config path for ``client_id`` (``None`` if N/A)."""
    if client_id == CLAUDE_CODE:
        return project_dir / ".mcp.json"
    if client_id == CURSOR:
        return project_dir / ".cursor" / "mcp.json"
    if client_id == GEMINI_CLI:
        return project_dir / ".gemini" / "settings.json"
    if client_id == CODEX_CLI:
        return project_dir / ".codex" / "config.toml"
    if client_id == VS_CODE:
        return project_dir / ".vscode" / "mcp.json"
    # Claude Desktop and Windsurf have no documented project scope.
    return None


def _client_format(client_id: str) -> str:
    if client_id == VS_CODE:
        return _FMT_JSON_SERVERS
    if client_id == CODEX_CLI:
        return _FMT_TOML
    return _FMT_JSON_MCPSERVERS


def _is_present(client_id: str) -> bool:
    """Best-effort detection of an installed client via config dir or binary."""
    home = Path.home()
    checks: dict[str, tuple[tuple[Path, ...], tuple[str, ...]]] = {
        CLAUDE_DESKTOP: ((config_path_for_platform().parent,), ()),
        CLAUDE_CODE: ((home / ".claude.json", home / ".claude"), ("claude",)),
        CURSOR: ((home / ".cursor",), ("cursor",)),
        GEMINI_CLI: ((home / ".gemini",), ("gemini",)),
        CODEX_CLI: ((home / ".codex",), ("codex",)),
        WINDSURF: ((home / ".codeium" / "windsurf",), ("windsurf",)),
        VS_CODE: ((_vscode_user_dir().parent,), ("code",)),
    }
    paths, binaries = checks[client_id]
    if any(path.exists() for path in paths):
        return True
    return any(shutil.which(binary) is not None for binary in binaries)


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


def _merge_json(path: Path, servers_key: str, entry: dict[str, Any]) -> None:
    config = _load_json(path)
    servers = config.setdefault(servers_key, {})
    if not isinstance(servers, dict):
        raise MCPClientError(
            f"{path}: existing {servers_key!r} is not an object; refusing to edit."
        )
    servers[_SERVER_NAME] = entry
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
    servers[_SERVER_NAME] = entry
    _atomic_write(path, tomli_w.dumps(config).encode("utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
