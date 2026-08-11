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
"""Contract tests for the public MCP SDK v2 documentation surfaces."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from datacron.mcp.security_manifest import (
    MCP_TOOL_CAPABILITIES,
    MUTATING_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERIFIED_DATE = "2026-08-11"
FINAL_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"
SPEC_RELEASE_URL = "https://blog.modelcontextprotocol.io/posts/2026-07-28/"
SDK_V2_URL = "https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md"
BL0038_TOOL_NAMES = {
    "delete_note_section",
    "patch_note_preamble",
    "rename_note_section",
}

CURRENT_DOCUMENTATION_PATHS = (
    Path("README.md"),
    Path("README.en.md"),
    Path("docs/fr/architecture.md"),
    Path("docs/en/architecture.md"),
    Path("docs/fr/spec.md"),
    Path("docs/en/spec.md"),
    Path("docs/fr/security-boundary.md"),
    Path("docs/en/security-boundary.md"),
    Path("docs/fr/operational-health.md"),
    Path("docs/en/operational-health.md"),
    Path("docs/fr/user-guide.md"),
    Path("docs/en/user-guide.md"),
    Path("docs/assets/architecture-overview.svg"),
)
VERIFIED_PAGE_PATHS = tuple(
    path
    for path in CURRENT_DOCUMENTATION_PATHS
    if path.suffix == ".md" and not path.name.startswith("README")
)
ARCHITECTURE_PATHS = (
    Path("docs/fr/architecture.md"),
    Path("docs/en/architecture.md"),
)
SPEC_PATHS = (Path("docs/fr/spec.md"), Path("docs/en/spec.md"))
SECURITY_PATHS = (
    Path("docs/fr/security-boundary.md"),
    Path("docs/en/security-boundary.md"),
)
USER_GUIDE_PATHS = (
    Path("docs/fr/user-guide.md"),
    Path("docs/en/user-guide.md"),
)
OPERATIONAL_PATHS = (
    Path("docs/fr/operational-health.md"),
    Path("docs/en/operational-health.md"),
)
SOURCE_DESCRIPTION_PATHS = (
    Path("src/datacron/cli.py"),
    Path("src/datacron/mcp/__init__.py"),
    Path("tests/unit/test_import_purity.py"),
)


def _read(relative_path: Path) -> str:
    """Return one UTF-8 repository file."""
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _collapse_whitespace(content: str) -> str:
    """Collapse prose whitespace for stable sentence-level assertions."""
    return " ".join(content.split())


def _table_tool_names(content: str) -> set[str]:
    """Extract tool identifiers from Markdown table rows."""
    known_tools = set(MCP_TOOL_CAPABILITIES)
    return {
        tool_name
        for line in content.splitlines()
        if line.startswith("|")
        for tool_name in known_tools
        if f"`{tool_name}`" in line
    }


def _list_tool_names(content: str) -> set[str]:
    """Extract tool identifiers from Markdown bullet-list entries."""
    known_tools = set(MCP_TOOL_CAPABILITIES)
    return {
        tool_name
        for line in content.splitlines()
        if line.startswith("- `")
        for tool_name in known_tools
        if f"`{tool_name}`" in line
    }


def _section(content: str, start: str, end: str) -> str:
    """Return content between two exact Markdown headings."""
    start_index = content.index(start)
    end_index = content.index(end, start_index)
    return content[start_index:end_index]


@pytest.mark.parametrize(
    (
        "relative_path",
        "available_heading",
        "guarantees_heading",
        "write_heading",
        "operational_heading",
    ),
    [
        (
            Path("README.md"),
            "Tools d'écriture disponibles :",
            "Garanties :",
            "### Écriture",
            "### Opérationnel",
        ),
        (
            Path("README.en.md"),
            "Available write tools:",
            "Guarantees:",
            "### Writing",
            "### Operational",
        ),
    ],
)
def test_mcp_v2_docs_readme_catalogs_match_runtime_manifest(
    relative_path: Path,
    available_heading: str,
    guarantees_heading: str,
    write_heading: str,
    operational_heading: str,
) -> None:
    """Keep both README mutator lists and the complete tables authoritative."""
    content = _read(relative_path)
    available_tools = _list_tool_names(_section(content, available_heading, guarantees_heading))
    table_tools = _table_tool_names(content)
    table_mutators = _table_tool_names(_section(content, write_heading, operational_heading))

    assert len(MCP_TOOL_CAPABILITIES) == 17
    assert len(MUTATING_TOOL_NAMES) == 8
    assert len(READ_ONLY_TOOL_NAMES) == 9
    assert available_tools == set(MUTATING_TOOL_NAMES)
    assert table_tools == set(MCP_TOOL_CAPABILITIES)
    assert table_mutators == set(MUTATING_TOOL_NAMES)
    assert table_tools.difference(table_mutators) == set(READ_ONLY_TOOL_NAMES)


@pytest.mark.parametrize("relative_path", CURRENT_DOCUMENTATION_PATHS)
def test_mcp_v2_docs_remove_v1_and_release_candidate_language(relative_path: Path) -> None:
    """Keep every current public surface aligned with stable SDK v2 terminology."""
    content = _read(relative_path)

    assert "FastMCP" not in content
    assert "2026-07-28 RC" not in content
    assert "release candidate" not in content.casefold()
    assert "transport en attente" not in content
    assert "SDK final" not in content


@pytest.mark.parametrize("relative_path", VERIFIED_PAGE_PATHS)
def test_mcp_v2_docs_have_current_verification_metadata(relative_path: Path) -> None:
    """Require evidence metadata on every edited standalone documentation page."""
    content = _read(relative_path)

    assert content.startswith("---\n")
    assert f"verified: {VERIFIED_DATE}" in content
    assert "tested_on:" in content


@pytest.mark.parametrize(
    "relative_path",
    [Path("README.md"), Path("README.en.md"), *ARCHITECTURE_PATHS, *SPEC_PATHS],
)
def test_mcp_v2_docs_state_the_measured_stdio_protocol_matrix(relative_path: Path) -> None:
    """Publish both measured protocol modes without implying an HTTP endpoint."""
    content = _read(relative_path)

    assert "MCPServer" in content
    assert "stdio" in content
    assert FINAL_PROTOCOL in content
    assert LEGACY_PROTOCOL in content
    assert "HTTP" in content


@pytest.mark.parametrize("relative_path", [*ARCHITECTURE_PATHS, *SPEC_PATHS])
def test_mcp_v2_docs_publish_structured_error_and_elicitation_boundaries(
    relative_path: Path,
) -> None:
    """Keep wire errors and the legacy-only push back-channel explicit."""
    content = _read(relative_path)

    assert "-32602" in content
    assert "-32603" in content
    assert "isError=true" in content
    assert "back-channel" in content
    assert "elicitation" in content.casefold()
    assert SPEC_RELEASE_URL in content
    assert SDK_V2_URL in content


@pytest.mark.parametrize("relative_path", SECURITY_PATHS)
def test_mcp_v2_docs_security_boundary_names_public_registry_and_error_shim(
    relative_path: Path,
) -> None:
    """Describe the public MCPServer registry and unknown-tool translation."""
    content = _read(relative_path)

    assert "MCPServer" in content
    assert "-32602" in content
    assert "-32603" in content
    assert any(term in content.casefold() for term in ("private", "privé"))


@pytest.mark.parametrize("relative_path", [*ARCHITECTURE_PATHS, *SPEC_PATHS])
def test_mcp_v2_docs_public_catalog_matches_runtime_manifest(relative_path: Path) -> None:
    """Keep every bilingual technical catalog identical to the closed manifest."""
    content = _read(relative_path)
    documented_tools = _table_tool_names(content)
    documented_mutators = {
        tool_name
        for line in content.splitlines()
        if line.startswith(("| Écriture |", "| Write |"))
        for tool_name in MUTATING_TOOL_NAMES
        if f"`{tool_name}`" in line
    }

    assert len(MCP_TOOL_CAPABILITIES) == 17
    assert len(MUTATING_TOOL_NAMES) == 8
    assert len(READ_ONLY_TOOL_NAMES) == 9
    assert documented_tools == set(MCP_TOOL_CAPABILITIES)
    assert documented_mutators == set(MUTATING_TOOL_NAMES)
    assert documented_tools >= BL0038_TOOL_NAMES
    assert "[ ]" not in content
    assert re.search(r"\b(?:14|17) (?:tools|outils)\b", content, re.IGNORECASE) is None


@pytest.mark.parametrize(
    ("relative_path", "write_heading", "supervise_heading"),
    [
        (Path("docs/fr/user-guide.md"), "### Écrire (si activé)", "### Superviser"),
        (Path("docs/en/user-guide.md"), "### Write (if enabled)", "### Supervise"),
    ],
)
def test_mcp_v2_docs_user_catalog_matches_runtime_manifest(
    relative_path: Path,
    write_heading: str,
    supervise_heading: str,
) -> None:
    """Keep user-facing read-only and mutating tool lists complete."""
    content = _read(relative_path)
    documented_tools = _table_tool_names(content)
    documented_mutators = _table_tool_names(_section(content, write_heading, supervise_heading))

    assert documented_tools == set(MCP_TOOL_CAPABILITIES)
    assert documented_mutators == set(MUTATING_TOOL_NAMES)
    assert documented_tools.difference(documented_mutators) == set(READ_ONLY_TOOL_NAMES)
    assert documented_mutators >= BL0038_TOOL_NAMES


@pytest.mark.parametrize("relative_path", OPERATIONAL_PATHS)
def test_mcp_v2_docs_read_only_inventory_matches_runtime_mutators(relative_path: Path) -> None:
    """List every tool omitted by certified read-only mode."""
    content = _read(relative_path)
    documented_mutators = {
        tool_name for tool_name in MUTATING_TOOL_NAMES if f"`{tool_name}`" in content
    }

    assert documented_mutators == set(MUTATING_TOOL_NAMES)


def test_mcp_v2_docs_svg_and_source_descriptions_use_mcpserver() -> None:
    """Keep the maintained diagram and Python docstrings on SDK v2 names."""
    svg = _read(Path("docs/assets/architecture-overview.svg"))
    assert "MCPServer" in svg
    assert "SDK v2" in svg
    assert "Datacron v2.1" not in svg
    assert "Lecture seule en v1" not in svg
    assert "create_note_ai / append_journal" not in svg
    assert "Datacron - Architecture MVP" in svg
    assert "Portabilité : Markdown source lisible" in svg
    assert "Outils d'écriture opt-in : confinement, historique, écriture atomique" in svg
    assert "read-only v1" not in svg
    assert "Clients MCP v1" not in svg

    for relative_path in SOURCE_DESCRIPTION_PATHS:
        content = _read(relative_path)
        assert "FastMCP" not in content


@pytest.mark.parametrize(
    ("relative_path", "stale_adr", "current_footer"),
    [
        (
            Path("docs/fr/architecture.md"),
            "v1 = Claude Desktop + Code uniquement. Cowork via tunnel HTTPS en v1.x.",
            "Document v2.2 synchronisé le 2026-08-11 avec `main`.",
        ),
        (
            Path("docs/en/architecture.md"),
            "v1 = Claude Desktop + Code only. Cowork via HTTPS tunnel in v1.x.",
            "v2.2 document synced on 2026-08-11 with `main`.",
        ),
    ],
)
def test_mcp_v2_docs_architecture_removes_superseded_v1_premise_and_dates_footer(
    relative_path: Path,
    stale_adr: str,
    current_footer: str,
) -> None:
    """Remove the superseded tunnel premise and date the current synchronization."""
    content = _collapse_whitespace(_read(relative_path))

    assert stale_adr not in content
    assert current_footer in content
    assert "synced on 2026-07-12" not in content
    assert "synchronisé le 2026-07-12" not in content


@pytest.mark.parametrize(
    ("relative_path", "validation_contract", "business_contract", "protocol_contract"),
    [
        *[
            (
                relative_path,
                "Les erreurs de validation d'un outil connu restent un résultat d'outil "
                "avec le champ wire `isError=true`.",
                "Seules les exceptions métier Datacron transformées par le wrapper "
                "conservent le payload texte stable "
                '`{"error": {"type": ..., "message": ...}}`.',
                "Les erreurs SDK/protocole restent distinctes.",
            )
            for relative_path in (Path("docs/fr/architecture.md"), Path("docs/fr/spec.md"))
        ],
        *[
            (
                relative_path,
                "Validation errors for a known tool remain a tool result with the wire "
                "field `isError=true`.",
                "Only Datacron business exceptions transformed by the wrapper preserve "
                "the stable text payload "
                '`{"error": {"type": ..., "message": ...}}`.',
                "SDK/protocol errors remain distinct.",
            )
            for relative_path in (Path("docs/en/architecture.md"), Path("docs/en/spec.md"))
        ],
    ],
)
def test_mcp_v2_docs_distinguish_validation_business_and_protocol_errors(
    relative_path: Path,
    validation_contract: str,
    business_contract: str,
    protocol_contract: str,
) -> None:
    """Limit stable Datacron payload claims to wrapped business exceptions."""
    content = _collapse_whitespace(_read(relative_path))

    assert validation_contract in content
    assert business_contract in content
    assert protocol_contract in content
    assert "Une erreur de validation ou d'exécution d'un outil reste" not in content
    assert "A tool validation or execution failure remains" not in content
    assert "Une valeur de tool invalide ou une erreur métier reste" not in content
    assert "An invalid tool value or business error remains" not in content
