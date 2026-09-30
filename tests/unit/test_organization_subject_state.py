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
"""A subject's state note and the subject notes dated after its verification."""

from __future__ import annotations

from typing import Any

import pytest

from datacron.core.config import OrganizationConfig
from datacron.organization.subject_state import (
    SubjectNote,
    note_subject_tags,
    resolve_subject_tag,
    summarize_subject,
)


def _organization(subjects: list[dict[str, Any]]) -> OrganizationConfig:
    return OrganizationConfig.model_validate(
        {
            "scope": "_memory",
            "rules": [{"tag": "memory/fact", "folder": "_memory/facts"}],
            "tags": {
                "placement_namespace": "memory",
                "subject_namespace": "project",
                "subjects": subjects,
                "allowed_namespaces": ["kind"],
            },
        }
    )


_HEIMDALL = {"tag": "project/heimdall", "aliases": ["heimdall.next"]}


@pytest.mark.parametrize(
    "subject",
    [
        "project/heimdall",
        "Project/Heimdall",
        "#project/heimdall",
        "heimdall",
        "  HEIMDALL ",
        "heimdall.next",
    ],
)
def test_a_subject_resolves_by_tag_leaf_or_alias(subject: str) -> None:
    assert resolve_subject_tag(subject, _organization([_HEIMDALL])) == "project/heimdall"


def test_an_unknown_subject_resolves_to_nothing() -> None:
    assert resolve_subject_tag("release process", _organization([_HEIMDALL])) is None


def test_an_ambiguous_subject_resolves_to_nothing() -> None:
    organization = _organization(
        [_HEIMDALL, {"tag": "project/heimdall-rs", "aliases": ["heimdall"]}]
    )

    assert resolve_subject_tag("heimdall", organization) is None


def test_without_a_tag_policy_no_subject_resolves() -> None:
    assert resolve_subject_tag("heimdall", None) is None
    assert note_subject_tags(["project/heimdall"], None) == ()


def test_note_subject_tags_keeps_only_registered_subjects() -> None:
    organization = _organization([_HEIMDALL])

    assert note_subject_tags(["memory/fact", "project/heimdall", "ssh"], organization) == (
        "project/heimdall",
    )


def _note(rel_path: str, tags: tuple[str, ...], **metadata: Any) -> SubjectNote:
    return SubjectNote.from_metadata(rel_path, tags, metadata)


def test_newer_notes_count_evidence_dated_after_last_verified() -> None:
    notes = [
        _note(
            "_memory/subjects/heimdall/heimdall.md",
            ("project/heimdall", "kind/development"),
            created="2026-06-01T08:00:00+00:00",
            last_verified="2026-09-13",
        ),
        _note("_memory/facts/2026-09-12-old.md", ("project/heimdall",), created="2026-09-12"),
        _note("_memory/facts/2026-09-13-same-day.md", ("project/heimdall",), created="2026-09-13"),
        _note("_memory/facts/2026-09-25-rust.md", ("project/heimdall",), created="2026-09-25"),
        _note(
            "_memory/subjects/heimdall/heimdall-history-2026-09.md",
            ("project/heimdall",),
            created="2026-09-20",
        ),
        _note("_memory/facts/undated.md", ("project/heimdall",)),
    ]

    state = summarize_subject("project/heimdall", notes)

    assert state.subject_notes == len(notes)
    assert [item.rel_path for item in state.state_notes] == [
        "_memory/subjects/heimdall/heimdall.md"
    ]
    # Only the 09-25 note: same-day, older, history and undated notes are not news.
    assert state.state_notes[0].newer_notes == 1


def test_a_state_note_never_verified_has_no_reference_point() -> None:
    notes = [
        _note("_memory/s/state.md", ("project/x", "kind/mission"), created="2026-01-01"),
        _note("_memory/s/2026-09-01-fact.md", ("project/x",), created="2026-09-01"),
    ]

    state = summarize_subject("project/x", notes)

    assert state.state_notes[0].last_verified is None
    assert state.state_notes[0].newer_notes is None


def test_a_subject_without_state_note_reports_none() -> None:
    state = summarize_subject("project/x", [_note("_memory/a.md", ("project/x",))])

    assert state.state_notes == ()
    assert state.subject_notes == 1


def test_link_names_cover_stem_title_and_aliases() -> None:
    note = _note(
        "_memory/s/heimdall.md", ("kind/development",), title="Projet Heimdall", aliases=["HN"]
    )

    assert note.link_names() == frozenset({"heimdall", "projet heimdall", "hn"})
