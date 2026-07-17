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
"""Pure contract tests for contradiction scan v2 proposals."""

from __future__ import annotations

from datetime import date

import pytest

from datacron.contradictions import (
    Candidate,
    CandidateClass,
    MutationScope,
    SectionAssertion,
    build_proposal,
    format_update_block,
)

_TODAY = date(2026, 7, 17)


def _candidate() -> Candidate:
    target = SectionAssertion(
        note_id="01HQXR7K9YZ8M2N3PQRSTV4WX5",
        note_rel_path="_memory/facts/old.md",
        header_path="Identity / Employer",
        section_title="Employer",
        chunk_id="01HQXR7K9YZ8M2N3PQRSTV4WX5::identity/employer::0000",
        line_start=5,
        line_end=7,
        content="The Windows team employer is Magellan.",
    )
    source = SectionAssertion(
        note_id="01HQXR7K9YZ8M2N3PQRSTV4WX6",
        note_rel_path="_memory/facts/current.md",
        header_path="Identity / Employer 2026-07-15",
        section_title="Employer 2026-07-15",
        chunk_id="01HQXR7K9YZ8M2N3PQRSTV4WX6::identity/employer-2026-07-15::0000",
        line_start=5,
        line_end=7,
        content="The current Windows team employer is Worldline.",
    )
    return Candidate(
        target=target,
        source=source,
        score=0.8,
        classification=CandidateClass.CONTRADICTION,
        rationale="Explicit replacement.",
        addressable=True,
        heading_level=2,
        expected_hash="a" * 64,
    )


@pytest.mark.parametrize(
    ("classification", "label"),
    [
        (CandidateClass.CONTRADICTION, "CORRECTION"),
        (CandidateClass.REFINEMENT, "MISE A JOUR"),
        (CandidateClass.OPEN_QUESTION, "QUESTION OUVERTE"),
    ],
)
def test_section_classes_have_one_canonical_provenance_block(
    classification: CandidateClass,
    label: str,
) -> None:
    proposal = build_proposal(
        _candidate(),
        classification=classification,
        scope=MutationScope.SECTION,
        today=_TODAY,
    )

    assert proposal.tool == "patch_note_section"
    assert proposal.block is not None
    assert proposal.block.startswith(f"> {label} 2026-07-17 : ")
    assert proposal.block.endswith("Voir _memory/facts/current.md.")
    assert proposal.token.startswith("cs2:2026-07-17:")


def test_open_question_formatter_has_exact_punctuation() -> None:
    rendered = format_update_block(
        CandidateClass.OPEN_QUESTION,
        "Which direction remains active?",
        "_memory/projects/source.md",
        today=_TODAY,
    )

    assert rendered == (
        "> QUESTION OUVERTE 2026-07-17 : Which direction remains active? "
        "Voir _memory/projects/source.md."
    )


def test_whole_note_invalidation_is_contradiction_only() -> None:
    proposal = build_proposal(
        _candidate(),
        classification=CandidateClass.CONTRADICTION,
        scope=MutationScope.WHOLE_NOTE,
        today=_TODAY,
    )

    assert proposal.tool == "set_frontmatter"
    assert proposal.block is None
    with pytest.raises(
        ValueError,
        match="whole-note invalidation requires CONTRADICTION classification",
    ):
        build_proposal(
            _candidate(),
            classification=CandidateClass.REFINEMENT,
            scope=MutationScope.WHOLE_NOTE,
            today=_TODAY,
        )


def test_proposal_token_is_deterministic_and_content_addressed() -> None:
    first = build_proposal(
        _candidate(),
        classification=CandidateClass.CONTRADICTION,
        scope=MutationScope.SECTION,
        today=_TODAY,
    )
    second = build_proposal(
        _candidate(),
        classification=CandidateClass.CONTRADICTION,
        scope=MutationScope.SECTION,
        today=_TODAY,
    )
    changed = build_proposal(
        _candidate(),
        classification=CandidateClass.REFINEMENT,
        scope=MutationScope.SECTION,
        today=_TODAY,
    )

    assert first.token == second.token
    assert first.token != changed.token
