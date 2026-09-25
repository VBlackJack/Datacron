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
"""Write Datacron's MCP server entry into Claude Desktop's config file.

The Claude Desktop configuration lives at:

- macOS:   ``~/Library/Application Support/Claude/claude_desktop_config.json``
- Windows: ``%APPDATA%/Claude/claude_desktop_config.json``
- Linux:   ``~/.config/Claude/claude_desktop_config.json``

:func:`install_claude_desktop_config` reads the file (creates an empty
``{}`` if absent), adds or overwrites the ``mcpServers.datacron`` entry,
preserves every other key, and writes back **atomically** (temp file
then ``os.replace``) so a crash mid-write cannot leave the file in a
partial state.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from datacron.core.logger import get_logger
from datacron.installers.env_merge import ENV_READ_PATHS, ENV_VAULT_ROOT, merge_server_env
from datacron.installers.foreign_files import replace_foreign_file

__all__ = [
    "DATACRON_SERVER_KEY",
    "ClaudeDesktopConfigError",
    "MCPServerInvocation",
    "config_path_for_platform",
    "datacron_env_in_config",
    "install_claude_desktop_config",
    "resolve_mcp_command",
    "resolve_mcp_invocation",
]

_LOGGER = get_logger(__name__)

DATACRON_SERVER_KEY: Final[str] = "datacron"
_MCP_SERVERS_KEY: Final[str] = "mcpServers"
_DATACRON_MCP_COMMAND: Final[str] = "datacron-mcp"


@dataclass(frozen=True)
class MCPServerInvocation:
    """Executable and arguments used to launch Datacron's MCP server."""

    command: str
    args: tuple[str, ...]


def _resolve_mcp_command() -> str:
    """Return an absolute path Claude Desktop can spawn as the MCP server.

    Claude Desktop launches MCP servers as subprocesses without inheriting
    the caller's shell PATH (and certainly not a project venv). A bare
    ``"datacron-mcp"`` command in the config produces a silent
    "executable not found" at startup. Resolution order:

    1. ``shutil.which("datacron-mcp")`` - picks up a system-wide or pipx install.
    2. The same ``Scripts/`` (Windows) or ``bin/`` (POSIX) directory as the
       currently-running interpreter - picks up the venv that invoked this
       installer (typical dev workflow).
    3. Raise :class:`ClaudeDesktopConfigError` with an actionable message.
    """
    on_path = shutil.which(_DATACRON_MCP_COMMAND)
    if on_path:
        return str(Path(on_path).resolve())

    binary = "datacron-mcp.exe" if sys.platform == "win32" else _DATACRON_MCP_COMMAND
    candidate = Path(sys.executable).parent / binary
    if candidate.is_file():
        return str(candidate.resolve())

    raise ClaudeDesktopConfigError(
        f"Cannot locate the {_DATACRON_MCP_COMMAND!r} executable. Looked on PATH "
        f"(shutil.which) and in {candidate.parent!s}. Install Datacron with "
        "`pip install -e .` inside an activated venv, or pass `command` "
        "explicitly with an absolute path."
    )


def resolve_mcp_command() -> str:
    """Return an absolute path to the ``datacron-mcp`` executable.

    Public wrapper around the resolution used for the Claude Desktop config, so
    every MCP client installer embeds the same absolute command (clients do not
    inherit the caller's PATH).

    Raises:
        ClaudeDesktopConfigError: If the executable cannot be located.
    """
    return _resolve_mcp_command()


def resolve_mcp_invocation() -> MCPServerInvocation:
    """Return the MCP launch form for a frozen or Python installation."""
    if getattr(sys, "frozen", False):
        return MCPServerInvocation(
            command=str(Path(sys.executable).resolve()),
            args=("mcp", "serve"),
        )
    return MCPServerInvocation(command=_resolve_mcp_command(), args=())


class ClaudeDesktopConfigError(RuntimeError):
    """Raised when the Claude Desktop configuration cannot be located or written."""


def config_path_for_platform(platform: str | None = None) -> Path:
    """Return the Claude Desktop config path for the given (or current) platform.

    Args:
        platform: An ``sys.platform`` value (``"darwin"``, ``"win32"``,
            or anything starting with ``"linux"``). Defaults to the
            current process's ``sys.platform``.

    Raises:
        ClaudeDesktopConfigError: For unrecognized platforms or missing
            environment variables (``%APPDATA%`` on Windows).
    """
    target = (platform or sys.platform).lower()

    if target == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if target == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ClaudeDesktopConfigError(
                "APPDATA environment variable is not set; "
                "cannot locate Claude Desktop config on Windows."
            )
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if target.startswith("linux"):
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_config) if xdg_config else Path.home() / ".config"
        return base / "Claude" / "claude_desktop_config.json"

    raise ClaudeDesktopConfigError(
        f"Unsupported platform for Claude Desktop install: {target!r}. "
        "Supported: darwin (macOS), win32 (Windows), linux (XDG)."
    )


