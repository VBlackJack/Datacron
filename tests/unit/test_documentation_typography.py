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
"""Guard the published documentation against typographic substitutes for ASCII.

Curly quotes, dashes, one-character ellipses, invisible spaces and ligatures say
nothing that a plain ASCII character does not, and they break on a Windows
console, in a diff and in a CI log. Accented letters produced by an AZERTY
keyboard stay welcome, as do arrows, box-drawing characters and emoji.

``_REMEDIES`` maps each refused code point to the ASCII text that replaces it.
The mapping only improves the failure message; it never grants permission.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DOCUMENTATION_ROOTS: Final[tuple[Path, ...]] = (
    _REPO_ROOT / "docs",
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "README.fr.md",
    _REPO_ROOT / "CHANGELOG.md",
)
_REQUIRED_SUBDIRECTORY: Final[Path] = _REPO_ROOT / "docs" / "fr"

_REMEDIES: Final[dict[str, str]] = {
    "\u2014": "-",  # em dash
    "\u2013": "-",  # en dash
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2212": "-",  # minus sign
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
    "\u201a": "'",  # single low-9 quotation mark
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u00ab": '"',  # left-pointing double angle quotation mark
    "\u00bb": '"',  # right-pointing double angle quotation mark
    "\u2026": "...",  # horizontal ellipsis
    "\u00a0": " ",  # no-break space
    "\u202f": " ",  # narrow no-break space
    "\u2009": " ",  # thin space
    "\u200b": "",  # zero width space
    "\ufeff": "",  # byte order mark
    "\u0153": "oe",  # latin small ligature oe
    "\u0152": "OE",  # latin capital ligature oe
    "\u00e6": "ae",  # latin small letter ae
    "\u00c6": "AE",  # latin capital letter ae
}


def _markdown_files() -> Iterator[Path]:
    for root in _DOCUMENTATION_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.md"))


def _violations(path: Path) -> list[str]:
    findings: list[str] = []
    relative = path.relative_to(_REPO_ROOT).as_posix()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for banned, remedy in _REMEDIES.items():
            if banned in line:
                findings.append(f"{relative}:{number}: U+{ord(banned):04X} -> write {remedy!r}")
    return findings


def test_documentation_scan_reaches_nested_directories() -> None:
    scanned = list(_markdown_files())
    assert scanned, "no documentation file was scanned"
    assert any(_REQUIRED_SUBDIRECTORY in path.parents for path in scanned), (
        f"the scan never reached {_REQUIRED_SUBDIRECTORY}"
    )


def test_documentation_uses_ascii_punctuation() -> None:
    findings = [finding for path in _markdown_files() for finding in _violations(path)]
    assert not findings, "\n".join(findings)
