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
"""Typed MCP input and output contracts exposed through ``tools/list``."""

from __future__ import annotations

from typing import Any, Literal, Required, TypeAlias

from typing_extensions import TypedDict

GetNoteFormat: TypeAlias = Literal["full", "map", "chunk"]
MemoryOrigin: TypeAlias = Literal["ai", "human", "merged"]
MemoryConfidence: TypeAlias = Literal["high", "medium", "low", "needs_verification"]
ContradictionScanMode: TypeAlias = Literal["scan", "confirm"]
ContradictionClassName: TypeAlias = Literal[
    "CONTRADICTION",
    "RAFFINEMENT",
    "QUESTION_OUVERTE",
]
ContradictionMutationScope: TypeAlias = Literal["section", "whole_note"]


class ReconcileStatsOutput(TypedDict):
    """Index repair counters returned alongside repaired searches."""

    checked_notes: int
    indexed_notes_before: int
    reindexed_notes: int
    deleted_notes: int
    skipped_notes: int


class NoteSummaryOutput(TypedDict):
    """Stable note metadata returned by ``list_notes``."""

    id: str
    rel_path: str
    title: str
    tags: list[str]
    aliases: list[str]
    frontmatter: dict[str, Any]
    created: str
    updated: str


class ListNotesOutput(TypedDict):
    """Successful ``list_notes`` payload."""

    notes: list[NoteSummaryOutput]
    total: int
    returned: int
    offset: int
    next_offset: int | None
    truncated: bool
    limit_applied: int


class HeadingOutput(TypedDict):
    """One heading in a note map."""

    level: int
    text: str
    path: str
    chunk_id: str


class GetNoteOutput(TypedDict, total=False):
    """Successful ``get_note`` payload across full, map, and chunk formats."""

    format: Required[GetNoteFormat]
    rel_path: Required[str]
    id: str | None
    note_id: str | None
    title: str | None
    tags: list[str] | None
    aliases: list[str] | None
    frontmatter: dict[str, Any] | None
    created: str | None
    updated: str | None
    content_hash: str | None
    note_content_hash: str | None
    chunk_content_hash: str | None
    content_hash_contract: str | None
    content: str | None
    estimated_tokens: int | None
    returned_estimated_tokens: int | None
    offset: int | None
    limit_applied: int | None
    total_chars: int | None
    returned_chars: int | None
    next_offset: int | None
    truncated: bool | None
    headings: list[HeadingOutput] | None
    chunk_count: int | None
    chunk_id: str | None
    header_path: str | None
    line_start: int | None
    line_end: int | None
    prev_chunk_id: str | None
    next_chunk_id: str | None


class SearchResultOutput(TypedDict):
    """One ranked retrieval hit."""

    chunk_id: str
    note_id: str
    note_rel_path: str
    header_path: str
    section_title: str | None
    chunk_type: str
    score: float
    snippet: str
    line_start: int
    line_end: int
    token_count: int


class SearchTextOutput(TypedDict, total=False):
    """Successful ``search_text`` payload."""

    query: Required[str]
    results: Required[list[SearchResultOutput]]
    returned: Required[int]
    limit_applied: Required[int]
    truncated_for_tokens: Required[bool]
    index_repair: ReconcileStatsOutput | None
    timings_ms: dict[str, float] | None


class ContradictionSectionReferenceOutput(TypedDict):
    """One section-level assertion reference."""

    note_id: str
    note_rel_path: str
    header_path: str
    chunk_id: str
    line_start: int
    line_end: int


class ContradictionEvidenceOutput(TypedDict):
    """Bounded excerpts supporting one candidate."""

    target: str
    source: str


class ContradictionMutationSummaryOutput(TypedDict):
    """Read-only summary of one content-addressed mutation proposal."""

    proposal_token: str
    classification: ContradictionClassName
    scope: ContradictionMutationScope
    tool: Literal["patch_note_section", "set_frontmatter"]
    block: str | None


ContradictionCandidateOutput = TypedDict(
    "ContradictionCandidateOutput",
    {
        "candidate_id": str,
        "score": float,
        "class": ContradictionClassName,
        "classification_options": list[ContradictionClassName],
        "rationale": str,
        "evidence": ContradictionEvidenceOutput,
        "target": ContradictionSectionReferenceOutput,
        "source": ContradictionSectionReferenceOutput,
        "addressable": bool,
        "manual_action": str | None,
        "suggested_mutation": ContradictionMutationSummaryOutput | None,
        "alternative_mutations": list[ContradictionMutationSummaryOutput],
    },
)


class ContradictionLimitsOutput(TypedDict):
    """Configured hard scan bounds."""

    max_pairs: int
    max_candidates: int


class PatchSectionCallArgumentsOutput(TypedDict):
    """Exact arguments for the section-level write path."""

    rel_path: str
    heading: str
    new_content: str
    expected_hash: str
    heading_level: int


