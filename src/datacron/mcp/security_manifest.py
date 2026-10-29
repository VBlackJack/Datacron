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
"""Closed, reviewable manifest of server-side MCP tool capabilities."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from datacron.core.config import DEFAULT_TOOL_PROFILE, TOOL_PROFILE_ESSENTIAL

ToolCapabilities = frozenset[str]

MCP_TOOL_CAPABILITIES: Final[MappingProxyType[str, ToolCapabilities]] = MappingProxyType(
    {
        "list_notes": frozenset({"vault_read"}),
        "get_note": frozenset({"vault_read"}),
        "session_context": frozenset({"vault_read", "indexed_vault_read"}),
        "prepare_follow_up": frozenset({"vault_read"}),
        "get_follow_up": frozenset({"vault_read"}),
        "search_text": frozenset({"indexed_vault_read"}),
        "search_regex": frozenset({"indexed_vault_read", "fixed_ripgrep_process"}),
        "get_backlinks": frozenset({"indexed_vault_read"}),
        "contradiction_scan": frozenset({"indexed_vault_read", "vault_read"}),
        "get_health": frozenset({"vault_read", "audit_metadata_read"}),
        "create_note_ai": frozenset({"confined_vault_write"}),
        "append_journal": frozenset({"confined_vault_write"}),
        "set_frontmatter": frozenset({"confined_vault_write"}),
        "patch_note_preamble": frozenset({"confined_vault_write"}),
        "patch_note_section": frozenset({"confined_vault_write"}),
        "delete_note_section": frozenset({"confined_vault_write"}),
        "rename_note_section": frozenset({"confined_vault_write"}),
        "move_note_section": frozenset({"confined_vault_write", "vault_read"}),
        "revert_note": frozenset({"confined_vault_write", "history_restore"}),
        "apply_organization_manifest": frozenset(
            {"confined_vault_write", "organization_batch_apply"}
        ),
        "get_note_history": frozenset({"audit_metadata_read"}),
        "get_write_progress": frozenset(
            {"vault_read", "indexed_vault_read", "audit_metadata_read"}
        ),
        "audit_query": frozenset({"audit_metadata_read"}),
        # Essential-profile verbs: composed from the writers and readers above and
        # registered only under DATACRON_TOOL_PROFILE=essential.
        "remember": frozenset({"confined_vault_write", "indexed_vault_read", "vault_read"}),
        "recall": frozenset({"indexed_vault_read", "vault_read"}),
        "what_changed": frozenset({"audit_metadata_read", "indexed_vault_read"}),
        # Registered only by the standard profile, and only when VAULT.yaml declares
        # runbooks and DATACRON_RUNBOOK_VAULT is set; it writes nowhere else.
        "runbook_capture": frozenset({"confined_vault_write", "vault_read"}),
    }
)

ESSENTIAL_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"remember", "recall", "what_changed", "get_note", "get_health"}
)

# Tools a configuration adds to the standard profile; never registered by default.
RUNBOOK_TOOL_NAMES: Final[frozenset[str]] = frozenset({"runbook_capture"})

STANDARD_TOOL_NAMES: Final[frozenset[str]] = frozenset(MCP_TOOL_CAPABILITIES).difference(
    {"remember", "recall", "what_changed"} | RUNBOOK_TOOL_NAMES
)

TOOL_NAMES_BY_PROFILE: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        DEFAULT_TOOL_PROFILE: STANDARD_TOOL_NAMES,
        TOOL_PROFILE_ESSENTIAL: ESSENTIAL_TOOL_NAMES,
    }
)

MUTATING_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "create_note_ai",
        "append_journal",
        "set_frontmatter",
        "patch_note_preamble",
        "patch_note_section",
        "delete_note_section",
        "rename_note_section",
        "move_note_section",
        "revert_note",
        "apply_organization_manifest",
        "remember",
        "runbook_capture",
    }
)

READ_ONLY_TOOL_NAMES: Final[frozenset[str]] = frozenset(MCP_TOOL_CAPABILITIES).difference(
    MUTATING_TOOL_NAMES
)

# Per-profile views of the two sets above: the names a given profile registers.
STANDARD_MUTATING_TOOL_NAMES: Final[frozenset[str]] = MUTATING_TOOL_NAMES & STANDARD_TOOL_NAMES
STANDARD_READ_ONLY_TOOL_NAMES: Final[frozenset[str]] = READ_ONLY_TOOL_NAMES & STANDARD_TOOL_NAMES
ESSENTIAL_MUTATING_TOOL_NAMES: Final[frozenset[str]] = MUTATING_TOOL_NAMES & ESSENTIAL_TOOL_NAMES
ESSENTIAL_READ_ONLY_TOOL_NAMES: Final[frozenset[str]] = READ_ONLY_TOOL_NAMES & ESSENTIAL_TOOL_NAMES

PROHIBITED_TOOL_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"network", "arbitrary_process", "eval", "dynamic_dispatch"}
)

__all__ = [
    "ESSENTIAL_MUTATING_TOOL_NAMES",
    "ESSENTIAL_READ_ONLY_TOOL_NAMES",
    "ESSENTIAL_TOOL_NAMES",
    "MCP_TOOL_CAPABILITIES",
    "MUTATING_TOOL_NAMES",
    "PROHIBITED_TOOL_CAPABILITIES",
    "READ_ONLY_TOOL_NAMES",
    "RUNBOOK_TOOL_NAMES",
    "STANDARD_MUTATING_TOOL_NAMES",
    "STANDARD_READ_ONLY_TOOL_NAMES",
    "STANDARD_TOOL_NAMES",
    "TOOL_NAMES_BY_PROFILE",
]
