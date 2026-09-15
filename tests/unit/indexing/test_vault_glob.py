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
"""Segment-wise semantics of the vault glob shared by ripgrep and its fallback."""

from __future__ import annotations

import pytest

from datacron.indexing.ripgrep import RegexGlobError, matches_vault_glob


@pytest.mark.parametrize(
    ("rel_path", "glob", "expected"),
    [
        # A multi-segment glob is anchored at the vault root, one segment at a time.
        ("folder/a.md", "folder/*.md", True),
        ("folder/sub/a.md", "folder/*.md", False),
        ("other/a.md", "folder/*.md", False),
        ("a.md", "folder/*.md", False),
        # A complete ** segment crosses zero or more directories.
        ("a.md", "**/*.md", True),
        ("x/y/z/a.md", "**/*.md", True),
        ("x/y/a.txt", "**/*.md", False),
        ("docs/notes/a.md", "docs/**/notes/*.md", True),
        ("docs/a/b/notes/c.md", "docs/**/notes/*.md", True),
        ("docs/a/notes/b/c.md", "docs/**/notes/*.md", False),
        ("docs/a/b", "docs/**", True),
        # A partial ** stays inside its segment.
        ("ax/b.md", "a**/b.md", True),
        ("a/x/b.md", "a**/b.md", False),
        # A leading ./ or / is dropped before matching.
        ("folder/a.md", "./folder/*.md", True),
        ("folder/a.md", "/folder/*.md", True),
        ("sub/folder/a.md", "./folder/*.md", False),
        # A one-segment glob matches the basename anywhere in the vault.
        ("deep/nested/a.md", "*.md", True),
        ("deep/nested/notes", "notes", True),
        ("x/a.md", "a.txt", False),
        # Segments are case-sensitive.
        ("a.md", "*.MD", False),
        ("Folder/a.md", "folder/*.md", False),
    ],
)
def test_matches_vault_glob_segments(rel_path: str, glob: str, expected: bool) -> None:
    assert matches_vault_glob(rel_path, glob) is expected


@pytest.mark.parametrize(
    "glob",
    [
        "",
        "!notes/*.md",
        "{a,b}.md",
        "notes\\a.md",
        "../a.md",
        "a/../b.md",
        "./",
        ".",
        "a//b.md",
        "a/./b.md",
        "notes/",
    ],
)
def test_matches_vault_glob_refuses_invalid_globs(glob: str) -> None:
    with pytest.raises(RegexGlobError) as excinfo:
        matches_vault_glob("notes/a.md", glob)
    assert excinfo.value.code == "regex_glob_invalid"


def test_matches_vault_glob_validates_before_reading_the_path() -> None:
    """The search tool validates the glob with an empty path when the index is empty."""
    assert matches_vault_glob("", "**") is True
    with pytest.raises(RegexGlobError):
        matches_vault_glob("", "{a,b}")