class InvalidateNoteCallArgumentsOutput(TypedDict):
    """Exact arguments for whole-note temporal invalidation."""

    rel_path: str
    invalid_at: str
    invalidated_by: str
    expected_hash: str


class ContradictionWriteCallOutput(TypedDict):
    """One existing write tool and its complete arguments."""

    tool: Literal["patch_note_section", "set_frontmatter"]
    arguments: PatchSectionCallArgumentsOutput | InvalidateNoteCallArgumentsOutput


class ContradictionWorkflowOutput(TypedDict):
    """Client-owned execution and retrieval verification instructions."""

    execute: str
    verify: str
    verification_tool: Literal["search_text"]
    verification_query: str


class ContradictionConfirmationOutput(TypedDict):
    """Accepted read-only proposal ready for an explicit client write."""

    proposal_token: str
    classification: ContradictionClassName
    scope: ContradictionMutationScope
    write_call: ContradictionWriteCallOutput
    workflow: ContradictionWorkflowOutput


class ContradictionScanOutput(TypedDict, total=False):
    """Successful scan or confirmation response.

    Every non-required key must also accept ``None``: structured-output
    serialization materializes absent optional keys as ``None`` before the
    low-level server validates the result against this schema, so a
    non-nullable optional key can never validate.
    """

    schema_version: Required[int]
    mode: Required[ContradictionScanMode]
    candidates: list[ContradictionCandidateOutput] | None
    candidate_count: int | None
    examined_pairs: int | None
    section_count: int | None
    limits: ContradictionLimitsOutput | None
    deterministic_order: str | None
    index_repair: ReconcileStatsOutput | None
    elicitation_action: Literal["accept", "decline", "cancel"] | None
    confirmation: ContradictionConfirmationOutput | None


class HealthIndexOutput(TypedDict):
    """Index evidence included in operational health."""

    generation: int
    generation_hash: str
    last_reindex: str | None
    notes_count: int
    vault_notes_count: int
    chunks_count: int
    consistent_with_vault: bool
    stale_entries: int
    hash_divergences: int
    staleness_seconds: float | None


class HealthIntegrityOutput(TypedDict):
    """Vault integrity counters included in operational health."""

    notes_count: int
    id_mismatches: int
    broken_wikilinks: int
    mixed_eol_notes: int
    supersedes_cycles: int
    frontmatter_parse_errors: int


class HealthChecksumOutput(TypedDict):
    """Point-in-time vault checksum evidence."""

    algorithm: str
    value: str
    notes_count: int
    scope: str
    claim: str


class InvariantSummaryOutput(TypedDict):
    """Counts by reliability evidence status."""

    proven: int
    baseline_tracked: int
    deferred: int


class HealthInvariantsOutput(TypedDict):
    """Reliability invariant evidence included in operational health."""

    summary: InvariantSummaryOutput
    statuses: dict[str, str]
    scope_notes: dict[str, str]


class GetHealthOutput(TypedDict):
    """Successful ``get_health`` payload."""

    status: Literal["healthy", "degraded", "critical"]
    server_version: str
    read_only: bool
    index: HealthIndexOutput
    integrity: HealthIntegrityOutput
    vault_checksum: HealthChecksumOutput
    durability: dict[str, Any]
    scrubber: dict[str, Any]
    invariants: HealthInvariantsOutput


class CreatedNoteOutput(TypedDict):
    """Identity of a newly created memory note."""

    id: str
    rel_path: str
    title: str


class CreateNoteOutput(TypedDict):
    """Successful ``create_note_ai`` payload."""

    created: CreatedNoteOutput
    content_hash: str
    indexed: bool


class AppendedNoteOutput(TypedDict):
    """Target of a journal append."""

    rel_path: str
    heading: str


class AppendJournalOutput(TypedDict):
    """Successful ``append_journal`` payload."""

    appended: AppendedNoteOutput
    content_hash: str
    indexed: bool


class UpdatedFrontmatterOutput(TypedDict):
    """Frontmatter fields updated on a note."""

    rel_path: str
    fields: list[str]


class SetFrontmatterOutput(TypedDict):
    """Successful ``set_frontmatter`` payload."""

    updated: UpdatedFrontmatterOutput
    content_hash: str
    indexed: bool


class PatchedSectionOutput(TypedDict):
    """Section selected by a successful patch."""

    rel_path: str
    heading: str
    level: int


class PatchNoteSectionOutput(TypedDict):
    """Successful ``patch_note_section`` payload."""

    patched: PatchedSectionOutput
    content_hash: str
    indexed: bool


class RevertedNoteOutput(TypedDict):
    """Identity and history target of a successful revert."""

    id: str
    rel_path: str
    to_hash: str


class RevertNoteOutput(TypedDict):
    """Successful ``revert_note`` payload."""

    reverted: RevertedNoteOutput
    content_hash: str
    indexed: bool
