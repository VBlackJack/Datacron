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

"""Where a subject's state note is, and what a tool response says about it.

``session_context`` and ``create_note_ai`` both need the same answer: which note
holds the current state of a subject, and how many of the subject's notes are
dated after that note was last verified. The drift this measures is otherwise
visible only to someone who runs ``datacron reorganize``, which no session does
at the moment it writes or reads a subject.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from datacron.core.case_folding import filesystem_folds_case
from datacron.core.models import ChunkType
from datacron.indexing.wikilinks import extract_wikilink_targets
from datacron.mcp.tools.payloads import _redact_retrieval_text
from datacron.organization.rules import resolve_rule
from datacron.organization.subject_state import (
    SubjectNote,
    SubjectState,
    has_state_note_tag,
    note_subject_tags,
    summarize_subject,
)
from datacron.organization.tags import path_within_scope

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = [
    "FOLD_GUIDANCE",
    "load_subject_notes",
    "placement_guidance",
    "subject_state_payload",
]

FOLD_GUIDANCE: Final[str] = (
    "Also fold this change into the subject's state note: update its dated state, "
    "add a dated one-line entry with a wikilink to this note, then set last_verified. "
    "The new note stays as the evidence."
)
_ROOT_FOLDER: Final[str] = ""
_WINDOWS_SEPARATOR: Final[str] = "\\"
_POSIX_SEPARATOR: Final[str] = "/"


async def load_subject_notes(app: DatacronApp, subject_tag: str) -> list[SubjectNote]:
    """Return every admitted indexed note carrying ``subject_tag``.

    The index answers in one query without reading a note; each row then goes
    through the read scope's admission check, which resolves the path on disk,
    so the cost grows with the size of the subject.

    Args:
        app: The running server.
        subject_tag: A canonical registered subject tag.

    Returns:
        One content-free record per note the read scope admits.
    """
    rows = await app.store.list_tagged_notes(subject_tag)
    return [
        SubjectNote.from_metadata(rel_path, tags, metadata)
        for rel_path, tags, metadata in rows
        if app.scope.allows_note_rel_path(rel_path)
    ]


def subject_state_payload(
    app: DatacronApp, state: SubjectState, max_state_notes: int
) -> dict[str, Any]:
    """Render a subject state for a tool response, bounded in state notes.

    Paths go through the same retrieval redaction as every other path a read
    tool returns, so a secret-shaped path segment is not served in clear here
    while ``sources`` redacts it.

    Args:
        app: The running server, for its redaction policy.
        state: The measured subject state.
        max_state_notes: The most state notes to list.

    Returns:
        A JSON-ready mapping; ``state_notes_omitted`` counts what the bound cut.
    """
    listed = state.state_notes[: max(max_state_notes, 0)]
    return {
        "subject_tag": state.subject_tag,
        "subject_notes": state.subject_notes,
        "state_notes": [
            {
                "rel_path": _redact_retrieval_text(app, item.rel_path),
                "last_verified": item.last_verified,
                "newer_notes": item.newer_notes,
            }
            for item in listed
        ],
        "state_notes_omitted": len(state.state_notes) - len(listed),
    }


async def placement_guidance(
    app: DatacronApp,
    *,
    rel_path: str,
    tags: list[str],
    body: str,
    max_state_notes: int,
) -> dict[str, Any] | None:
    """Describe where a just-created note belongs and which state note it feeds.

    Nothing here refuses a write: the note is already committed when this runs.
    The response names the folder the vault's rule expects when the note is
    elsewhere, and, for a note of a registered subject, the subject's state
    notes and whether the new note already links one of them.

    Args:
        app: The running server.
        rel_path: The vault-relative path of the created note.
        tags: The note's effective tags.
        body: The note body, scanned for wikilinks.
        max_state_notes: The most state notes to list.

    Returns:
        The guidance mapping, or ``None`` when the vault declares nothing to say.
    """
    organization = app.organization
    if organization is None or not organization.rules:
        return None
    guidance: dict[str, Any] = {}
    # A Windows caller may spell the path with backslashes, which PurePosixPath
    # would read as one file name in the vault root.
    posix_rel_path = rel_path.replace(_WINDOWS_SEPARATOR, _POSIX_SEPARATOR)
    fold_case = filesystem_folds_case(app.vault_root)
    rule = resolve_rule(tags, organization)
    # Placement is judged inside the organization scope only, as the planner and
    # the tag policy judge it; a note outside the scope has no expected folder.
    in_scope = organization.scope is not None and path_within_scope(
        posix_rel_path, organization.scope, fold_case=fold_case
    )
    if rule is not None and in_scope:
        folder = PurePosixPath(posix_rel_path).parent.as_posix()
        folder = _ROOT_FOLDER if folder == "." else folder
        if _folder_key(folder, fold_case) != _folder_key(rule.folder, fold_case):
            guidance["expected_folder"] = rule.folder
    subjects = note_subject_tags(tags, organization)
    # A new state note is the destination of the fold, never a source that owes one.
    if subjects and not has_state_note_tag(tags):
        notes = await load_subject_notes(app, subjects[0])
        state = summarize_subject(subjects[0], notes)
        if state.state_notes:
            state_names = frozenset().union(
                *(note.link_names() for note in notes if note.is_state_note)
            )
            targets = {
                target.casefold() for target in extract_wikilink_targets(body, ChunkType.NARRATIVE)
            }
            guidance["subject"] = subject_state_payload(app, state, max_state_notes)
            guidance["links_state_note"] = bool(targets & state_names)
            guidance["next_step"] = FOLD_GUIDANCE
    return guidance or None


def _folder_key(folder: str, fold_case: bool) -> str:
    normalized = folder.strip("/")
    return normalized.casefold() if fold_case else normalized
