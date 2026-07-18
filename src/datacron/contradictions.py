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
"""Deterministic live contradiction candidates and read-only proposals."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal

from datacron.core import config as core_config
from datacron.core.markdown_sections import find_section_span, parse_heading_line
from datacron.core.models import Chunk, ChunkType, Note

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = [
    "CONTRADICTION_SCHEMA_VERSION",
    "CandidateClass",
    "MutationScope",
    "build_proposal",
    "build_scan_report",
    "confirm_proposal",
    "format_update_block",
]

CONTRADICTION_SCHEMA_VERSION: Final[int] = 2
_PROPOSAL_PREFIX: Final[str] = "cs2"
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{_PROPOSAL_PREFIX}:(\d{{4}}-\d{{2}}-\d{{2}}):([0-9a-f]{{64}})$"
)
_ISO_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_UPDATE_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^>\s*(?:"
    + "|".join(
        re.escape(label) for label in core_config.DEFAULT_CONTRADICTION_PROVENANCE_LABELS.values()
    )
    + r")\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*:",
    flags=re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)"
)
_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_SEARCH_NEIGHBOR_LIMIT: Final[int] = 8
_QUERY_TERM_LIMIT: Final[int] = 6
_EVIDENCE_CHAR_LIMIT: Final[int] = 280
_STATEMENT_CHAR_LIMIT: Final[int] = 240
_MIN_LEXICAL_SCORE: Final[float] = 0.25
_MIN_MARKED_SCORE: Final[float] = 0.15
_HEADING_SEPARATOR: Final[str] = " / "

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "about",
        "after",
        "avec",
        "avant",
        "cette",
        "comme",
        "dans",
        "depuis",
        "des",
        "elle",
        "elles",
        "entre",
        "est",
        "for",
        "from",
        "have",
        "into",
        "mais",
        "notre",
        "nous",
        "pour",
        "plus",
        "que",
        "qui",
        "sans",
        "sur",
        "the",
        "their",
        "this",
        "une",
        "with",
    }
)
_OPEN_MARKERS: Final[tuple[str, ...]] = (
    "a confirmer",
    "awaiting",
    "en attente",
    "pending",
    "question ouverte",
    "reste a trancher",
    "tbd",
)
_CONTRADICTION_MARKERS: Final[tuple[str, ...]] = (
    "ancienne version",
    "contrairement",
    "correction",
    "ne plus",
    "obsolete",
    "perime",
    "remplace",
    "replaces",
    "superseded",
)
_REFINEMENT_MARKERS: Final[tuple[str, ...]] = (
    "clarifie",
    "complete",
    "confirme",
    "mise a jour",
    "precision",
    "raffinement",
)


class CandidateClass(StrEnum):
    """Conservative classification attached to every candidate."""

    CONTRADICTION = "CONTRADICTION"
    REFINEMENT = "RAFFINEMENT"
    OPEN_QUESTION = "QUESTION_OUVERTE"


class MutationScope(StrEnum):
    """Scope of the proposed lifecycle mutation."""

    SECTION = "section"
    WHOLE_NOTE = "whole_note"


@dataclass(frozen=True)
class SectionAssertion:
    """One section-level unit reconstructed from indexed chunks."""

    note_id: str
    note_rel_path: str
    header_path: str
    section_title: str | None
    chunk_id: str
    line_start: int
    line_end: int
    content: str

    @property
    def stable_key(self) -> tuple[str, str]:
        return self.note_id, self.chunk_id


@dataclass(frozen=True)
class Candidate:
    """Internal candidate with enough authority to derive a CAS proposal."""

    target: SectionAssertion
    source: SectionAssertion
    score: float
    classification: CandidateClass
    rationale: str
    addressable: bool = False
    heading_level: int | None = None
    expected_hash: str | None = None
    manual_action: str | None = None


@dataclass(frozen=True)
class Proposal:
    """One immutable, content-addressed write-tool proposal."""

    token: str
    candidate: Candidate
    classification: CandidateClass
    scope: MutationScope
    tool: str
    block: str | None
    proposal_date: date


@dataclass
class _SectionBuilder:
    chunks: list[Chunk]


def format_update_block(
    classification: CandidateClass,
    statement: str,
    source_rel_path: str,
    *,
    today: date,
) -> str:
    """Return the one canonical dated section-level provenance block."""
    label = core_config.DEFAULT_CONTRADICTION_PROVENANCE_LABELS[classification.name.lower()]
    cleaned_statement = _clean_statement(statement, classification)
    sentence = (
        cleaned_statement
        if cleaned_statement.endswith((".", "?", "!"))
        else f"{cleaned_statement}."
    )
    connector = core_config.DEFAULT_CONTRADICTION_SOURCE_CONNECTOR
    return f"> {label} {today.isoformat()} : {sentence} {connector} {source_rel_path}."


async def build_scan_report(
    app: DatacronApp,
    *,
    today: date | None = None,
    detail: Literal["summary", "full"] = "summary",
) -> tuple[dict[str, Any], list[Candidate]]:
    """Scan the live index within configured bounds and return stable candidates."""
    proposal_date = today or datetime.now(tz=UTC).date()
    candidates, examined_pairs, section_count = await _scan_candidate_models(app)
    payload_candidates = [
        _candidate_payload(
            app,
            candidate,
            proposal_date=proposal_date,
            detail=detail,
        )
        for candidate in candidates
    ]
    payload = {
        "schema_version": CONTRADICTION_SCHEMA_VERSION,
        "mode": "scan",
        "candidates": payload_candidates,
        "candidate_count": len(payload_candidates),
        "examined_pairs": examined_pairs,
        "section_count": section_count,
        "limits": {
            "max_pairs": app.settings.contradiction_max_pairs,
            "max_candidates": app.settings.contradiction_max_candidates,
            "max_per_note_pair": app.settings.contradiction_max_per_note_pair,
        },
        "deterministic_order": (
            "score_desc,target_note_id,target_chunk_id,source_note_id,source_chunk_id"
        ),
    }
    return payload, candidates


async def confirm_proposal(
    app: DatacronApp,
    proposal_token: str,
) -> dict[str, Any]:
    """Recompute and confirm one proposal without mutating server or vault state."""
    proposal_date = _proposal_date_from_token(proposal_token)
    candidates, _examined_pairs, _section_count = await _scan_candidate_models(app)
    proposal = _find_proposal(
        candidates,
        proposal_token=proposal_token,
        proposal_date=proposal_date,
    )
    if proposal is None:
        raise ValueError("proposal token is unknown or stale; rerun contradiction_scan")

    target_note = await _read_note(app, proposal.candidate.target.note_rel_path)
    if target_note.content_hash != proposal.candidate.expected_hash:
        raise ValueError("proposal token is stale because the target note changed; rerun scan")

    write_call = _write_call(target_note, proposal)
    verification_query = _verification_query(proposal.candidate.source.content)
    return {
        "schema_version": CONTRADICTION_SCHEMA_VERSION,
        "mode": "confirm",
        "confirmation": {
            "proposal_token": proposal.token,
            "classification": proposal.classification.value,
            "scope": proposal.scope.value,
            "write_call": write_call,
            "workflow": {
                "execute": f"Execute the returned {proposal.tool} call explicitly.",
                "verify": "Then call search_text and inspect the mutated section.",
                "verification_tool": "search_text",
                "verification_query": verification_query,
            },
        },
    }


def build_proposal(
    candidate: Candidate,
    *,
    classification: CandidateClass,
    scope: MutationScope,
    today: date,
) -> Proposal:
    """Derive one safe proposal; invalid class/scope combinations cannot be built."""
    if not candidate.addressable or candidate.expected_hash is None:
        raise ValueError("candidate target is not addressable")
    if scope is MutationScope.WHOLE_NOTE and classification is not CandidateClass.CONTRADICTION:
        raise ValueError("whole-note invalidation requires CONTRADICTION classification")

    statement = _statement(candidate.source.content, classification)
    if scope is MutationScope.SECTION:
        if candidate.heading_level is None or candidate.target.section_title is None:
            raise ValueError("section proposal requires an addressable heading")
        tool = "patch_note_section"
        block = format_update_block(
            classification,
            statement,
            candidate.source.note_rel_path,
            today=today,
        )
        mutation_fingerprint: dict[str, object] = {
            "tool": tool,
            "heading": candidate.target.section_title,
            "heading_level": candidate.heading_level,
            "block": block,
        }
    else:
        tool = "set_frontmatter"
        block = None
        mutation_fingerprint = {
            "tool": tool,
            "invalid_at": f"{today.isoformat()}T00:00:00+00:00",
            "invalidated_by": candidate.source.note_id,
        }

    canonical = {
        "schema_version": CONTRADICTION_SCHEMA_VERSION,
        "target_note_id": candidate.target.note_id,
        "target_chunk_id": candidate.target.chunk_id,
        "source_note_id": candidate.source.note_id,
        "source_chunk_id": candidate.source.chunk_id,
        "classification": classification.value,
        "scope": scope.value,
        "expected_hash": candidate.expected_hash,
        "mutation": mutation_fingerprint,
    }
    digest = hashlib.sha256(_canonical_json(canonical)).hexdigest()
    token = f"{_PROPOSAL_PREFIX}:{today.isoformat()}:{digest}"
    return Proposal(
        token=token,
        candidate=candidate,
        classification=classification,
        scope=scope,
        tool=tool,
        block=block,
        proposal_date=today,
    )


async def _scan_candidate_models(
    app: DatacronApp,
) -> tuple[list[Candidate], int, int]:
    sections = await _collect_sections(app, limit=app.settings.contradiction_max_pairs)
    section_by_key = {(item.note_id, item.header_path): item for item in sections}
    seen_pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    raw_candidates: list[Candidate] = []

    for section in sections:
        if len(seen_pairs) >= app.settings.contradiction_max_pairs:
            break
        query = _query(section)
        if not query:
            continue
        results = await app.store.search(query, limit=_SEARCH_NEIGHBOR_LIMIT)
        for result in results:
            other = section_by_key.get((result.chunk.note_id, result.chunk.header_path))
            if other is None or other.note_id == section.note_id:
                continue
            ordered_pair = sorted((section.stable_key, other.stable_key))
            pair_key = (ordered_pair[0], ordered_pair[1])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            candidate = _classify_pair(section, other)
            if candidate is not None:
                raw_candidates.append(candidate)
            if len(seen_pairs) >= app.settings.contradiction_max_pairs:
                break

    raw_candidates.sort(
        key=lambda item: (
            -item.score,
            item.target.note_id,
            item.target.chunk_id,
            item.source.note_id,
            item.source.chunk_id,
        )
    )
    note_pair_counts: Counter[tuple[str, str]] = Counter()
    capped_candidates: list[Candidate] = []
    for candidate in raw_candidates:
        first_note_id, second_note_id = sorted((candidate.target.note_id, candidate.source.note_id))
        note_pair = (first_note_id, second_note_id)
        if note_pair_counts[note_pair] >= app.settings.contradiction_max_per_note_pair:
            continue
        note_pair_counts[note_pair] += 1
        capped_candidates.append(candidate)
    limited = capped_candidates[: app.settings.contradiction_max_candidates]
    indexed_notes = await app.store.list_indexed_notes()
    note_cache: dict[str, Note] = {}
    resolved = [
        await _resolve_addressability(app, candidate, indexed_notes, note_cache)
        for candidate in limited
    ]
    return resolved, len(seen_pairs), len(sections)


async def _collect_sections(app: DatacronApp, *, limit: int) -> list[SectionAssertion]:
    builders: dict[tuple[str, str], _SectionBuilder] = {}
    async for chunk in app.store.iter_all_chunks():
        if chunk.chunk_type in {ChunkType.FRONTMATTER, ChunkType.HEADING, ChunkType.CODE}:
            continue
        key = (chunk.note_id, chunk.header_path)
        if key not in builders and len(builders) >= limit:
            break
        builders.setdefault(key, _SectionBuilder(chunks=[])).chunks.append(chunk)

    sections: list[SectionAssertion] = []
    for builder in builders.values():
        chunks = sorted(builder.chunks, key=lambda item: (item.line_start, item.chunk_id))
        if not chunks:
            continue
        first = chunks[0]
        content = "\n\n".join(item.content.strip() for item in chunks if item.content.strip())
        if not content:
            continue
        sections.append(
            SectionAssertion(
                note_id=first.note_id,
                note_rel_path=first.note_rel_path,
                header_path=first.header_path,
                section_title=first.section_title,
                chunk_id=first.chunk_id,
                line_start=min(item.line_start for item in chunks),
                line_end=max(item.line_end for item in chunks),
                content=content,
            )
        )
    return sorted(sections, key=lambda item: item.stable_key)


def _classify_pair(left: SectionAssertion, right: SectionAssertion) -> Candidate | None:
    if _already_handled(left, right):
        return None
    left_terms = _terms(_without_update_blocks(left.content))
    right_terms = _terms(_without_update_blocks(right.content))
    if not left_terms or not right_terms:
        return None
    lexical_score = len(left_terms & right_terms) / min(len(left_terms), len(right_terms))
    left_date = _latest_content_date(left)
    right_date = _latest_content_date(right)
    has_temporal_signal = left_date is not None and right_date is not None
    normalized_pair = _normalize_text(f"{left.content}\n{right.content}")
    has_marker = _contains_any(
        normalized_pair,
        _OPEN_MARKERS + _CONTRADICTION_MARKERS + _REFINEMENT_MARKERS,
    )
    threshold = _MIN_MARKED_SCORE if has_marker or has_temporal_signal else _MIN_LEXICAL_SCORE
    if lexical_score < threshold:
        return None

    target, source = _ordered_sides(left, right, left_date=left_date, right_date=right_date)
    classification, rationale = _classification(source.content)
    score = round(min(1.0, lexical_score + (0.05 if has_temporal_signal else 0.0)), 6)
    return Candidate(
        target=target,
        source=source,
        score=score,
        classification=classification,
        rationale=rationale,
    )


async def _resolve_addressability(  # noqa: PLR0911 - guard clauses preserve refusal reasons
    app: DatacronApp,
    candidate: Candidate,
    indexed_notes: dict[str, tuple[str, str]],
    note_cache: dict[str, Note],
) -> Candidate:
    target_indexed = indexed_notes.get(candidate.target.note_rel_path)
    source_indexed = indexed_notes.get(candidate.source.note_rel_path)
    if target_indexed is None or source_indexed is None:
        return replace(
            candidate,
            manual_action="Indexed note authority is unavailable; rerun scan.",
        )

    try:
        target_note = await _cached_note(app, candidate.target.note_rel_path, note_cache)
        source_note = await _cached_note(app, candidate.source.note_rel_path, note_cache)
    except (FileNotFoundError, ValueError):
        return replace(candidate, manual_action="A candidate note is unreadable; inspect manually.")

    if (
        target_note.content_hash != target_indexed[1]
        or source_note.content_hash != source_indexed[1]
    ):
        return replace(candidate, manual_action="The index is stale; rerun scan before proposing.")
    if _contains_redacted_material(app, candidate, target_note):
        return replace(
            candidate,
            manual_action="Sensitive content was redacted; review this candidate manually.",
        )

    candidate = _guard_cross_project_contradiction(candidate, target_note, source_note)

    selector = _addressable_selector(target_note.content, candidate.target.header_path)
    if selector is None:
        return replace(
            candidate,
            manual_action=(
                "Target heading is missing or ambiguous for patch_note_section; edit manually."
            ),
        )
    heading, level = selector
    if heading != candidate.target.section_title:
        return replace(candidate, manual_action="Indexed heading no longer matches the note.")
    return replace(
        candidate,
        addressable=True,
        heading_level=level,
        expected_hash=target_note.content_hash,
    )


def _candidate_payload(
    app: DatacronApp,
    candidate: Candidate,
    *,
    proposal_date: date,
    detail: Literal["summary", "full"],
) -> dict[str, Any]:
    suggested: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = []
    if candidate.addressable:
        proposals = _proposal_variants(candidate, proposal_date=proposal_date)
        for proposal in proposals:
            is_suggested = (
                proposal.classification is candidate.classification
                and proposal.scope is MutationScope.SECTION
            )
            rendered = _proposal_summary(
                app,
                proposal,
                include_block=detail == "full" or is_suggested,
            )
            if is_suggested:
                suggested = rendered
            else:
                alternatives.append(rendered)

    return {
        "candidate_id": _candidate_id(candidate),
        "score": candidate.score,
        "class": candidate.classification.value,
        "classification_options": _classification_options(candidate.classification),
        "rationale": candidate.rationale,
        "evidence": {
            "target": _redact(
                app,
                _excerpt(candidate.target.content, limit=_evidence_limit(app, detail)),
            ),
            "source": _redact(
                app,
                _excerpt(candidate.source.content, limit=_evidence_limit(app, detail)),
            ),
        },
        "target": _section_reference(app, candidate.target),
        "source": _section_reference(app, candidate.source),
        "addressable": candidate.addressable,
        "manual_action": candidate.manual_action,
        "suggested_mutation": suggested,
        "alternative_mutations": alternatives,
    }


def _proposal_variants(candidate: Candidate, *, proposal_date: date) -> list[Proposal]:
    proposals = [
        build_proposal(
            candidate,
            classification=classification,
            scope=MutationScope.SECTION,
            today=proposal_date,
        )
        for classification in CandidateClass
    ]
    proposals.append(
        build_proposal(
            candidate,
            classification=CandidateClass.CONTRADICTION,
            scope=MutationScope.WHOLE_NOTE,
            today=proposal_date,
        )
    )
    return proposals


def _find_proposal(
    candidates: list[Candidate],
    *,
    proposal_token: str,
    proposal_date: date,
) -> Proposal | None:
    for candidate in candidates:
        if not candidate.addressable:
            continue
        for proposal in _proposal_variants(candidate, proposal_date=proposal_date):
            if proposal.token == proposal_token:
                return proposal
    return None


def _proposal_summary(
    app: DatacronApp,
    proposal: Proposal,
    *,
    include_block: bool,
) -> dict[str, Any]:
    return {
        "proposal_token": proposal.token,
        "classification": proposal.classification.value,
        "scope": proposal.scope.value,
        "tool": proposal.tool,
        "block": (
            _redact(app, proposal.block) if include_block and proposal.block is not None else None
        ),
    }


def _write_call(target_note: Note, proposal: Proposal) -> dict[str, Any]:
    candidate = proposal.candidate
    if proposal.scope is MutationScope.WHOLE_NOTE:
        return {
            "tool": "set_frontmatter",
            "arguments": {
                "rel_path": candidate.target.note_rel_path,
                "invalid_at": f"{proposal.proposal_date.isoformat()}T00:00:00+00:00",
                "invalidated_by": candidate.source.note_id,
                "expected_hash": candidate.expected_hash,
            },
        }

    if (
        proposal.block is None
        or candidate.target.section_title is None
        or candidate.heading_level is None
    ):
        raise ValueError("section proposal is incomplete")
    lines = target_note.content.splitlines(keepends=True)
    content_start, content_end = find_section_span(
        lines,
        candidate.target.section_title,
        candidate.heading_level,
    )
    current_content = "".join(lines[content_start:content_end]).rstrip()
    new_content = f"{current_content}\n\n{proposal.block}" if current_content else proposal.block
    return {
        "tool": "patch_note_section",
        "arguments": {
            "rel_path": candidate.target.note_rel_path,
            "heading": candidate.target.section_title,
            "new_content": new_content,
            "expected_hash": candidate.expected_hash,
            "heading_level": candidate.heading_level,
        },
    }


def _addressable_selector(body: str, header_path: str) -> tuple[str, int] | None:
    stack: list[str] = []
    entries: list[tuple[str, int, str]] = []
    for line in body.splitlines():
        parsed = parse_heading_line(line)
        if parsed is None:
            continue
        level, text = parsed
        stack = stack[: max(level - 1, 0)]
        stack.append(text)
        entries.append((text, level, _HEADING_SEPARATOR.join(stack)))

    path_matches = [entry for entry in entries if entry[2] == header_path]
    if len(path_matches) != 1:
        return None
    text, level, _path = path_matches[0]
    if sum(entry[0] == text and entry[1] == level for entry in entries) != 1:
        return None
    return text, level


def _already_handled(left: SectionAssertion, right: SectionAssertion) -> bool:
    left_latest = _latest_content_date(left)
    right_latest = _latest_content_date(right)
    left_blocks = _block_dates(left.content)
    right_blocks = _block_dates(right.content)
    return bool(
        (right_latest is not None and any(block >= right_latest for block in left_blocks))
        or (left_latest is not None and any(block >= left_latest for block in right_blocks))
    )


def _ordered_sides(
    left: SectionAssertion,
    right: SectionAssertion,
    *,
    left_date: date | None,
    right_date: date | None,
) -> tuple[SectionAssertion, SectionAssertion]:
    if left_date is not None and right_date is not None and left_date != right_date:
        return (left, right) if left_date < right_date else (right, left)
    return (left, right) if left.stable_key < right.stable_key else (right, left)


def _classification(content: str) -> tuple[CandidateClass, str]:
    normalized = _normalize_text(_without_update_blocks(content))
    has_open = _contains_any(normalized, _OPEN_MARKERS) or "?" in content
    has_contradiction = _contains_any(normalized, _CONTRADICTION_MARKERS)
    has_refinement = _contains_any(normalized, _REFINEMENT_MARKERS)
    if has_open:
        return CandidateClass.OPEN_QUESTION, "The newer section explicitly leaves the issue open."
    if has_contradiction and has_refinement:
        return CandidateClass.REFINEMENT, (
            "The section mixes replacement and clarification signals; "
            "refinement is the safe default."
        )
    if has_contradiction:
        return CandidateClass.CONTRADICTION, (
            "The newer section explicitly marks earlier information as replaced or obsolete."
        )
    if has_refinement:
        return CandidateClass.REFINEMENT, (
            "The newer section explicitly confirms, clarifies, or extends the earlier information."
        )
    return CandidateClass.OPEN_QUESTION, (
        "The lexical overlap is real but the relationship is not classifiable without review."
    )


def _guard_cross_project_contradiction(
    candidate: Candidate,
    target_note: Note,
    source_note: Note,
) -> Candidate:
    """Avoid suggesting contradictions across unrelated, unordered projects."""
    if candidate.classification is not CandidateClass.CONTRADICTION:
        return candidate
    has_temporal_signal = _has_explicit_temporal_order(candidate)
    target_projects = _frontmatter_project_tags(target_note)
    source_projects = _frontmatter_project_tags(source_note)
    if (
        has_temporal_signal
        or not (target_projects or source_projects)
        or not target_projects.isdisjoint(source_projects)
    ):
        return candidate
    return replace(
        candidate,
        classification=CandidateClass.OPEN_QUESTION,
        rationale=(
            "The notes share no project tag and provide no explicit temporal ordering; "
            "the relationship requires review."
        ),
    )


def _has_explicit_temporal_order(candidate: Candidate) -> bool:
    target_dates = _dates(_without_update_blocks(candidate.target.content))
    source_dates = _dates(_without_update_blocks(candidate.source.content))
    return bool(target_dates and source_dates)


def _frontmatter_project_tags(note: Note) -> frozenset[str]:
    raw_tags = note.frontmatter.get("tags", [])
    if isinstance(raw_tags, str):
        values = raw_tags.split(",")
    elif isinstance(raw_tags, list):
        values = [str(item) for item in raw_tags]
    else:
        return frozenset()
    return frozenset(
        normalized
        for value in values
        if (normalized := value.strip().casefold()).startswith("project/")
    )


def _classification_options(suggested: CandidateClass) -> list[str]:
    safe_order = [
        suggested,
        CandidateClass.REFINEMENT,
        CandidateClass.OPEN_QUESTION,
        CandidateClass.CONTRADICTION,
    ]
    return list(dict.fromkeys(item.value for item in safe_order))


def _statement(content: str, classification: CandidateClass) -> str:
    excerpt = _excerpt(_without_update_blocks(content), limit=_STATEMENT_CHAR_LIMIT)
    return _clean_statement(excerpt, classification)


def _clean_statement(statement: str, classification: CandidateClass) -> str:
    cleaned = _WHITESPACE_PATTERN.sub(" ", statement).strip().rstrip(".?! ")
    if not cleaned:
        cleaned = "Review the newer source section"
    if classification is CandidateClass.OPEN_QUESTION:
        return f"{cleaned}?"
    return cleaned


def _excerpt(content: str, *, limit: int = _EVIDENCE_CHAR_LIMIT) -> str:
    lines = [
        _MARKDOWN_PREFIX_PATTERN.sub("", line).strip()
        for line in content.splitlines()
        if line.strip()
    ]
    rendered = _WHITESPACE_PATTERN.sub(" ", " ".join(lines)).strip()
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[: max(limit - 3, 0)].rstrip()}..."


def _evidence_limit(
    app: DatacronApp,
    detail: Literal["summary", "full"],
) -> int:
    if detail == "full":
        return _EVIDENCE_CHAR_LIMIT
    return app.settings.contradiction_summary_evidence_chars


def _query(section: SectionAssertion) -> str:
    terms = _terms(f"{section.header_path}\n{_without_update_blocks(section.content)}")
    selected = sorted(terms, key=lambda item: (-len(item), item))[:_QUERY_TERM_LIMIT]
    return " ".join(selected)


def _terms(value: str) -> set[str]:
    return {
        term
        for term in _WORD_PATTERN.findall(_normalize_text(value))
        if term not in _STOPWORDS and not term.isdigit()
    }


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return ascii_text.casefold()


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _latest_content_date(section: SectionAssertion) -> date | None:
    without_blocks = _without_update_blocks(section.content)
    dates = _dates(f"{section.note_rel_path}\n{section.header_path}\n{without_blocks}")
    return max(dates) if dates else None


def _dates(value: str) -> list[date]:
    parsed: list[date] = []
    for match in _ISO_DATE_PATTERN.findall(value):
        try:
            parsed.append(date.fromisoformat(match))
        except ValueError:
            continue
    return parsed


def _block_dates(value: str) -> list[date]:
    return [
        date.fromisoformat(match.group("date")) for match in _UPDATE_BLOCK_PATTERN.finditer(value)
    ]


def _without_update_blocks(value: str) -> str:
    return "\n".join(
        line for line in value.splitlines() if _UPDATE_BLOCK_PATTERN.match(line) is None
    )


def _candidate_id(candidate: Candidate) -> str:
    canonical = {
        "target": candidate.target.chunk_id,
        "source": candidate.source.chunk_id,
    }
    return hashlib.sha256(_canonical_json(canonical)).hexdigest()


def _section_reference(app: DatacronApp, section: SectionAssertion) -> dict[str, Any]:
    return {
        "note_id": section.note_id,
        "note_rel_path": _redact(app, section.note_rel_path),
        "header_path": _redact(app, section.header_path),
        "chunk_id": section.chunk_id,
        "line_start": section.line_start,
        "line_end": section.line_end,
    }


def _verification_query(content: str) -> str:
    selected = sorted(_terms(content), key=lambda item: (-len(item), item))[:3]
    return " ".join(selected)


def _proposal_date_from_token(token: str) -> date:
    match = _TOKEN_PATTERN.fullmatch(token)
    if match is None:
        raise ValueError("proposal_token must match cs2:YYYY-MM-DD:<sha256>")
    try:
        return date.fromisoformat(match.group(1))
    except ValueError as exc:
        raise ValueError("proposal_token contains an invalid date") from exc


async def _cached_note(
    app: DatacronApp,
    rel_path: str,
    cache: dict[str, Note],
) -> Note:
    if rel_path not in cache:
        cache[rel_path] = await _read_note(app, rel_path)
    return cache[rel_path]


async def _read_note(app: DatacronApp, rel_path: str) -> Note:
    path = app.scope.authorize_rel_path(rel_path, "read")
    return await app.vault_reader.read_note(path)


def _contains_redacted_material(
    app: DatacronApp,
    candidate: Candidate,
    target_note: Note,
) -> bool:
    if not app.secret_redactor.retrieval_enabled(app.settings):
        return False
    values = (
        candidate.target.note_rel_path,
        candidate.source.note_rel_path,
        candidate.target.content,
        candidate.source.content,
        target_note.content,
    )
    return any(app.secret_redactor.redact_text(value) != value for value in values)


def _redact(app: DatacronApp, value: str) -> str:
    if not app.secret_redactor.retrieval_enabled(app.settings):
        return value
    return app.secret_redactor.redact_text(value)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
