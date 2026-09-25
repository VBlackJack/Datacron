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

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_REQUIRED_SUBDIRECTORY: Final[Path] = _REPO_ROOT / "docs" / "fr"
_BINARY_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".db",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".png",
        ".pyc",
        ".woff",
        ".woff2",
        ".zip",
    }
)
_REQUIRED_NON_DOCS_FILES: Final[tuple[Path, ...]] = (
    _REPO_ROOT / "packaging" / "windows" / "datacron-installer.iss",
    _REPO_ROOT / "pyproject.toml",
    _REPO_ROOT / "docs" / "assets" / "architecture-overview.svg",
)
"""Files outside the Markdown pages whose text reaches a user, pinned so the sweep keeps them.

The guard used to scan two hand-listed roots, which covered 137 of the repository's
299 tracked files. Everything a user reads outside ``docs/`` was outside it: the
installer's own UI strings, the PyPI long description, the MCP registry entry. A sweep
a curly quote can walk around is the failure this rule exists to stop, so the scan is
the tracked tree now and these files are asserted reachable. The SVG diagram is text
whose labels render on the README, so it is scanned like any other text file.
"""

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
    "\u2007": " ",  # figure space
    "\u200b": "",  # zero width space
    "\u200c": "",  # zero width non-joiner
    "\u200d": "",  # zero width joiner
    "\u2060": "",  # word joiner
    "\ufeff": "",  # byte order mark
    "\u0153": "oe",  # latin small ligature oe
    "\u0152": "OE",  # latin capital ligature oe
    "\u00e6": "ae",  # latin small letter ae
    "\u00c6": "AE",  # latin capital letter ae
}


