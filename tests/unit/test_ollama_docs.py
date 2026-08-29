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
"""Contract tests for the bilingual Ollama integration guide."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRENCH_GUIDE = REPOSITORY_ROOT / "docs" / "fr" / "ollama.md"
ENGLISH_GUIDE = REPOSITORY_ROOT / "docs" / "en" / "ollama.md"
VERIFIED_DATE = "2026-08-11"
PINNED_MCPO_COMMAND = "uvx --with mcp==1.28.1 mcpo==0.0.20"
OFFICIAL_SOURCE_URLS = (
    "https://docs.ollama.com/api/introduction",
    "https://docs.ollama.com/capabilities/tool-calling",
    "https://github.com/jonigl/mcp-client-for-ollama",
    "https://docs.openwebui.com/features/extensibility/mcp/",
    "https://github.com/open-webui/mcpo",
    "https://github.com/mark3labs/mcphost",
)


def _read(path: Path) -> str:
    """Return UTF-8 documentation content after asserting that it exists."""
    assert path.is_file(), f"missing documentation file: {path.relative_to(REPOSITORY_ROOT)}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("navigation_path", "expected_link"),
    [
        (REPOSITORY_ROOT / "README.md", "docs/fr/ollama.md"),
        (REPOSITORY_ROOT / "README.en.md", "docs/en/ollama.md"),
        (REPOSITORY_ROOT / "docs" / "fr" / "index.md", "ollama.md"),
        (REPOSITORY_ROOT / "docs" / "en" / "index.md", "ollama.md"),
    ],
)
def test_ollama_docs_are_linked_from_navigation(navigation_path: Path, expected_link: str) -> None:
    """Keep the bilingual guide discoverable from every public navigation surface."""
    assert expected_link in _read(navigation_path)


def test_ollama_docs_have_verified_metadata_and_language_links() -> None:
    """Require evidence metadata and an explicit link to the paired translation."""
    french = _read(FRENCH_GUIDE)
    english = _read(ENGLISH_GUIDE)

    for content in (french, english):
        assert content.startswith("---\n")
        assert f"verified: {VERIFIED_DATE}" in content
        assert "tested_on:" in content
        assert "mcpo 0.0.20 / MCP 1.28.1" in content

    assert "../en/ollama.md" in french
    assert "../fr/ollama.md" in english


def test_ollama_docs_distinguish_measured_and_documented_bridges() -> None:
    """Prevent an upstream-only bridge from being presented as locally executed."""
    french = _read(FRENCH_GUIDE)
    english = _read(ENGLISH_GUIDE)

    assert "Ollama n'est pas un hôte MCP" in french
    assert "Ollama is not an MCP host" in english
    assert "ollmcp" in french
    assert "non exécuté" in french
    assert "ollmcp" in english
    assert "not executed" in english
    assert "mcpo" in french
    assert "testé localement" in french
    assert "mcpo" in english
    assert "locally tested" in english
    assert PINNED_MCPO_COMMAND in french
    assert PINNED_MCPO_COMMAND in english
    assert "MCP 2.0.0" in french
    assert "streamablehttp_client" in french
    assert "MCP 2.0.0" in english
    assert "streamablehttp_client" in english
    assert "archivé" in french
    assert "13 avril 2026" in french
    assert "n'est pas recommandé" in french
    assert "archived since April 13, 2026" in english
    assert "not recommended" in english
    assert "--strict-auth" in french
    assert "--strict-auth" in english

    for source_url in OFFICIAL_SOURCE_URLS:
        assert source_url in french
        assert source_url in english


def test_ollama_docs_keep_the_smoke_read_only_and_model_quality_separate() -> None:
    """Keep the proven transport boundary separate from model-quality claims."""
    french = _read(FRENCH_GUIDE)
    english = _read(ENGLISH_GUIDE)

    for content in (french, english):
        assert "DATACRON_VAULT_ROOT" in content
        assert "DATACRON_READ_PATHS" in content
        assert "DATACRON_WRITE_PATHS" in content
        assert "BL-0019" in content
        assert "BL-0107" in content
        assert "qwen3:8b" not in content

    assert "ne garantit pas" in french
    assert "does not guarantee" in english


def test_ollama_docs_local_relative_links_resolve() -> None:
    """Reject broken local Markdown links in the new bilingual pages."""
    link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
    for guide_path in (FRENCH_GUIDE, ENGLISH_GUIDE):
        for target in link_pattern.findall(_read(guide_path)):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (guide_path.parent / target.split("#", maxsplit=1)[0]).resolve()
            assert resolved.exists(), f"broken link in {guide_path.name}: {target}"


def test_ollama_docs_readme_write_tool_count_is_current() -> None:
    """Keep the public write-tool inventory aligned with the measured registry."""
    assert "| Écriture | 8 tools" in _read(REPOSITORY_ROOT / "README.md")
    assert "| Writing | 8 confined" in _read(REPOSITORY_ROOT / "README.en.md")
