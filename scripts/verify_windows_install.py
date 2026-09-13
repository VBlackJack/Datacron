# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Verify a candidate installer on an explicitly disposable Windows machine."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

_DEFAULT_TIMEOUT = 300
_DISPOSABLE_FLAG = "DATACRON_DISPOSABLE_MACHINE"
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _CREATION_FLAGS = 0


def installation_guard(allow_install: bool) -> None:
    """Refuse a normal workstation and any existing conventional installation."""
    if os.name != "nt" or not allow_install or os.environ.get(_DISPOSABLE_FLAG) != "1":
        raise RuntimeError("Requires Windows, --allow-install and DATACRON_DISPOSABLE_MACHINE=1")
    if (Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Datacron").exists():
        raise RuntimeError("An existing Datacron installation must not be replaced by this test")
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Datacron"):
                raise RuntimeError("An existing Datacron installer registry entry was found")
        except FileNotFoundError:
            return
    else:
        raise RuntimeError("Installer validation requires Windows")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def smoke(executable: Path, vault: Path, env: dict[str, str]) -> dict[str, Any]:
    """Use a fresh MCP connection to the installed binary, without Python on PATH."""
    params = StdioServerParameters(
        command=str(executable), args=["mcp", "serve", "--vault", str(vault)], env=env
    )
    async with Client(stdio_client(params), mode="auto") as session:
        versions = await session.call_tool("get_health", {})
        note = await session.call_tool("get_note", {"id_or_path": "welcome.md"})
        search = await session.call_tool("search_text", {"query": "InstallationCanary"})
        outputs = []
        for response in (versions, note, search):
            if response.is_error or not isinstance(response.content[0], TextContent):
                raise RuntimeError("Installed MCP smoke returned a tool error")
            outputs.append(json.loads(response.content[0].text))
        if "InstallationCanary" not in outputs[1]["content"] or not outputs[2]["returned"]:
            raise RuntimeError("Installed MCP could not retrieve the fixture")
        if not outputs[0]["index"]["consistent_with_vault"]:
            raise RuntimeError("Installed index is not consistent with fixture bytes")
        return {
            "server_version": outputs[0]["server_version"],
            "mcp_read": True,
            "mcp_search": True,
        }


async def bounded_smoke(
    executable: Path, vault: Path, env: dict[str, str], seconds: int
) -> dict[str, Any]:
    async with asyncio.timeout(seconds):
        return await smoke(executable, vault, env)


def main() -> None:
    """Install and reinstall only on an opted-in disposable machine; retain evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--allow-install", action="store_true")
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    installation_guard(args.allow_install)
    installer = args.installer.resolve(strict=True)
    if digest(installer) != args.sha256.lower():
        raise RuntimeError("Installer SHA256 mismatch")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATACRON_", "PYTHON")) and k != "VIRTUAL_ENV"
    }
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    with tempfile.TemporaryDirectory(prefix="datacron-install-test-") as directory:
        root = Path(directory)
        vault = root / "vault"
        vault.mkdir()
        fixture = vault / "welcome.md"
        fixture.write_text("# InstallationCanary\n\nPreserve these bytes.\n", encoding="utf-8")
        before = digest(fixture)
        destination = root / "application"
        command = [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/INDEX",
            f"/DIR={destination}",
            f"/VAULT={vault}",
        ]
        subprocess.run(  # noqa: S603 - hash-verified candidate on an opted-in disposable host
            command,
            check=True,
            timeout=args.timeout,
            env=env,
            creationflags=_CREATION_FLAGS,
        )
        config = vault / ".datacron" / "VAULT.yaml"
        config_before = digest(config)
        executable = destination / "datacron.exe"
        first = asyncio.run(bounded_smoke(executable, vault, env, args.timeout))
        subprocess.run(  # noqa: S603 - hash-verified candidate on an opted-in disposable host
            command,
            check=True,
            timeout=args.timeout,
            env=env,
            creationflags=_CREATION_FLAGS,
        )
        second = asyncio.run(bounded_smoke(executable, vault, env, args.timeout))
        if digest(config) != config_before or digest(fixture) != before:
            raise RuntimeError("Reinstallation changed vault configuration or fixture bytes")
        report = {
            "installer_sha256": args.sha256,
            "runtime_path": env["PATH"],
            "first_install": first,
            "reinstall": second,
            "config_preserved": True,
            "note_preserved": True,
            "scope": "disposable_windows_silent_install_not_interactive_gui",
        }
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        subprocess.run(  # noqa: S603 - hash-verified candidate on an opted-in disposable host
            [str(destination / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            check=True,
            timeout=args.timeout,
            env=env,
            creationflags=_CREATION_FLAGS,
        )


if __name__ == "__main__":
    main()