def _tracked_text_files() -> Iterator[Path]:
    """Every tracked file the typography rule governs, decoded as text.

    Driven from ``git ls-files`` rather than a list of roots, because a list of roots
    is exactly what let the installer UI and the packaging metadata drift out of reach.
    A file that is not valid UTF-8 is not text and is skipped; so is a known binary
    suffix, which saves reading it at all.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        path = _REPO_ROOT / entry
        if path.suffix.casefold() in _BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield path


def _markdown_files() -> Iterator[Path]:
    yield from (path for path in _tracked_text_files() if path.suffix.casefold() == ".md")


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


def test_the_scan_reaches_user_facing_text_outside_the_docs_tree() -> None:
    """A sweep is only worth its result if it reaches what it claims to cover.

    Two hand-listed roots covered 137 of 299 tracked files, and the text a user reads
    in the installer and on PyPI sat outside them for as long as nobody looked.
    """
    scanned = set(_tracked_text_files())
    missing = [path for path in _REQUIRED_NON_DOCS_FILES if path not in scanned]
    assert not missing, f"the sweep does not reach {[str(path) for path in missing]}"


def test_documentation_uses_ascii_punctuation() -> None:
    findings = [finding for path in _tracked_text_files() for finding in _violations(path)]
    assert not findings, "\n".join(findings)


# Removing a banned character can damage a sentence, and a scan for banned code points is
# blind to exactly that: an em dash rewritten as a bare hyphen glues a clause to the word
# before it. The conjunction must keep its space, so a hyphen may not sit against one.
#
# This rule is deliberately narrow. Widening it to every clause opener flags "waiting-for
# replies", "opt-in", every French imperative with an enclitic pronoun ("installe-le") and
# every option name inside a code span ("`-wal`"), so it would report far more noise than
# damage. It catches the common shape; the rest of the residue was swept by hand.
_GLUED_CLAUSE: Final[re.Pattern[str]] = re.compile(
    r"[^\W\d_]-(?:or|and|as|but|so|then|ou|et|mais|donc)\s"
)


def _language_pairs() -> list[tuple[Path, Path]]:
    english = _REPO_ROOT / "docs" / "en"
    french = _REPO_ROOT / "docs" / "fr"
    return [(page, french / page.name) for page in sorted(english.glob("*.md"))]


def test_documentation_keeps_clauses_apart_from_their_conjunction() -> None:
    findings: list[str] = []
    for path in _markdown_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            findings.extend(
                f"{relative}:{number}: {match.group(0)!r} -> write ' - {match.group(0)[2:]}'"
                for match in _GLUED_CLAUSE.finditer(line)
            )
    assert not findings, "\n".join(findings)


# The translation link belongs at the top of a page, under its title: a reader who
# landed in the wrong language decides in the first screen, not after the last section.
_TRANSLATION_LINK_WINDOW: Final[int] = 10


def _links_near_top(page: Path, target: str) -> bool:
    head = page.read_text(encoding="utf-8").splitlines()[:_TRANSLATION_LINK_WINDOW]
    return any(target in line for line in head)


def test_every_public_page_links_to_its_translation() -> None:
    pairs = _language_pairs()
    assert pairs, "no English documentation page was found"
    findings: list[str] = []
    for english, french in pairs:
        if not french.is_file():
            findings.append(f"docs/fr/{english.name} is missing")
            continue
        if not _links_near_top(english, f"(../fr/{english.name})"):
            findings.append(
                f"docs/en/{english.name} does not link to its translation "
                f"within its first {_TRANSLATION_LINK_WINDOW} lines"
            )
        if not _links_near_top(french, f"(../en/{english.name})"):
            findings.append(
                f"docs/fr/{french.name} does not link to its translation "
                f"within its first {_TRANSLATION_LINK_WINDOW} lines"
            )
    orphans = sorted(
        page.name
        for page in (_REPO_ROOT / "docs" / "fr").glob("*.md")
        if not (_REPO_ROOT / "docs" / "en" / page.name).is_file()
    )
    findings.extend(f"docs/fr/{name} has no English counterpart" for name in orphans)
    assert not findings, "\n".join(findings)


# The same substitutes are refused in the source tree: a curly quote or an em dash in
# a Python string is a user-visible default or a message that will reach a console.
_SOURCE_ROOT: Final[Path] = _REPO_ROOT / "src"


def _source_files() -> Iterator[Path]:
    yield from sorted(_SOURCE_ROOT.rglob("*.py"))


def test_source_scan_reaches_the_package() -> None:
    scanned = list(_source_files())
    assert scanned, "no source file was scanned"
    assert any(path.parent.name == "core" for path in scanned), "the scan never reached core"


def test_source_code_uses_ascii_punctuation() -> None:
    findings = [finding for path in _source_files() for finding in _violations(path)]
    assert not findings, "\n".join(findings)


# Release notes follow a stricter rule than the rest of the documentation. They are
# read on a release page and by an update client that never chose a language or a
# font, so only ASCII and the accented letters an AZERTY keyboard produces are
# accepted there: the arrows, box-drawing characters and emoji welcome in a guide are
# refused in a note. The allowlist grants; nothing else does.
_FRENCH_ACCENTED_LETTERS: Final[frozenset[str]] = frozenset("àâäçéèêëîïôöùûüÿÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ")
_ASCII_LIMIT: Final[int] = 0x7F
_RELEASE_NOTES_PATTERN: Final[str] = "*release-notes*.md"
_CHANGELOG: Final[Path] = _REPO_ROOT / "CHANGELOG.md"


def _release_note_files() -> list[Path]:
    notes = [_CHANGELOG]
    for language in ("en", "fr"):
        notes.extend(sorted((_REPO_ROOT / "docs" / language).glob(_RELEASE_NOTES_PATTERN)))
    return notes


def release_note_violations(path: Path, root: Path) -> list[str]:
    """Every character of ``path`` outside ASCII and the French accented letters."""
    findings: list[str] = []
    relative = path.relative_to(root).as_posix()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        findings.extend(
            f"{relative}:{number}: U+{ord(character):04X} is not allowed in release notes"
            for character in line
            if ord(character) > _ASCII_LIMIT and character not in _FRENCH_ACCENTED_LETTERS
        )
    return findings


def test_release_notes_scan_reaches_the_changelog_and_the_notes() -> None:
    scanned = _release_note_files()
    assert _CHANGELOG in scanned
    for language in ("en", "fr"):
        directory = _REPO_ROOT / "docs" / language
        assert any(path.parent == directory for path in scanned), (
            f"the release-notes guard never reached docs/{language}"
        )


def test_release_notes_are_ascii_plus_french_accents() -> None:
    findings = [
        finding
        for path in _release_note_files()
        for finding in release_note_violations(path, _REPO_ROOT)
    ]
    assert not findings, "\n".join(findings)


def test_release_notes_guard_refuses_arrows_and_keeps_accents(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("Corrigé : état \u2192 prêt \u2705\n", encoding="utf-8")
    assert release_note_violations(note, tmp_path) == [
        "note.md:1: U+2192 is not allowed in release notes",
        "note.md:1: U+2705 is not allowed in release notes",
    ]
