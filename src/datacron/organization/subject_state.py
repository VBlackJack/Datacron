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

"""A subject's state note and how far the subject's other notes have moved past it.

A subject is a registered tag of the vault's tag policy; its state note is the
note that carries that tag and a tag of the state-note namespace. The planner
measures the same objects over a folder; this module measures them over every
indexed note that carries the subject tag, wherever it is filed, because a note
written in the wrong folder is exactly the one a folder scan cannot see.

Everything here is pure: callers supply the tags and the parsed frontmatter, and
nothing reads the vault, the index or the clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Final, final

from datacron.core.config import DEFAULT_STATE_NOTE_NAMESPACE, OrganizationConfig

__all__ = [
    "HISTORY_STEM_MARKER",
    "LAST_VERIFIED_KEY",
    "STATE_NOTE_PREFIX",
    "StateNoteSummary",
    "SubjectNote",
    "SubjectState",
    "frontmatter_aliases",
    "frontmatter_calendar_date",
    "frontmatter_day",
    "frontmatter_title",
    "has_state_note_tag",
    "is_history_stem",
    "note_subject_tags",
    "resolve_subject_tag",
    "summarize_subject",
]

STATE_NOTE_PREFIX: Final[str] = DEFAULT_STATE_NOTE_NAMESPACE + "/"
# A split history note is named ``<subject>-history-<period>``; it records the
# past of a state note and never counts as news the state note has missed.
HISTORY_STEM_MARKER: Final[str] = "-history-"
LAST_VERIFIED_KEY: Final[str] = "last_verified"
_TITLE_KEY: Final[str] = "title"
_ALIASES_KEY: Final[str] = "aliases"
_CALENDAR_DATE_KEYS: Final[tuple[str, ...]] = ("created", "updated")
_TAG_PREFIX: Final[str] = "#"
_NAMESPACE_SEPARATOR: Final[str] = "/"


def frontmatter_day(value: object) -> str | None:
    """Render a frontmatter date or datetime as ``YYYY-MM-DD``.

    Args:
        value: A parsed YAML value, or its ISO string after a JSON round trip.

    Returns:
        The calendar day, or ``None`` when the value is not a usable date.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return None


def frontmatter_calendar_date(metadata: Mapping[str, object]) -> str | None:
    """Return the first usable local calendar date from created then updated."""
    for key in _CALENDAR_DATE_KEYS:
        rendered = frontmatter_day(metadata.get(key))
        if rendered is not None:
            return rendered
    return None


def frontmatter_title(metadata: Mapping[str, object]) -> str | None:
    """Return the frontmatter title when it is a non-blank string."""
    value = metadata.get(_TITLE_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def frontmatter_aliases(metadata: Mapping[str, object]) -> tuple[str, ...]:
    """Return the frontmatter aliases that are already strings, blanks dropped.

    A number or a mapping in the list is not an alias: coercing it to text
    would let ``[[123]]`` satisfy a link the author never declared.
    """
    value = metadata.get(_ALIASES_KEY)
    candidates: tuple[object, ...]
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)):
        candidates = tuple(value)
    else:
        return ()
    return tuple(item.strip() for item in candidates if isinstance(item, str) and item.strip())


def has_state_note_tag(tags: Iterable[str]) -> bool:
    """Return whether any tag belongs to the state-note namespace."""
    return any(tag.startswith(STATE_NOTE_PREFIX) for tag in tags)


def is_history_stem(stem: str) -> bool:
    """Return whether a stem names a split history note."""
    return HISTORY_STEM_MARKER in stem


def _normalize_subject_key(value: str) -> str:
    return value.strip().lstrip(_TAG_PREFIX).strip().casefold()


def resolve_subject_tag(subject: str, organization: OrganizationConfig | None) -> str | None:
    """Map a free-text subject to exactly one registered subject tag.

    The subject matches a registered tag, the tag's last segment (``heimdall``
    for ``project/heimdall``) or one of its declared aliases, case-insensitively.
    A subject that matches several registered subjects resolves to none: picking
    one would pin the wrong state note with the authority of an exact match.

    Args:
        subject: The subject a caller asked about.
        organization: The vault's organization block, if any.

    Returns:
        The canonical subject tag, or ``None`` when nothing or several match.
    """
    if organization is None or organization.tags is None:
        return None
    key = _normalize_subject_key(subject)
    if not key:
        return None
    matches: set[str] = set()
    for registered in organization.tags.subjects:
        tag = registered.tag.casefold()
        leaf = tag.rsplit(_NAMESPACE_SEPARATOR, 1)[-1]
        aliases = {alias.casefold() for alias in registered.aliases}
        if key in {tag, leaf} or key in aliases:
            matches.add(registered.tag)
    return next(iter(matches)) if len(matches) == 1 else None


