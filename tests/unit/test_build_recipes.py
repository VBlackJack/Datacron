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
"""Keep the three PyInstaller recipes saying the same thing.

The bundling flags are spelled in three places: the POSIX build script, the
Windows build script, and the release workflow that produces the binaries users
download. Nothing linked them. A module added to ``--collect-submodules`` in the
scripts an operator runs locally, and forgotten in the workflow, ships a binary
that fails on a machine without the source tree - and it fails at import time, on
the user's machine, after the release is published.

This does not merge the three recipes, which would change how the published
binaries are built. It refuses the drift.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_POSIX_SCRIPT: Final[Path] = _REPO_ROOT / "scripts" / "build_installer.sh"
_WINDOWS_SCRIPT: Final[Path] = _REPO_ROOT / "scripts" / "build_installer.ps1"
_RELEASE_WORKFLOW: Final[Path] = _REPO_ROOT / ".github" / "workflows" / "release.yml"

_BUNDLING_FLAGS: Final[tuple[str, ...]] = (
    "--noconfirm",
    "--onefile",
    "--collect-data",
    "--collect-submodules",
    "--hidden-import",
)
_COLLECTED: Final[re.Pattern[str]] = re.compile(
    r"--(?P<flag>collect-data|collect-submodules|hidden-import)[\s,\"']+(?P<value>[A-Za-z_][\w.]*)"
)


def _recipe(path: Path) -> set[tuple[str, str]]:
    """Return the (flag, module) pairs one recipe declares, however it spells them."""
    text = path.read_text(encoding="utf-8")
    return {(match["flag"], match["value"]) for match in _COLLECTED.finditer(text)}


def test_every_recipe_declares_the_same_bundled_modules() -> None:
    posix = _recipe(_POSIX_SCRIPT)
    windows = _recipe(_WINDOWS_SCRIPT)
    workflow = _recipe(_RELEASE_WORKFLOW)

    assert posix, "the POSIX build script declares no bundled modules"
    assert posix == windows, (
        f"build_installer.sh and build_installer.ps1 disagree: {sorted(posix ^ windows)}"
    )
    assert posix == workflow, (
        "the release workflow and the build scripts disagree, so the published "
        f"binary is not the one the scripts build: {sorted(posix ^ workflow)}"
    )


def test_every_recipe_uses_the_same_bundling_mode() -> None:
    for path in (_POSIX_SCRIPT, _WINDOWS_SCRIPT, _RELEASE_WORKFLOW):
        text = path.read_text(encoding="utf-8")
        missing = [flag for flag in _BUNDLING_FLAGS if flag not in text]
        assert not missing, f"{path.name} is missing {missing}"
