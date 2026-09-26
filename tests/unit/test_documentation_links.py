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
"""Guard the internal links of the published documentation.

Two things went stale without any test noticing: a heading anchor written from
memory (GitHub keeps accented letters in a slug and turns a colon between two
spaces into a double hyphen), and a page added under ``docs/`` that no index ever
listed. Both are checked here, in English and in French, from the files as they
are on disk.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DOCUMENTATION_ROOTS: Final[tuple[Path, ...]] = (
    _REPO_ROOT / "docs",
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "README.fr.md",
)
_LANGUAGE_DIRECTORIES: Final[tuple[Path, ...]] = (
    _REPO_ROOT / "docs" / "en",
    _REPO_ROOT / "docs" / "fr",
)
_INDEX_PAGE: Final[str] = "index.md"

_MARKDOWN_LINK: Final[re.Pattern[str]] = re.compile(r"\]\(([^)\s]+)\)")
_HEADING: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*(```|~~~)")
_INLINE_LINK_TEXT: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# GitHub keeps letters, digits, underscores, spaces and hyphens; everything else is dropped.
_SLUG_DROP: Final[re.Pattern[str]] = re.compile(r"[^\w\- ]", re.UNICODE)


def github_slug(heading: str) -> str:
    """The anchor GitHub derives from one heading, before duplicate numbering."""
    text = _INLINE_LINK_TEXT.sub(r"\1", heading)
    text = text.replace("`", "").replace("*", "")
    return _SLUG_DROP.sub("", text.casefold()).replace(" ", "-")


def heading_anchors(content: str) -> set[str]:
    """Every anchor a page exposes; a repeated heading gets a numbered suffix like GitHub."""
    anchors: set[str] = set()
    seen: Counter[str] = Counter()
    in_fence = False
    for line in content.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match is None:
            continue
        slug = github_slug(match.group(2))
        anchors.add(slug if seen[slug] == 0 else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _markdown_files() -> Iterator[Path]:
    for root in _DOCUMENTATION_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.md"))


_URI_SCHEME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_BLOB_BASE: Final[str] = "https://github.com/VBlackJack/Datacron/blob/main/"


def _internal_links(path: Path) -> Iterator[tuple[int, str, str]]:
    """Yield every internal link, whether or not it carries an anchor.

    Discarding the anchorless ones left 266 published links unchecked, which is
    most of them: a renamed or moved page broke every plain link to it and the
    guard stayed green.
    """
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for target in _MARKDOWN_LINK.findall(line):
            # The READMEs are rendered verbatim as the PyPI project page, which
            # does not rewrite relative targets, so their links are absolute
            # GitHub URLs. They are still repository paths and still checked.
            if target.startswith(_BLOB_BASE):
                page, _, anchor = target[len(_BLOB_BASE) :].partition("#")
                yield number, _from_repo_root(path, page), anchor
                continue
            # Any other URI scheme, not a fixed list: the install instructions
            # carry lmstudio:// deep links, and a new client brings its own.
            if target.startswith("#") or _URI_SCHEME.match(target):
                continue
            page, _, anchor = target.partition("#")
            yield number, page, anchor


def _from_repo_root(path: Path, page: str) -> str:
    """Express a repository-root-relative page as one relative to ``path``."""
    if not page:
        return ""
    import os

    return os.path.relpath(_REPO_ROOT / page, path.parent).replace("\\", "/")


def _dead_anchors(path: Path) -> list[str]:
    findings: list[str] = []
    relative = path.relative_to(_REPO_ROOT).as_posix()
    for number, page, anchor in _internal_links(path):
        target = path if not page else (path.parent / page).resolve()
        if not target.is_file():
            findings.append(f"{relative}:{number}: {page} does not exist")
            continue
        if not anchor:
            continue
        if anchor not in heading_anchors(target.read_text(encoding="utf-8")):
            findings.append(
                f"{relative}:{number}: #{anchor} is not a heading of "
                f"{target.relative_to(_REPO_ROOT).as_posix()}"
            )
    return findings


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Add to LM Studio", "add-to-lm-studio"),
        ("Ajouter Datacron à LM Studio", "ajouter-datacron-à-lm-studio"),
        (
            "Le bloc `tags` : une politique de tags déclarée",
            "le-bloc-tags--une-politique-de-tags-déclarée",
        ),
        ("8. Activer l'écriture (optionnel)", "8-activer-lécriture-optionnel"),
        ("The `tags` block: a declared tag policy", "the-tags-block-a-declared-tag-policy"),
        ("ADR-001 - Source of truth = Markdown vault", "adr-001---source-of-truth--markdown-vault"),
    ],
)
def test_github_slug_matches_published_anchors(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_duplicate_headings_are_numbered_like_github() -> None:
    assert heading_anchors("# Notes\n\n## Notes\n\n## Notes\n") == {"notes", "notes-1", "notes-2"}


def test_fenced_headings_expose_no_anchor() -> None:
    assert heading_anchors("```\n# not a heading\n```\n\n# Title\n") == {"title"}


def test_link_scan_reaches_both_languages_and_the_readmes() -> None:
    scanned = list(_markdown_files())
    assert any(path.parent == _REPO_ROOT / "docs" / "fr" for path in scanned)
    assert any(path.parent == _REPO_ROOT / "docs" / "en" for path in scanned)
    assert _REPO_ROOT / "README.fr.md" in scanned
    links = [link for path in scanned for link in _internal_links(path)]
    assert any(anchor for _number, _page, anchor in links), "no anchor link was scanned"
    assert sum(1 for _number, _page, anchor in links if not anchor) > 100, (
        "the anchorless links are the majority and must be reached"
    )


def test_every_internal_anchor_targets_an_existing_heading() -> None:
    findings = [finding for path in _markdown_files() for finding in _dead_anchors(path)]
    assert not findings, "\n".join(findings)


@pytest.mark.parametrize("directory", _LANGUAGE_DIRECTORIES, ids=lambda path: path.name)
def test_every_documentation_page_is_listed_by_its_index(directory: Path) -> None:
    index = (directory / _INDEX_PAGE).read_text(encoding="utf-8")
    listed = set(_MARKDOWN_LINK.findall(index))
    orphans = sorted(
        page.name
        for page in directory.glob("*.md")
        if page.name != _INDEX_PAGE and page.name not in listed
    )
    assert not orphans, f"docs/{directory.name}/index.md does not list: {orphans}"


_LICENCE_BODY: Final[tuple[str, ...]] = (
    "Copyright 2026 Julien Bombled",
    "",
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "",
    "    http://www.apache.org/licenses/LICENSE-2.0",
    "",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
)
# The comment marker each tracked source type writes its header with.
_COMMENT_MARKERS: Final[dict[str, str]] = {
    ".py": "#",
    ".yml": "#",
    ".yaml": "#",
    ".toml": "#",
    ".sh": "#",
    ".ps1": "#",
    ".bat": "REM",
    ".iss": ";",
}
# A first line that must stay first for the file to work: an interpreter line,
# or the batch echo switch. The header follows it.
_LEADING_DIRECTIVES: Final[tuple[str, ...]] = ("#!", "@echo off")
_GIT_TIMEOUT_SECONDS: Final[int] = 60


def licence_header(marker: str) -> tuple[str, ...]:
    """The whole licence block written with one comment marker."""
    return tuple(f"{marker} {line}" if line else marker for line in _LICENCE_BODY)


def _tracked_source_files() -> list[Path]:
    """Every tracked file whose type carries a header, listed by Git rather than a glob.

    Globbing ``src`` alone let 45 files drift: the workflows, ``pyproject.toml``,
    the scripts and most of the tests had lost the disclaimer, and the empty
    package markers had no header at all. Asking Git keeps an ignored scratch
    file out and a new directory in.
    """
    git = shutil.which("git")
    assert git is not None, "the licence guard needs Git to list the tracked files"
    listing = subprocess.run(
        [git, "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout.decode("utf-8")
    return [
        _REPO_ROOT / name
        for name in sorted(filter(None, listing.split("\0")))
        if Path(name).suffix in _COMMENT_MARKERS and (_REPO_ROOT / name).is_file()
    ]


def licence_header_offenders(paths: list[Path], root: Path) -> list[str]:
    """The files, relative to ``root``, whose opening block is not the whole header."""
    offenders: list[str] = []
    for path in paths:
        expected = licence_header(_COMMENT_MARKERS[path.suffix])
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].startswith(_LEADING_DIRECTIVES):
            lines = lines[1:]
        if tuple(lines[: len(expected)]) != expected:
            offenders.append(path.relative_to(root).as_posix())
    return offenders


def test_every_tracked_source_file_carries_the_whole_licence_header() -> None:
    """Nine files stopped after the licence URL, dropping the disclaimer.

    The header is the one legal statement the project makes about itself, and
    it drifted silently because nothing compared it: three files had lost the
    blank comment lines as well, so the copyright, the grant and the URL had
    been run together. Comparing the whole block rather than looking for one
    phrase is what keeps the next paragraph from going the same way. An empty
    package marker is not exempt: it carries the header like any other file.
    """
    offenders = licence_header_offenders(_tracked_source_files(), _REPO_ROOT)
    assert not offenders, "incomplete or altered licence header:\n" + "\n".join(offenders)


def test_licence_guard_reaches_every_type_and_nested_directories() -> None:
    """The guard is only as good as the files it sees, so its reach is asserted."""
    scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _tracked_source_files()}
    assert {Path(name).suffix for name in scanned} >= {".py", ".yml", ".toml", ".bat", ".iss"}
    for nested in (
        "tests/unit/core/__init__.py",
        ".github/workflows/ci.yml",
        "packaging/windows/datacron-installer.iss",
        "src/datacron/core/config.py",
    ):
        assert nested in scanned, f"the licence guard does not reach {nested}"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("truncated.py", "\n".join(licence_header("#")[:7]) + "\n"),
        ("empty.py", ""),
        ("wrong-marker.bat", "@echo off\n" + "\n".join(licence_header("#")) + "\n"),
        ("stub.yml", "# Copyright 2026 Julien Bombled\n# Licensed under the Apache License.\n"),
    ],
)
def test_licence_guard_refuses_an_incomplete_header(
    tmp_path: Path, name: str, content: str
) -> None:
    probe = tmp_path / name
    probe.write_text(content, encoding="utf-8")
    assert licence_header_offenders([probe], tmp_path) == [name]


@pytest.mark.parametrize(
    ("name", "directive", "marker"),
    [("hook.sh", "#!/usr/bin/env sh", "#"), ("release.bat", "@echo off", "REM")],
)
def test_licence_guard_accepts_the_header_after_a_leading_directive(
    tmp_path: Path, name: str, directive: str, marker: str
) -> None:
    probe = tmp_path / name
    probe.write_text("\n".join((directive, *licence_header(marker), "")), encoding="utf-8")
    assert licence_header_offenders([probe], tmp_path) == []
