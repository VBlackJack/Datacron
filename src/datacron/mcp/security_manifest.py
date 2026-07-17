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

ToolCapabilities = frozenset[str]

MCP_TOOL_CAPABILITIES: Final[MappingProxyType[str, ToolCapabilities]] = MappingProxyType(
    {
        "list_notes": frozenset({"vault_read"}),
        "get_note": frozenset({"vault_read"}),
        "search_text": frozenset({"indexed_vault_read"}),
        "search_regex": frozenset({"indexed_vault_read", "fixed_ripgrep_process"}),
        "get_backlinks": frozenset({"indexed_vault_read"}),
        "contradiction_scan": frozenset({"indexed_vault_read", "vault_read"}),
        "get_health": frozenset({"vault_read", "audit_metadata_read"}),
        "create_note_ai": frozenset({"confined_vault_write"}),
        "append_journal": frozenset({"confined_vault_write"}),
        "set_frontmatter": frozenset({"confined_vault_write"}),
        "patch_note_section": frozenset({"confined_vault_write"}),
        "revert_note": frozenset({"confined_vault_write", "history_restore"}),
        "get_note_history": frozenset({"audit_metadata_read"}),
        "audit_query": frozenset({"audit_metadata_read"}),
    }
)

MUTATING_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "create_note_ai",
        "append_journal",
        "set_frontmatter",
        "patch_note_section",
        "revert_note",
    }
)

READ_ONLY_TOOL_NAMES: Final[frozenset[str]] = frozenset(MCP_TOOL_CAPABILITIES).difference(
    MUTATING_TOOL_NAMES
)

PROHIBITED_TOOL_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"network", "arbitrary_process", "eval", "dynamic_dispatch"}
)

__all__ = [
    "MCP_TOOL_CAPABILITIES",
    "MUTATING_TOOL_NAMES",
    "PROHIBITED_TOOL_CAPABILITIES",
    "READ_ONLY_TOOL_NAMES",
]
