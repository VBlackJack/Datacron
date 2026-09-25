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
"""Content-free operator guidance derived from explicit diagnostics."""

from __future__ import annotations

from typing import Any, Final

ERROR_ACTIONS: Final[dict[str, str]] = {
    "duplicate_note_identity": (
        "Inspect the conflicting note IDs; choose their identities explicitly, then retry indexing."
    ),
    "StaleChunkError": (
        "Read the parent note and refresh the index before requesting a new chunk ID."
    ),
    "WriteConflictError": (
        "Read the current target and original request receipt; reprepare only uncommitted work."
    ),
    "recovery_required": (
        "Stop writers and inspect recovery. Preserve the operation journal and pending files."
    ),
    "context_budget_too_small": (
        "Retry with required_tokens or read _memory/INIT.md. Reuse a known "
        "contract hash only when its instructions remain in context."
    ),
    "committed_index_incomplete": (
        "The note is already committed. Repair indexing and reread it; do not "
        "create a new write request."
    ),
    "internal_error": (
        "Use the correlation_id to locate the local diagnostic. Inspect the "
        "original receipt before retrying a write."
    ),
    "follow_up_offset_invalid": (
        "Restart at offset 0, or continue with the next_offset and snapshot_hash "
        "returned by the previous page."
    ),
    "heading_ambiguous": (
        "Several sections match the selector named in the error. Use get_note format=map to "
        "inspect the headings, then pass the level for inter-level matches, or the level, "
        "the occurrence and expected_hash for same-level duplicates."
    ),
    "section_has_subsections": (
        "Nothing was written. Patch one of the listed subsections with its level and "
        "occurrence, or remove it explicitly with delete_note_section first, then patch "
        "the section again with the new expected_hash."
    ),
    "section_structure_changed": (
        "Nothing was written. Close every code fence, HTML comment or raw HTML block the "
        "new content opens, or repair the note's existing structure after reading it with "
        "get_note, then retry with the current expected_hash."
    ),
}


def health_guidance(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Explain measured health failures without inventing repair authority."""
    guidance = []
    if payload["recovery"].get("required"):
        guidance.append(
            {
                "code": "recovery_required",
                "severity": "blocking",
                "action": ERROR_ACTIONS["recovery_required"],
            }
        )
    if not payload["index"]["consistent_with_vault"]:
        guidance.append(
            {
                "code": "index_stale",
                "severity": "warning",
                "action": (
                    "After resolving identity and recovery blockers, reconcile or"
                    " rebuild the index, then verify health."
                ),
            }
        )
    if payload["integrity"].get("id_mismatches"):
        guidance.append(
            {
                "code": "identity_mismatch",
                "severity": "warning",
                "action": (
                    "Run ops inspect-id and review each authority before "
                    "selecting an explicit repair."
                ),
            }
        )
    if not payload["durability"].get("effective_writes_enabled"):
        guidance.append(
            {
                "code": "writes_disabled",
                "severity": "information",
                "action": (
                    "Check read-only mode, configured write paths and durability "
                    "policy before enabling writes."
                ),
            }
        )
    if payload["status"] != "healthy" and not guidance:
        guidance.append(
            {
                "code": "integrity_review",
                "severity": "warning",
                "action": (
                    "Read full health findings and resolve the reported integrity"
                    " or scrubber anomalies before maintenance."
                ),
            }
        )
    return guidance