def note_subject_tags(
    tags: Iterable[str], organization: OrganizationConfig | None
) -> tuple[str, ...]:
    """Return the registered subject tags a note carries, in the note's order."""
    if organization is None or organization.tags is None:
        return ()
    registered = {subject.tag.casefold(): subject.tag for subject in organization.tags.subjects}
    found = dict.fromkeys(
        registered[tag.casefold()] for tag in tags if tag.casefold() in registered
    )
    return tuple(found)


@final
@dataclass(frozen=True, slots=True)
class SubjectNote:
    """Content-free facts about one note that carries a subject tag."""

    rel_path: str
    tags: tuple[str, ...]
    calendar_date: str | None
    last_verified: str | None
    title: str | None = None
    aliases: tuple[str, ...] = ()

    @classmethod
    def from_metadata(
        cls, rel_path: str, tags: Iterable[str], metadata: Mapping[str, object]
    ) -> SubjectNote:
        """Derive the facts of one note from its effective tags and frontmatter."""
        return cls(
            rel_path=rel_path,
            tags=tuple(tags),
            calendar_date=frontmatter_calendar_date(metadata),
            last_verified=frontmatter_day(metadata.get(LAST_VERIFIED_KEY)),
            title=frontmatter_title(metadata),
            aliases=frontmatter_aliases(metadata),
        )

    @property
    def is_state_note(self) -> bool:
        """True when the note carries a tag of the state-note namespace."""
        return has_state_note_tag(self.tags)

    @property
    def stem(self) -> str:
        """The filename without its suffix."""
        return PurePosixPath(self.rel_path).stem

    def link_names(self) -> frozenset[str]:
        """Every casefolded name a wikilink may use to reach this note."""
        names = {self.stem.casefold()}
        if self.title is not None:
            names.add(self.title.casefold())
        names.update(alias.casefold() for alias in self.aliases)
        return frozenset(names)


@final
@dataclass(frozen=True, slots=True)
class StateNoteSummary:
    """One state note and the subject notes dated after its last verification.

    ``newer_notes`` is ``None`` when the state note has no ``last_verified``:
    without a verification date there is no reference point, and ``updated``
    proves nothing about what the note knows.
    """

    rel_path: str
    last_verified: str | None
    newer_notes: int | None


@final
@dataclass(frozen=True, slots=True)
class SubjectState:
    """The state notes of one subject and the size of the subject."""

    subject_tag: str
    subject_notes: int
    state_notes: tuple[StateNoteSummary, ...]


def summarize_subject(subject_tag: str, notes: Iterable[SubjectNote]) -> SubjectState:
    """Measure each state note of a subject against the subject's other notes.

    A note counts as newer when it is neither a state note nor a split history
    note and its calendar date (``created``, then ``updated``) is strictly after
    the state note's ``last_verified``. That is the same population the
    planner's ``UNLINKED`` rule asks to link, measured in every folder.

    Args:
        subject_tag: The canonical subject tag the notes were selected by.
        notes: Every note carrying that tag, in any order.

    Returns:
        The subject size and one summary per state note, ordered by path.
    """
    ordered = sorted(notes, key=lambda item: item.rel_path)
    evidence_dates = [
        note.calendar_date
        for note in ordered
        if not note.is_state_note and not is_history_stem(note.stem)
    ]
    summaries = tuple(
        StateNoteSummary(
            rel_path=note.rel_path,
            last_verified=note.last_verified,
            newer_notes=_count_after(evidence_dates, note.last_verified),
        )
        for note in ordered
        if note.is_state_note
    )
    return SubjectState(
        subject_tag=subject_tag,
        subject_notes=len(ordered),
        state_notes=summaries,
    )


def _count_after(dates: Iterable[str | None], reference: str | None) -> int | None:
    if reference is None:
        return None
    return sum(1 for day in dates if day is not None and day > reference)
