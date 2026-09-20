# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Verify a candidate installer on an explicitly disposable Windows machine.

The silent round trip below was the whole of this script. The 2026-09-17 audit
found four defects in the installer script itself, and none of them shows up in
a default install: they need a vault path carrying an ampersand, a trailing
backslash or a double quote, a PATH entry the user wrote by hand, and a
superseded vault whose unregistration fails. Each is a scenario here, run in
its own installation, so the answer is a receipt rather than a recollection.

One step of the plan is not here. Choosing "Keep my current configuration" on
the reinstall page after passing ``/RESETCONFIG`` is a wizard interaction, and
this script only drives silent installs; it is listed in the report as owed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

_DEFAULT_TIMEOUT = 300
_DISPOSABLE_FLAG = "DATACRON_DISPOSABLE_MACHINE"
_STATE_KEY: Final[str] = r"Software\Datacron"
_VAULT_VALUE: Final[str] = "VaultRoot"
_PATH_RECEIPT_VALUE: Final[str] = "PathEntryAdded"
_ENVIRONMENT_KEY: Final[str] = "Environment"
_SILENT: Final[tuple[str, ...]] = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
_POWERSHELL: Final[str] = str(
    Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
_WINDOWS_ONLY: Final[str] = "Installer validation requires Windows"
_MANUAL_STEP: Final[str] = (
    "/RESETCONFIG followed by choosing Keep my current configuration on the reinstall "
    "page: a wizard interaction, not covered by this script"
)
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _CREATION_FLAGS = 0


class ScenarioError(RuntimeError):
    """Raised when a scenario reaches a conclusion the installer must not allow."""


def installation_guard(allow_install: bool) -> None:
    """Refuse a normal workstation and any existing conventional installation."""
    if os.name != "nt" or not allow_install or os.environ.get(_DISPOSABLE_FLAG) != "1":
        raise RuntimeError("Requires Windows, --allow-install and DATACRON_DISPOSABLE_MACHINE=1")
    if (Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Datacron").exists():
        raise RuntimeError("An existing Datacron installation must not be replaced by this test")
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STATE_KEY):
                raise RuntimeError("An existing Datacron installer registry entry was found")
        except FileNotFoundError:
            return
    else:
        raise RuntimeError("Installer validation requires Windows")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_value(key: str, name: str) -> str | None:
    """Return one HKCU string value, or None when the key or value is absent.

    The platform check is what lets this file be type-checked on Linux, where
    ``winreg`` does not exist: the three matrices that run there check every
    line the guard leaves reachable.
    """
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
        except FileNotFoundError:
            return None
        return str(value)
    raise RuntimeError(_WINDOWS_ONLY)


def _set_registry_string(key: str, name: str, value: str, *, expand: bool = False) -> None:
    """Write one HKCU string value into a key that already exists."""
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        kind = winreg.REG_EXPAND_SZ if expand else winreg.REG_SZ
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, kind, value)
        return
    raise RuntimeError(_WINDOWS_ONLY)


def _set_user_path(value: str) -> None:
    _set_registry_string(_ENVIRONMENT_KEY, "Path", value, expand=True)


def _start_menu_shortcut(name: str) -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Datacron"
        / f"{name}.lnk"
    )


def _shortcut_arguments(shortcut: Path, timeout: int) -> str:
    """Read a shortcut's argument string through the shell's own resolver."""
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shell.CreateShortcut('{shortcut}').Arguments"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, path from this process
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        creationflags=_CREATION_FLAGS,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Context:
    """Everything a scenario needs, and nothing that varies between them."""

    installer: Path
    root: Path
    env: dict[str, str]
    timeout: int


