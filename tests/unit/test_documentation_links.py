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


def _anchor_links(path: Path) -> Iterator[tuple[int, str, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for target in _MARKDOWN_LINK.findall(line):
            if target.startswith(("http://", "https://", "mailto:")) or "#" not in target:
                continue
            page, _, anchor = target.partition("#")
            yield number, page, anchor


def _dead_anchors(path: Path) -> list[str]:
    findings: list[str] = []
    relative = path.relative_to(_REPO_ROOT).as_posix()
    for number, page, anchor in _anchor_links(path):
        target = path if not page else (path.parent / page).resolve()
        if not target.is_file():
            findings.append(f"{relative}:{number}: {page} does not exist")
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
    assert any(list(_anchor_links(path)) for path in scanned), "no anchor link was scanned"


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
