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
"""Public MCP tool surface and compatibility re-exports."""

from datacron.mcp.tools.advisory import _contradiction_scan_impl
from datacron.mcp.tools.ops import _audit_query_impl, _get_health_impl, _get_note_history_impl
from datacron.mcp.tools.read import GetNoteFormat, StaleChunkError, _get_note_impl, _list_notes_impl
from datacron.mcp.tools.registry import register_tools
from datacron.mcp.tools.search import (
    _get_backlinks_impl,
    _repair_index_on_read,
    _search_regex_impl,
    _search_text_impl,
)
from datacron.mcp.tools.write import (
    _append_journal_impl,
    _create_note_ai_impl,
    _patch_note_section_impl,
    _revert_note_impl,
    _set_frontmatter_impl,
)

__all__ = [
    "GetNoteFormat",
    "StaleChunkError",
    "_append_journal_impl",
    "_audit_query_impl",
    "_contradiction_scan_impl",
    "_create_note_ai_impl",
    "_get_backlinks_impl",
    "_get_health_impl",
    "_get_note_history_impl",
    "_get_note_impl",
    "_list_notes_impl",
    "_patch_note_section_impl",
    "_repair_index_on_read",
    "_revert_note_impl",
    "_search_regex_impl",
    "_search_text_impl",
    "_set_frontmatter_impl",
    "register_tools",
]