def install_claude_desktop_config(
    vault_root: Path,
    *,
    config_path: Path | None = None,
    command: str | None = None,
    extra_env: dict[str, str] | None = None,
    removed_env: Iterable[str] = (),
) -> Path:
    """Install Datacron into the Claude Desktop ``claude_desktop_config.json``.

    Args:
        vault_root: Absolute path to the vault Datacron should serve.
            Embedded as ``DATACRON_VAULT_ROOT`` and ``DATACRON_READ_PATHS``
            in the launched subprocess's environment.
        config_path: Override for the config file location (testing).
            Defaults to :func:`config_path_for_platform`.
        command: The executable Claude Desktop will spawn. ``None``
            (default) triggers :func:`resolve_mcp_invocation` to produce the
            command and arguments for the current installation. Pass an explicit
            string to bypass resolution; its arguments default to empty.
        extra_env: Optional additional env vars to merge into the
            subprocess environment.
        removed_env: Env var names the caller explicitly turns off; they are
            dropped from the existing entry instead of being preserved.

    Returns:
        The :class:`Path` of the config file that was written.

    Raises:
        ClaudeDesktopConfigError: If the config cannot be located, parsed,
            or written, or if ``command`` is ``None`` and the
            ``datacron-mcp`` executable cannot be resolved.
    """
    resolved_vault = vault_root.expanduser().resolve()
    target = (config_path or config_path_for_platform()).expanduser()
    invocation = (
        MCPServerInvocation(command=command, args=())
        if command is not None
        else resolve_mcp_invocation()
    )
    _LOGGER.info(
        "Installing Datacron entry into %s (vault=%s, command=%s)",
        target,
        resolved_vault,
        invocation.command,
    )

    config = _load_existing_config(target)
    servers = config.setdefault(_MCP_SERVERS_KEY, {})
    if not isinstance(servers, dict):
        raise ClaudeDesktopConfigError(
            f"Existing {_MCP_SERVERS_KEY!r} entry in {target} is not an object; "
            "refusing to overwrite."
        )

    # Carry forward the DATACRON_* settings already in the entry. Rewriting the
    # env wholesale silently dropped DATACRON_WRITE_PATHS, DATACRON_READ_ONLY and
    # DATACRON_DURABILITY from an existing install, so re-running `mcp install`
    # after an upgrade turned a configured writable vault back into a read-only
    # one, with the next write refused and nothing saying why. Anything this call
    # supplies still wins; only the keys it does not mention are preserved, and
    # removed_env is how an explicit "off" reaches the file.
    supplied = {
        ENV_VAULT_ROOT: str(resolved_vault),
        ENV_READ_PATHS: str(resolved_vault),
        **(extra_env or {}),
    }
    env = merge_server_env(
        _preserved_datacron_env(servers.get(DATACRON_SERVER_KEY)),
        supplied,
        removed=removed_env,
    )

    servers[DATACRON_SERVER_KEY] = {
        "command": invocation.command,
        "args": list(invocation.args),
        "env": env,
    }

    _write_atomically(target, config)
    _LOGGER.info("Wrote %s entries to %s", len(servers), target)
    return target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def datacron_env_in_config(config_path: Path) -> dict[str, str]:
    """Return the DATACRON_* environment of the Datacron entry in ``config_path``.

    Setup reports what it wrote from this, rather than from what it asked for:
    preserved settings are part of what the server will run with.
    """
    config = _load_existing_config(config_path)
    servers = config.get(_MCP_SERVERS_KEY)
    if not isinstance(servers, dict):
        return {}
    return _preserved_datacron_env(servers.get(DATACRON_SERVER_KEY))


def _preserved_datacron_env(existing_entry: object) -> dict[str, str]:
    """Return the DATACRON_* environment an existing entry already declares."""
    if not isinstance(existing_entry, dict):
        return {}
    existing_env = existing_entry.get("env")
    if not isinstance(existing_env, dict):
        return {}
    return {
        key: value
        for key, value in existing_env.items()
        if isinstance(key, str) and isinstance(value, str) and key.startswith("DATACRON_")
    }


def _load_existing_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")  # an editor may save a BOM
    except OSError as exc:
        raise ClaudeDesktopConfigError(f"Failed to read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeDesktopConfigError(
            f"Existing config at {path} is not valid JSON: {exc}. "
            "Refusing to overwrite - please fix or remove the file."
        ) from exc
    if not isinstance(loaded, dict):
        raise ClaudeDesktopConfigError(
            f"Existing config at {path} is not a JSON object (found {type(loaded).__name__})."
        )
    return loaded


def _write_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Replace this client's config durably, keeping a copy of what was there.

    The rename kept the existing config intact if the process died mid-write,
    but neither the file nor its directory was flushed: after a power loss or a
    hard kill the entry can come back as a zero-length file. That is the
    classic rename-without-fsync shape, and the product ships the durable
    writer used here precisely to close it for its own sidecar. The sibling
    installer module takes a copy before replacing a file it does not own; this
    one does the same, for the same reason.
    """
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    replace_foreign_file(path, serialized.encode("utf-8"), error=ClaudeDesktopConfigError)