def _install(
    context: Context,
    *,
    destination: Path,
    vault_argument: str,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one silent installation, returning its result without raising.

    The command line is built as text rather than as a list because three of
    these scenarios are about how the vault path survives quoting, and a list
    would hand that decision to Python instead of measuring the installer.
    """
    log = destination.with_suffix(".log")
    parts = [
        f'"{context.installer}"',
        *_SILENT,
        f'/LOG="{log}"',
        f'/DIR="{destination}"',
        vault_argument,
        *extra,
    ]
    return subprocess.run(  # noqa: S603 - hash-verified candidate on a disposable host
        " ".join(parts),
        capture_output=True,
        text=True,
        check=False,
        timeout=context.timeout,
        env=context.env,
        creationflags=_CREATION_FLAGS,
    )


def _uninstall(context: Context, destination: Path) -> int:
    uninstaller = destination / "unins000.exe"
    if not uninstaller.is_file():
        return -1
    completed = subprocess.run(  # noqa: S603 - the uninstaller this test just installed
        [str(uninstaller), *_SILENT],
        capture_output=True,
        text=True,
        check=False,
        timeout=context.timeout,
        env=context.env,
        creationflags=_CREATION_FLAGS,
    )
    return completed.returncode


def _footprint(destination: Path) -> dict[str, Any]:
    """Describe what an installation left behind, for evidence and for refusals.

    A refusal that names only "something was installed" cannot be acted on: the
    executable and the registry entry are written at different moments by
    different code, and only one of them says which.
    """
    return {
        "executable": (destination / "datacron.exe").is_file(),
        "recorded_vault": _registry_value(_STATE_KEY, _VAULT_VALUE),
        "path_receipt": _registry_value(_STATE_KEY, _PATH_RECEIPT_VALUE),
    }


def _installed_anything(destination: Path) -> bool:
    footprint = _footprint(destination)
    return bool(footprint["executable"]) or footprint["recorded_vault"] is not None


def _forget_installer_state() -> dict[str, Any]:
    """Drop leftover installer state so each scenario starts from a known machine.

    HKCU is the one thing the scenarios share. A scenario that fails mid-way, or
    an uninstall that does not reach its last step, otherwise decides the verdict
    of every scenario after it. What was there is returned rather than discarded.
    """
    before: dict[str, Any] = {
        "recorded_vault": _registry_value(_STATE_KEY, _VAULT_VALUE),
        "path_receipt": _registry_value(_STATE_KEY, _PATH_RECEIPT_VALUE),
    }
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _STATE_KEY)
        except FileNotFoundError:
            pass
        except OSError:
            before["could_not_be_removed"] = True
    return before


def _expected_shortcut_arguments(destination: Path, subcommand: str, vault: str) -> str:
    executable = destination / "datacron.exe"
    return f'/s /k ""{executable}" {subcommand} --vault "{vault}""'


def _status_through_the_shortcut(context: Context, arguments: str) -> str:
    """Run what the shortcut runs, with /c so it terminates, and return its output."""
    command = "cmd.exe " + arguments.replace("/s /k", "/s /c", 1)
    completed = subprocess.run(  # noqa: S603 - the argument string under test
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=context.timeout,
        env=context.env,
        creationflags=_CREATION_FLAGS,
    )
    return completed.stdout + completed.stderr


def _reported_vault_root(output: str) -> str | None:
    """Return the vault root ``datacron status`` printed, if it printed one."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("vault_root:"):
            return stripped.split(":", 1)[1].strip()
    return None


def scenario_ampersand_vault(context: Context) -> dict[str, Any]:
    """An ampersand in a space-free vault path must not reach cmd unquoted.

    AddQuotes only quotes a path containing a space, so ``C:\\notes&more`` was
    passed bare and cmd ran whatever followed the ampersand as a second command,
    from a shortcut this installer wrote into the Start menu.
    """
    vault = context.root / "notes&more"
    vault.mkdir()
    destination = context.root / "app-ampersand"
    installed = _install(context, destination=destination, vault_argument=f'/VAULT="{vault}"')
    if installed.returncode != 0:
        raise ScenarioError(f"the installation failed with exit code {installed.returncode}")

    evidence: dict[str, Any] = {"vault": str(vault), "shortcuts": {}}
    try:
        for name, subcommand in (("Datacron Status", "status"), ("Datacron Setup", "setup")):
            shortcut = _start_menu_shortcut(name)
            arguments = _shortcut_arguments(shortcut, context.timeout)
            expected = _expected_shortcut_arguments(destination, subcommand, str(vault))
            if arguments != expected:
                raise ScenarioError(f"{name} carries {arguments!r}, expected {expected!r}")
            evidence["shortcuts"][name] = arguments
        output = _status_through_the_shortcut(
            context, _expected_shortcut_arguments(destination, "status", str(vault))
        )
        reached = _reported_vault_root(output)
        evidence["status_reported_vault_root"] = reached
        if reached is None:
            raise ScenarioError(f"the shortcut command line produced no status: {output[:400]!r}")
        # The temporary directory can be handed to the installer in its 8.3 form,
        # and the binary prints the resolved path, so the two spellings differ on
        # everything but the part this measures: whether the name survived the
        # ampersand, or cmd cut the command line there and ran the rest.
        if not reached.casefold().endswith(vault.name.casefold()):
            raise ScenarioError(f"the vault reached the binary as {reached!r}, not {vault.name!r}")
    finally:
        evidence["uninstall_exit_code"] = _uninstall(context, destination)
    return evidence


def scenario_trailing_backslash(context: Context) -> dict[str, Any]:
    """A path ending in a backslash escapes the closing quote of every command."""
    vault = context.root / "trailing"
    vault.mkdir()
    destination = context.root / "app-trailing"
    installed = _install(context, destination=destination, vault_argument=f'/VAULT="{vault}\\"')
    if installed.returncode != 0:
        raise ScenarioError(f"the installation failed with exit code {installed.returncode}")

    evidence: dict[str, Any] = {"vault_as_typed": f"{vault}\\"}
    try:
        recorded = _registry_value(_STATE_KEY, _VAULT_VALUE)
        if recorded != str(vault):
            raise ScenarioError(f"the recorded vault is {recorded!r}, expected {str(vault)!r}")
        evidence["recorded_vault"] = recorded
        for name, subcommand in (("Datacron Status", "status"), ("Datacron Setup", "setup")):
            arguments = _shortcut_arguments(_start_menu_shortcut(name), context.timeout)
            expected = _expected_shortcut_arguments(destination, subcommand, str(vault))
            if arguments != expected:
                raise ScenarioError(f"{name} carries {arguments!r}, expected {expected!r}")
        evidence["shortcuts_normalized"] = True
    finally:
        evidence["uninstall_exit_code"] = _uninstall(context, destination)
    return evidence


def scenario_quote_in_vault_path(context: Context) -> dict[str, Any]:
    """A path carrying a double quote cannot be quoted safely and is refused.

    Delivering one is the hard part, and the reason the refusal exists at all:
    a quote is what the Windows command line uses to delimit, so the three
    spellings below may each be consumed before the installer sees anything. An
    attempt that arrives without its quote proves nothing either way, and says
    so rather than passing; the wizard's directory box remains the way a user
    reaches this, and that stays manual.
    """
    attempts: list[dict[str, Any]] = []
    for index, spelling in enumerate(
        (
            '/VAULT="C:\\data\\qu""ote"',
            '/VAULT=C:\\data\\qu\\"ote',
            '/VAULT="C:\\data\\qu\\"ote"',
        )
    ):
        destination = context.root / f"app-quoted-{index}"
        installed = _install(context, destination=destination, vault_argument=spelling)
        footprint = _footprint(destination)
        recorded = footprint["recorded_vault"]
        delivered = recorded is not None and '"' in recorded
        attempts.append(
            {
                "argument": spelling,
                "exit_code": installed.returncode,
                "footprint": footprint,
                "quote_reached_the_installer": delivered,
            }
        )
        if installed.returncode == 0 or footprint["executable"]:
            _uninstall(context, destination)
        _forget_installer_state()
        if delivered:
            if installed.returncode == 0:
                raise ScenarioError(
                    f"the installer accepted the vault path {recorded!r}: {attempts[-1]}"
                )
            return {"refused": True, "attempts": attempts}
    return {
        "refused": None,
        "inconclusive": True,
        "attempts": attempts,
        "note": "no command line spelling delivered a double quote; this stays manual",
    }


def scenario_user_path_entry_is_preserved(context: Context) -> dict[str, Any]:
    """Uninstall must not delete a PATH entry the user wrote themselves."""
    destination = context.root / "app-path"
    before = _registry_value(_ENVIRONMENT_KEY, "Path") or ""
    hand_written = f"{before};{destination}" if before else str(destination)
    _set_user_path(hand_written)

    vault = context.root / "path-vault"
    vault.mkdir()
    installed = _install(context, destination=destination, vault_argument=f'/VAULT="{vault}"')
    if installed.returncode != 0:
        _set_user_path(before)
        raise ScenarioError(f"the installation failed with exit code {installed.returncode}")

    receipt = _registry_value(_STATE_KEY, _PATH_RECEIPT_VALUE)
    uninstall_code = _uninstall(context, destination)
    after = _registry_value(_ENVIRONMENT_KEY, "Path") or ""
    _set_user_path(before)
    if str(destination).casefold() not in after.casefold():
        raise ScenarioError("uninstalling removed a PATH entry the installer did not add")
    return {
        "receipt_written_by_installer": receipt,
        "uninstall_exit_code": uninstall_code,
        "entry_survived_uninstall": True,
    }


def scenario_superseded_unregistration_warns(context: Context) -> dict[str, Any]:
    """A failed unregistration of a superseded vault is a warning, not exit 20."""
    first_vault = context.root / "first-vault"
    first_vault.mkdir()
    destination = context.root / "app-superseded"
    first = _install(context, destination=destination, vault_argument=f'/VAULT="{first_vault}"')
    if first.returncode != 0:
        raise ScenarioError(f"the first installation failed with exit code {first.returncode}")

    evidence: dict[str, Any] = {}
    try:
        unreachable = "Z:\\datacron-does-not-exist"
        _set_registry_string(_STATE_KEY, _VAULT_VALUE, unreachable)
        evidence["superseded_vault"] = unreachable

        second_vault = context.root / "second-vault"
        second_vault.mkdir()
        second = _install(
            context, destination=destination, vault_argument=f'/VAULT="{second_vault}"'
        )
        evidence["exit_code"] = second.returncode
        log = destination.with_suffix(".log")
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        evidence["unregistration_failed"] = "old-vault unregistration returned exit code" in text
        evidence["reported_as_warning"] = "Datacron post-install warning" in text
        if second.returncode != 0:
            raise ScenarioError(f"a superseded vault left the install at exit {second.returncode}")
        if evidence["unregistration_failed"]:
            if not evidence["reported_as_warning"]:
                raise ScenarioError("the failed unregistration was not reported as a warning")
        else:
            # Pointing the recorded vault at an unreachable drive was meant to
            # make `datacron unregister` exit non-zero. It did not, so the
            # install stayed at exit 0 for the ordinary reason and the warning
            # path was never entered. Passing on that would claim the fix was
            # measured when only its absence was.
            evidence["inconclusive"] = True
            evidence["note"] = "the unregistration succeeded, so the warning path was not exercised"
    finally:
        evidence["uninstall_exit_code"] = _uninstall(context, destination)
    return evidence


def scenario_silent_without_vault(context: Context) -> dict[str, Any]:
    """A silent install with no /VAULT must fail without waiting on a message box."""
    destination = context.root / "app-novault"
    log = destination.with_suffix(".log")
    command = " ".join(
        [f'"{context.installer}"', *_SILENT, f'/LOG="{log}"', f'/DIR="{destination}"']
    )
    completed = subprocess.run(  # noqa: S603 - hash-verified candidate, disposable host
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=context.timeout,
        env=context.env,
        creationflags=_CREATION_FLAGS,
    )
    footprint = _footprint(destination)
    if completed.returncode == 0:
        _uninstall(context, destination)
        raise ScenarioError("a silent install without /VAULT reported success")
    if footprint["executable"]:
        _uninstall(context, destination)
        raise ScenarioError(f"the refusal installed the application: {footprint}")
    return {
        "exit_code": completed.returncode,
        "returned_within_timeout": True,
        "footprint": footprint,
    }


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


def scenario_install_reinstall_and_serve(context: Context) -> dict[str, Any]:
    """The original round trip: install, serve, reinstall, and keep both intact."""
    vault = context.root / "smoke-vault"
    vault.mkdir()
    fixture = vault / "welcome.md"
    fixture.write_text("# InstallationCanary\n\nPreserve these bytes.\n", encoding="utf-8")
    before = digest(fixture)
    destination = context.root / "app-smoke"

    first_run = _install(
        context, destination=destination, vault_argument=f'/VAULT="{vault}"', extra=("/INDEX",)
    )
    if first_run.returncode != 0:
        raise ScenarioError(f"the installation failed with exit code {first_run.returncode}")
    config = vault / ".datacron" / "VAULT.yaml"
    config_before = digest(config)
    executable = destination / "datacron.exe"
    evidence: dict[str, Any] = {}
    try:
        evidence["first_install"] = asyncio.run(
            bounded_smoke(executable, vault, context.env, context.timeout)
        )
        second_run = _install(
            context,
            destination=destination,
            vault_argument=f'/VAULT="{vault}"',
            extra=("/INDEX",),
        )
        if second_run.returncode != 0:
            raise ScenarioError(f"the reinstall failed with exit code {second_run.returncode}")
        evidence["reinstall"] = asyncio.run(
            bounded_smoke(executable, vault, context.env, context.timeout)
        )
        if digest(config) != config_before or digest(fixture) != before:
            raise ScenarioError("Reinstallation changed vault configuration or fixture bytes")
        evidence["config_preserved"] = True
        evidence["note_preserved"] = True
    finally:
        evidence["uninstall_exit_code"] = _uninstall(context, destination)
        # Measured because the first run showed every scenario handing the next
        # one a recorded vault: the uninstaller's own log line says it removes
        # this key, so whether it is still here after a clean uninstall is worth
        # a receipt rather than an assumption.
        evidence["footprint_after_uninstall"] = _footprint(destination)
    return evidence


_SCENARIOS: Final[tuple[tuple[str, Callable[[Context], dict[str, Any]]], ...]] = (
    ("install_reinstall_and_serve", scenario_install_reinstall_and_serve),
    ("ampersand_vault_path", scenario_ampersand_vault),
    ("trailing_backslash_vault_path", scenario_trailing_backslash),
    ("quote_in_vault_path_refused", scenario_quote_in_vault_path),
    ("user_path_entry_preserved", scenario_user_path_entry_is_preserved),
    ("superseded_unregistration_warns", scenario_superseded_unregistration_warns),
    ("silent_without_vault_refused", scenario_silent_without_vault),
)


def _clean_environment() -> dict[str, str]:
    """Serve from a PATH that cannot reach a development Python or this checkout."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATACRON_", "PYTHON")) and key != "VIRTUAL_ENV"
    }
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    return env


def main() -> int:
    """Install only on an opted-in disposable machine; retain evidence for each scenario."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--allow-install", action="store_true")
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only the named scenario; repeat for several.",
    )
    arguments = parser.parse_args()
    if arguments.timeout < 1:
        parser.error("timeout must be positive")
    installation_guard(arguments.allow_install)
    installer = arguments.installer.resolve(strict=True)
    if digest(installer) != arguments.sha256.lower():
        raise RuntimeError("Installer SHA256 mismatch")

    selected = [
        (name, run) for name, run in _SCENARIOS if not arguments.only or name in set(arguments.only)
    ]
    if not selected:
        parser.error("no scenario matches --only")

    results: dict[str, Any] = {}
    failures: list[str] = []
    inconclusive: list[str] = []
    environment = _clean_environment()
    for name, run in selected:
        with tempfile.TemporaryDirectory(prefix=f"datacron-{name}-") as directory:
            context = Context(
                installer=installer,
                root=Path(directory),
                env=environment,
                timeout=arguments.timeout,
            )
            leftover = _forget_installer_state()
            try:
                results[name] = {"passed": True, "evidence": run(context)}
            except (ScenarioError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                results[name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                failures.append(name)
            if any(value is not None and value is not False for value in leftover.values()):
                results[name]["state_left_by_an_earlier_scenario"] = leftover
            evidence = results[name].get("evidence")
            if isinstance(evidence, dict) and evidence.get("inconclusive"):
                inconclusive.append(name)

    report = {
        "installer_sha256": arguments.sha256,
        "runtime_path": environment["PATH"],
        "scenarios": results,
        "failed": failures,
        "inconclusive": inconclusive,
        "not_covered": [_MANUAL_STEP],
        "scope": "disposable_windows_silent_install_not_interactive_gui",
    }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "failed": failures,
                "inconclusive": inconclusive,
                "report": str(arguments.report),
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
