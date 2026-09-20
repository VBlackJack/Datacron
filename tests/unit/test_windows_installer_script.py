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
"""Static guards on the Windows installer script.

Compiling the ``.iss`` needs Inno Setup and exercising it needs a disposable
Windows machine, so none of it runs in this suite. These are the properties that
can be read off the source, and they are the ones whose absence caused the
defects the 2026-09-17 audit found: a command line built without quotes, a PATH
entry removed without a receipt, a switch that answered a question the operator
was still being asked.

They are not a substitute for an install. ``scripts/verify_windows_install.py``
is that, on a machine that opted in.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_INSTALLER: Final[Path] = _REPO_ROOT / "packaging" / "windows" / "datacron-installer.iss"


def _source() -> str:
    return _INSTALLER.read_text(encoding="utf-8")


def test_shortcut_command_lines_quote_unconditionally() -> None:
    """An ampersand in a space-free vault path must not reach cmd unquoted.

    `AddQuotes` only quotes a path containing a space, so `C:\\notes&calc` was
    passed bare: cmd splits on the ampersand and runs what follows as a second
    command, from a shortcut the installer wrote into the Start menu.
    """
    body = _source()
    start = body.index("function ShortcutParameters")
    end = body.index("end;", start)
    builder = body[start:end]

    assert "AddQuotes(" not in builder, "the shortcut builder must not rely on the space heuristic"
    assert "'\"' + ExpandConstant" in builder, "the executable is quoted explicitly"
    assert "--vault \"' + EffectiveVaultPath + '\"" in builder, (
        "the vault path is quoted explicitly"
    )
    assert "/s /k" in builder, "cmd needs /s to strip exactly the outer quote pair"


def test_the_vault_path_is_normalized_once_before_any_command_line() -> None:
    """A trailing backslash escapes the closing quote of every command built from it."""
    body = _source()

    assert "VaultPath := NormalizePathEntry(SelectedVaultPath);" in body
    assert "VaultQuoteRejected" in body, "a path carrying a quote cannot be quoted and is refused"
    assert body.count("VaultPath := SelectedVaultPath;") == 0


def test_the_user_path_entry_is_removed_only_against_a_receipt() -> None:
    """Uninstall must not delete a PATH entry the user wrote themselves."""
    body = _source()
    start = body.index("procedure RemoveAppFromUserPath")
    end = body.index("end;", body.index("Removed Datacron application directory", start))
    remover = body[start:end]

    assert "'PathEntryAdded'" in remover, "removal must consult the receipt"
    assert remover.index("'PathEntryAdded'") < remover.index("PathWithoutEntry"), (
        "the receipt is checked before the PATH is touched"
    )
    adder = body[body.index("function AddAppToUserPath") : start]
    assert "RegWriteStringValue(HKCU, InstallerStateKey, 'PathEntryAdded', AppPath)" in adder


def test_resetconfig_seeds_the_wizard_rather_than_overriding_it() -> None:
    """The switch must not destroy a config the operator just chose to keep."""
    body = _source()
    start = body.index("function ResetConfigurationRequested")
    end = body.index("end;", start)
    decision = body[start:end]

    assert "WizardSilent" in decision
    assert re.search(r"if WizardSilent then\s+Result := CommandLineSwitchPresent", decision), (
        "outside silent mode the page decides, not the switch"
    )
    assert "ResetSwitchDefault" in body, "the switch seeds the page's initial selection"


def test_a_stale_unregister_is_a_warning_and_not_a_failed_install() -> None:
    """Exit 20 must mean the product is unconfigured, not that a cleanup step failed."""
    body = _source()
    assert "MarkSetupWarning(CustomMessage('UnregisterFailed'))" in body
    assert "MarkSetupFailure(CustomMessage('UnregisterFailed'))" not in body
    assert "procedure MarkSetupWarning" in body
    warning = body[
        body.index("procedure MarkSetupWarning") : body.index("procedure MarkSetupFailure")
    ]
    assert "SetupFailed := True" not in warning, "a warning must not set the failure flag"
    assert "CustomMessage('SetupFailed')" not in warning
