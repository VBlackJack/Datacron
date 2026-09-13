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
"""Deterministic, read-only organization planner.

The planner compares a vault against the intent its sidecar declares and
reports the gap. It opens notes for reading only: it never moves, renames,
rewrites or creates anything, and it writes no cache, plan or checkpoint.

Determinism is a contract, not a happy accident. Two runs over an unchanged
vault produce byte-identical output, so the result is diffable and usable as a
CI signal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final, final

from datacron.core.config import (
    OrganizationConfig,
    OrganizationRule,
    Settings,
    VaultConfig,
    get_settings,
)
from datacron.core.frontmatter import FrontmatterError, extract_tags, parse
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.scope import NoteAdmissionError, SingleTenantVaultScope
from datacron.core.vault import SKIPPED_FOLDERS, NoteAdmissionPolicy
from datacron.organization.rules import matches_naming, resolve_rule
from datacron.organization.tags import evaluate_tag_policy

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "STATE_NOTE_NAMESPACE",
    "Deviation",
    "DeviationKind",
    "FreshnessEntry",
    "OrganizationConfigurationError",
    "OrganizationNoteSnapshot",
    "OrganizationPlan",
    "SkippedNote",
    "hash_organization_plan",
    "organization_plan_mapping",
    "plan_organization",
    "plan_organization_snapshot",
    "snapshot_note",
]

_NOTE_SUFFIX: Final[str] = ".md"
_BYTES_PER_KB: Final[int] = 1024
PLAN_SCHEMA_VERSION: Final[str] = "organization-plan-v2"
# The namespace whose tags mark a folder's state note. A state note is
# recognised by this tag, never by its stem.
STATE_NOTE_NAMESPACE: Final[str] = "kind"
_STATE_NOTE_PREFIX: Final[str] = STATE_NOTE_NAMESPACE + "/"
_STATE_NOTE_EXPECTED: Final[str] = "one note tagged kind/platform, kind/development or kind/mission"
# A split history note is named ``<subject>-history-<period>`` and never has
# to link back to the state note it was split from.
_HISTORY_STEM_MARKER: Final[str] = "-history-"
_FENCE_MARKER: Final[str] = "```"
# CommonMark admits up to three spaces of indentation before a code fence.
_FENCE_MAX_INDENT: Final[int] = 3
_FENCE_EXPECTED: Final[str] = "even number of fence lines"
_WIKILINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[\[([^\[\]]+)\]\]")
_WIKILINK_LABEL_SEPARATOR: Final[str] = "|"
_WIKILINK_ANCHOR_SEPARATOR: Final[str] = "#"
_LAST_VERIFIED_KEY: Final[str] = "last_verified"
_CRLF: Final[str] = "\r\n"
_CR: Final[str] = "\r"
_LF: Final[str] = "\n"
_TITLE_KEY: Final[str] = "title"
_ALIASES_KEY: Final[str] = "aliases"


def _filesystem_parts(path: PurePosixPath) -> tuple[str, ...]:
    """Normalize path case only for case-insensitive filesystem contracts."""
    if os.name == "nt":
        return tuple(part.casefold() for part in path.parts)
    return path.parts


class OrganizationConfigurationError(ValueError):
    """Raised before scanning when the declared organization is unsafe."""


class DeviationKind(StrEnum):
    """The nine gaps the planner reports, each measured against a declared intent.

    The first three measure a governed note against its rule. The next three
    exist only when the vault declares ``organization.tags``; without that
    block, an unmatched note is out of scope, never a deviation. The last three
    measure the subject folders and the code fences: ``NO_STATE_NOTE`` and
    ``UNLINKED`` exist only when the vault declares ``state_note_min_notes``
    and ``linking_since`` respectively, ``UNBALANCED_FENCE`` whenever rules
    are declared. The planner never invents a placement, a link or a note.
    """

    WRONG_FOLDER = "WRONG_FOLDER"
    NAMING = "NAMING"
    OVER_SIZE = "OVER_SIZE"
    UNGOVERNED = "UNGOVERNED"
    UNKNOWN_TAG = "UNKNOWN_TAG"
    TAG_CARDINALITY = "TAG_CARDINALITY"
    NO_STATE_NOTE = "NO_STATE_NOTE"
    UNLINKED = "UNLINKED"
    UNBALANCED_FENCE = "UNBALANCED_FENCE"


@final
@dataclass(frozen=True, slots=True)
class Deviation:
    """One measured gap between a note's location and its governing rule."""

    rel_path: str
    kind: DeviationKind
    tag: str
    detail: str
    expected: str | None = None

    @property
    def sort_key(self) -> tuple[str, str]:
        """Order by path then kind, never by filesystem traversal order."""
        return (self.rel_path, str(self.kind))


@final
@dataclass(frozen=True, slots=True)
class SkippedNote:
    """A note the planner could not read. Never fatal to the scan."""

    rel_path: str
    reason: str


@final
@dataclass(frozen=True, slots=True)
class OrganizationNoteSnapshot:
    """Content-free planner inputs for one already-admitted note.

    Every field is derived once at snapshot time and carries no prose: the
    link targets are normalized stems, the fence count is an integer, and the
    dates are rendered calendar days. Both snapshot builders (the filesystem
    scan and the manifest projection) go through :func:`snapshot_note`, so the
    projected report of a validated bundle equals the report measured after
    the bundle is applied.
    """

    rel_path: str
    size_bytes: int
    tags: tuple[str, ...]
    calendar_date: str | None
    skipped_reason: str | None = None
    title: str | None = None
    aliases: tuple[str, ...] = ()
    wikilink_targets: tuple[str, ...] = ()
    fence_lines: int = 0
    last_verified: str | None = None

    @property
    def fence_balanced(self) -> bool:
        """True when every opening fence line has a closing one."""
        return self.fence_lines % 2 == 0

    @property
    def is_state_note(self) -> bool:
        """True when the note carries a tag of the state-note namespace."""
        return any(tag.startswith(_STATE_NOTE_PREFIX) for tag in self.tags)

    @property
    def stem(self) -> str:
        """The filename without its suffix."""
        return PurePosixPath(self.rel_path).stem


@final
@dataclass(frozen=True, slots=True)
class FreshnessEntry:
    """One state note whose verification is missing or older than the threshold."""

    rel_path: str
    tag: str
    last_verified: str | None
    age_days: int | None


@final
@dataclass(frozen=True, slots=True)
class OrganizationPlan:
    """The full read-only result of one planning pass.

    ``freshness`` is informative and optional: it is computed only when a
    threshold is requested, never turns a clean plan into a non-empty one, and
    is not part of the manifest projection, which has no run date.
    """

    vault_root: str
    scope: str | None
    scanned: int
    governed: int
    unmatched: int
    deviations: tuple[Deviation, ...]
    skipped: tuple[SkippedNote, ...]
    freshness_days: int | None = None
    freshness: tuple[FreshnessEntry, ...] | None = None

    @property
    def has_deviations(self) -> bool:
        """True when at least one gap was measured."""
        return bool(self.deviations)

    def counts_by_kind(self) -> dict[str, int]:
        """Deviation totals per kind, in declared enum order for stable output."""
        return {
            kind.value: sum(1 for item in self.deviations if item.kind is kind)
            for kind in DeviationKind
        }


def organization_plan_mapping(plan: OrganizationPlan) -> dict[str, object]:
    """Return the stable public mapping used to bind organization mutations.

    The ``freshness`` field is present only when the plan carries one, so a
    plan computed without a threshold hashes exactly like a manifest projection.
    """
    mapping: dict[str, object] = {
        "schema": PLAN_SCHEMA_VERSION,
        "vault_root": plan.vault_root,
        "scope": plan.scope,
        "scanned": plan.scanned,
        "governed": plan.governed,
        "unmatched": plan.unmatched,
        "counts": plan.counts_by_kind(),
        "deviations": [
            {
                "rel_path": item.rel_path,
                "kind": str(item.kind),
                "tag": item.tag,
                "detail": item.detail,
                "expected": item.expected,
            }
            for item in plan.deviations
        ],
        "skipped": [{"rel_path": item.rel_path, "reason": item.reason} for item in plan.skipped],
    }
    if plan.freshness is not None:
        mapping["freshness"] = [
            {
                "rel_path": item.rel_path,
                "tag": item.tag,
                "last_verified": item.last_verified,
                "age_days": item.age_days,
            }
            for item in plan.freshness
        ]
    return mapping


def hash_organization_plan(plan: OrganizationPlan) -> str:
    """Hash the canonical UTF-8 JSON form of an organization plan."""
    payload = json.dumps(
        organization_plan_mapping(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class _OrganizationContext:
    """Validated canonical paths and admission policy for one planning pass."""

    vault_root: Path
    scope_root: Path
    scope_rel_path: str
    guard: SingleTenantVaultScope
    target_folders: Mapping[str, str]


def _prepare_context(
    vault_root: Path,
    config: VaultConfig,
    settings: Settings,
) -> _OrganizationContext:
    """Validate every configured path and build one canonical admission guard."""
    organization = config.organization
    if organization is None or not organization.rules or organization.scope is None:
        raise OrganizationConfigurationError("active organization rules require a scope")

    resolved_root = vault_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise OrganizationConfigurationError(f"vault root is not a directory: {resolved_root}")

    policy = NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | frozenset(config.excluded_folders),
        excluded_files=frozenset(config.excluded_files),
    )
    guard = SingleTenantVaultScope(resolved_root, settings, policy)
    try:
        scope_root = guard.authorize_rel_path(organization.scope, "read")
    except (PathConfinementError, RuntimeError) as exc:
        raise OrganizationConfigurationError(
            f"organization scope escapes the vault: {organization.scope!r}"
        ) from exc
    if not scope_root.exists():
        raise OrganizationConfigurationError(
            f"organization scope does not exist: {organization.scope!r}"
        )
    if not scope_root.is_dir():
        raise OrganizationConfigurationError(
            f"organization scope is not a directory: {organization.scope!r}"
        )

    targets: dict[str, str] = {}
    for rule in organization.rules:
        try:
            resolved_folder = guard.authorize_rel_path(rule.folder, "read")
        except PathConfinementError as exc:
            raise OrganizationConfigurationError(
                f"organization rule folder resolves outside the vault: {rule.folder!r}"
            ) from exc
        except RuntimeError as exc:
            raise OrganizationConfigurationError(
                f"organization rule folder cannot be resolved safely: {rule.folder!r}"
            ) from exc
        try:
            resolved_folder = assert_within_paths(resolved_folder, [scope_root], kind="read")
        except PathConfinementError as exc:
            raise OrganizationConfigurationError(
                "organization rule folder resolves outside organization scope "
                f"{organization.scope!r}: {rule.folder!r}"
            ) from exc
        except RuntimeError as exc:
            raise OrganizationConfigurationError(
                f"organization rule folder cannot be resolved safely: {rule.folder!r}"
            ) from exc
        if resolved_folder.exists() and not resolved_folder.is_dir():
            raise OrganizationConfigurationError(
                f"organization rule folder is not a directory: {rule.folder!r}"
            )
        targets[rule.tag] = resolved_folder.relative_to(resolved_root).as_posix()

    return _OrganizationContext(
        vault_root=resolved_root,
        scope_root=scope_root,
        scope_rel_path=scope_root.relative_to(resolved_root).as_posix(),
        guard=guard,
        target_folders=targets,
    )


def _directory_is_excluded(
    directory: Path,
    vault_root: Path,
    policy: NoteAdmissionPolicy,
) -> bool:
    """Apply the canonical admission policy before descending into a directory."""
    relative = directory.relative_to(vault_root)
    return any(
        part.startswith(".") or part.casefold() in policy.excluded_folders
        for part in relative.parts
    )


def _discover_note_paths(context: _OrganizationContext) -> list[Path]:
    """Discover Markdown candidates without traversing an unauthorized target."""
    discovered: list[Path] = []
    pending = [context.scope_root]
    visited: set[Path] = set()
    policy = context.guard.admission_policy

    while pending:
        directory = pending.pop()
        try:
            canonical_directory = context.guard.authorize_path(directory, "read")
            canonical_directory = assert_within_paths(
                canonical_directory,
                [context.scope_root],
                kind="read",
            )
        except (PathConfinementError, RuntimeError):
            continue
        if canonical_directory in visited or _directory_is_excluded(
            canonical_directory,
            context.vault_root,
            policy,
        ):
            continue
        visited.add(canonical_directory)

        for candidate in canonical_directory.iterdir():
            try:
                canonical_candidate = context.guard.authorize_path(candidate, "read")
                canonical_candidate = assert_within_paths(
                    canonical_candidate,
                    [context.scope_root],
                    kind="read",
                )
            except (OSError, PathConfinementError, RuntimeError):
                continue
            if canonical_candidate.is_dir():
                if (
                    candidate.name.startswith(".")
                    or candidate.name.casefold() in policy.excluded_folders
                    or _directory_is_excluded(
                        canonical_candidate,
                        context.vault_root,
                        policy,
                    )
                ):
                    continue
                pending.append(canonical_candidate)
                continue
            if candidate.name.casefold().endswith(_NOTE_SUFFIX):
                discovered.append(candidate)
    return discovered


def _authorize_note_paths(
    candidates: Iterable[Path],
    context: _OrganizationContext,
) -> list[Path]:
    """Authorize, confine and de-duplicate notes before any content access."""
    admitted: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            lexical_rel_path = candidate.relative_to(context.vault_root).as_posix()
            authorized = context.guard.authorize_note_rel_path(lexical_rel_path)
            authorized = assert_within_paths(authorized, [context.scope_root], kind="read")
        except (OSError, NoteAdmissionError, PathConfinementError, RuntimeError, ValueError):
            continue
        if authorized in seen:
            continue
        seen.add(authorized)
        admitted.append(authorized)
    return sorted(
        admitted,
        key=lambda item: item.relative_to(context.vault_root).as_posix(),
    )


def _iter_note_paths(
    vault_root: Path,
    config: VaultConfig,
    *,
    settings: Settings | None = None,
) -> list[Path]:
    """Collect admitted notes in stable order from the declared scope only."""
    organization = config.organization
    if organization is None or not organization.rules:
        return []
    context = _prepare_context(vault_root, config, settings or get_settings())
    return _authorize_note_paths(_discover_note_paths(context), context)


def _frontmatter_day(value: object) -> str | None:
    """Render a frontmatter date or datetime as ``YYYY-MM-DD``, else ``None``."""
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


def _frontmatter_calendar_date(metadata: Mapping[str, object]) -> str | None:
    """Return the first usable local calendar date from created then updated."""
    for key in ("created", "updated"):
        rendered = _frontmatter_day(metadata.get(key))
        if rendered is not None:
            return rendered
    return None


def _frontmatter_title(metadata: Mapping[str, object]) -> str | None:
    """Return the frontmatter title when it is a non-blank string."""
    value = metadata.get(_TITLE_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _frontmatter_aliases(metadata: Mapping[str, object]) -> tuple[str, ...]:
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


def _normalize_line_endings(body: str) -> str:
    """Fold CRLF and lone CR to LF so both snapshot builders scan the same lines.

    The filesystem scan reads notes with universal newlines while the manifest
    projection decodes raw payload bytes; without this fold a CRLF note would
    yield different fence counts or link targets on the two paths.
    """
    return body.replace(_CRLF, _LF).replace(_CR, _LF)


def _is_fence_line(line: str) -> bool:
    """Apply the shared opening-and-closing fence rule to one body line."""
    indent = len(line) - len(line.lstrip(" "))
    return indent <= _FENCE_MAX_INDENT and line[indent:].startswith(_FENCE_MARKER)


def _scan_body(body: str) -> tuple[int, tuple[str, ...]]:
    """Count fence lines and collect wikilink targets outside fenced blocks.

    A target is the part before ``|`` with its ``#anchor`` removed, stripped
    and casefolded; targets are deduplicated in first-seen order. A tilde
    fence is not a fence for this rule.
    """
    fence_lines = 0
    inside_fence = False
    targets: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        if _is_fence_line(line):
            fence_lines += 1
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        for match in _WIKILINK_PATTERN.finditer(line):
            reference = match.group(1).split(_WIKILINK_LABEL_SEPARATOR, 1)[0]
            target = reference.split(_WIKILINK_ANCHOR_SEPARATOR, 1)[0].strip().casefold()
            if not target or target in seen:
                continue
            seen.add(target)
            targets.append(target)
    return fence_lines, tuple(targets)


def snapshot_note(
    rel_path: str,
    size_bytes: int,
    metadata: Mapping[str, Any],
    body: str,
) -> OrganizationNoteSnapshot:
    """Derive the content-free planner inputs of one parsed note.

    This is the single derivation both the filesystem scan and the manifest
    projection use; nothing here keeps a reference to ``body``.
    """
    normalized = _normalize_line_endings(body)
    fence_lines, wikilink_targets = _scan_body(normalized)
    return OrganizationNoteSnapshot(
        rel_path=rel_path,
        size_bytes=size_bytes,
        tags=tuple(extract_tags(dict(metadata), normalized)),
        calendar_date=_frontmatter_calendar_date(metadata),
        title=_frontmatter_title(metadata),
        aliases=_frontmatter_aliases(metadata),
        wikilink_targets=wikilink_targets,
        fence_lines=fence_lines,
        last_verified=_frontmatter_day(metadata.get(_LAST_VERIFIED_KEY)),
    )


def _evaluate(
    rel_path: str,
    stem: str,
    folder: str,
    size_bytes: int,
    rule: OrganizationRule,
    expected_folder: str,
    calendar_date: str | None,
    fence_lines: int,
) -> list[Deviation]:
    """Measure one governed note against its rule."""
    found: list[Deviation] = []
    if folder != expected_folder:
        found.append(
            Deviation(
                rel_path=rel_path,
                kind=DeviationKind.WRONG_FOLDER,
                tag=rule.tag,
                detail=f"in {folder or '(vault root)'}",
                expected=expected_folder,
            )
        )
    if not matches_naming(stem, rule.naming, calendar_date=calendar_date):
        found.append(
            Deviation(
                rel_path=rel_path,
                kind=DeviationKind.NAMING,
                tag=rule.tag,
                detail=f"stem {stem!r}",
                expected=rule.naming,
            )
        )
    if rule.max_kb is not None and size_bytes > rule.max_kb * _BYTES_PER_KB:
        found.append(
            Deviation(
                rel_path=rel_path,
                kind=DeviationKind.OVER_SIZE,
                tag=rule.tag,
                detail=f"{size_bytes // _BYTES_PER_KB} KB",
                expected=f"{rule.max_kb} KB",
            )
        )
    if fence_lines % 2 != 0:
        found.append(
            Deviation(
                rel_path=rel_path,
                kind=DeviationKind.UNBALANCED_FENCE,
                tag=rule.tag,
                detail=f"{fence_lines} fence lines",
                expected=_FENCE_EXPECTED,
            )
        )
    return found


def _subject_rule_tags(organization: OrganizationConfig) -> frozenset[str]:
    """Return the rule tags that name a registered subject.

    A subject rule is one keyed by a tag of the declared subject namespace; a
    vault without a tag policy, or without a subject namespace, has none.
    """
    policy = organization.tags
    if policy is None or policy.subject_namespace is None:
        return frozenset()
    prefix = policy.subject_namespace.casefold() + "/"
    return frozenset(
        rule.tag for rule in organization.rules if rule.tag.casefold().startswith(prefix)
    )


def _state_note_names(note: OrganizationNoteSnapshot) -> set[str]:
    """Every casefolded name a wikilink may use to reach a state note."""
    names = {note.stem.casefold()}
    if note.title is not None:
        names.add(note.title.casefold())
    names.update(alias.casefold() for alias in note.aliases)
    return names


def _is_linking_candidate(note: OrganizationNoteSnapshot, since: str) -> bool:
    """A recent, non-state, non-history note owes a link to its state note."""
    return (
        not note.is_state_note
        and note.calendar_date is not None
        and note.calendar_date >= since
        and _HISTORY_STEM_MARKER not in note.stem
    )


def _evaluate_subject_folder(
    rule: OrganizationRule,
    folder: str,
    notes: list[OrganizationNoteSnapshot],
    organization: OrganizationConfig,
) -> list[Deviation]:
    """Measure one subject folder: its state note and the links to it."""
    found: list[Deviation] = []
    state_notes = [note for note in notes if note.is_state_note]
    threshold = organization.state_note_min_notes
    if threshold is not None and len(notes) >= threshold and not state_notes:
        found.append(
            Deviation(
                rel_path=folder,
                kind=DeviationKind.NO_STATE_NOTE,
                tag=rule.tag,
                detail=f"{len(notes)} notes, no {_STATE_NOTE_PREFIX} tag",
                expected=_STATE_NOTE_EXPECTED,
            )
        )
    linking_since = organization.linking_since
    if linking_since is None or not state_notes:
        return found
    since = linking_since.isoformat()
    accepted: set[str] = set()
    for state_note in state_notes:
        accepted.update(_state_note_names(state_note))
    primary_stem = state_notes[0].stem
    for note in notes:
        if not _is_linking_candidate(note, since):
            continue
        if any(target in accepted for target in note.wikilink_targets):
            continue
        found.append(
            Deviation(
                rel_path=note.rel_path,
                kind=DeviationKind.UNLINKED,
                tag=rule.tag,
                detail=f"no wikilink to {primary_stem}",
                expected=f"[[{primary_stem}]]",
            )
        )
    return found


def _utc_today() -> date:
    """The run date every freshness age is measured against."""
    return datetime.now(UTC).date()


def _freshness(
    state_notes: Iterable[tuple[OrganizationNoteSnapshot, str]],
    *,
    freshness_days: int,
    today: date,
) -> tuple[FreshnessEntry, ...]:
    """List state notes whose ``last_verified`` is missing or older than the threshold."""
    entries: list[FreshnessEntry] = []
    for note, tag in state_notes:
        if note.last_verified is None:
            entries.append(
                FreshnessEntry(rel_path=note.rel_path, tag=tag, last_verified=None, age_days=None)
            )
            continue
        age_days = (today - date.fromisoformat(note.last_verified)).days
        if age_days > freshness_days:
            entries.append(
                FreshnessEntry(
                    rel_path=note.rel_path,
                    tag=tag,
                    last_verified=note.last_verified,
                    age_days=age_days,
                )
            )
    return tuple(sorted(entries, key=lambda item: item.rel_path))


def _plan_snapshots(
    *,
    vault_root: Path,
    scope: str,
    organization: OrganizationConfig,
    target_folders: Mapping[str, str],
    notes: Iterable[OrganizationNoteSnapshot],
    freshness_days: int | None = None,
    today: date | None = None,
) -> OrganizationPlan:
    """Evaluate normalized note metadata without materializing note prose."""
    deviations: list[Deviation] = []
    skipped: list[SkippedNote] = []
    subject_rule_tags = _subject_rule_tags(organization)
    subject_folders: dict[str, list[OrganizationNoteSnapshot]] = {}
    state_notes: list[tuple[OrganizationNoteSnapshot, str]] = []
    scanned = 0
    governed = 0
    unmatched = 0
    for note in sorted(notes, key=lambda item: item.rel_path):
        relative = PurePosixPath(note.rel_path)
        scanned += 1
        if note.skipped_reason is not None:
            skipped.append(SkippedNote(rel_path=note.rel_path, reason=note.skipped_reason))
            continue
        rule = resolve_rule(note.tags, organization)
        if organization.tags is not None:
            for violation in evaluate_tag_policy(note.tags, organization):
                deviations.append(
                    Deviation(
                        rel_path=note.rel_path,
                        kind=DeviationKind(violation.kind.value),
                        tag=violation.tag or (rule.tag if rule is not None else ""),
                        detail=violation.detail,
                        expected=violation.expected,
                    )
                )
        if rule is None:
            unmatched += 1
            continue
        governed += 1
        parent = relative.parent.as_posix()
        folder = "" if parent == "." else parent
        expected_folder = target_folders[rule.tag]
        deviations.extend(
            _evaluate(
                rel_path=note.rel_path,
                stem=relative.stem,
                folder=folder,
                size_bytes=note.size_bytes,
                rule=rule,
                expected_folder=expected_folder,
                calendar_date=note.calendar_date,
                fence_lines=note.fence_lines,
            )
        )
        if note.is_state_note:
            state_notes.append((note, rule.tag))
        if rule.tag in subject_rule_tags and folder == expected_folder:
            subject_folders.setdefault(rule.tag, []).append(note)
    for rule in organization.rules:
        folder_notes = subject_folders.get(rule.tag)
        if folder_notes:
            deviations.extend(
                _evaluate_subject_folder(
                    rule,
                    target_folders[rule.tag],
                    folder_notes,
                    organization,
                )
            )
    freshness: tuple[FreshnessEntry, ...] | None = None
    if freshness_days is not None:
        freshness = _freshness(
            state_notes,
            freshness_days=freshness_days,
            today=today if today is not None else _utc_today(),
        )
    return OrganizationPlan(
        vault_root=str(vault_root.expanduser().resolve()),
        scope=scope,
        scanned=scanned,
        governed=governed,
        unmatched=unmatched,
        deviations=tuple(sorted(deviations, key=lambda item: item.sort_key)),
        skipped=tuple(sorted(skipped, key=lambda item: item.rel_path)),
        freshness_days=freshness_days,
        freshness=freshness,
    )


def _snapshot_target_folders(config: VaultConfig) -> tuple[str, OrganizationConfig, dict[str, str]]:
    """Validate rule folders lexically for an in-memory planner projection."""
    organization = config.organization
    if organization is None or not organization.rules or organization.scope is None:
        raise OrganizationConfigurationError("active organization rules require a scope")
    scope = PurePosixPath(organization.scope)
    scope_key = _filesystem_parts(scope)
    targets: dict[str, str] = {}
    for rule in organization.rules:
        folder = PurePosixPath(rule.folder)
        folder_key = _filesystem_parts(folder)
        if folder_key[: len(scope_key)] != scope_key:
            raise OrganizationConfigurationError(
                "organization rule folder resolves outside organization scope "
                f"{organization.scope!r}: {rule.folder!r}"
            )
        targets[rule.tag] = folder.as_posix()
    return scope.as_posix(), organization, targets


def plan_organization_snapshot(
    vault_root: Path,
    config: VaultConfig,
    notes: Iterable[OrganizationNoteSnapshot],
) -> OrganizationPlan:
    """Plan from content-free admitted-note metadata without filesystem copies."""
    organization = config.organization
    if organization is None or not organization.rules:
        return OrganizationPlan(
            vault_root=str(vault_root.expanduser().resolve()),
            scope=None,
            scanned=0,
            governed=0,
            unmatched=0,
            deviations=(),
            skipped=(),
        )
    scope, active, targets = _snapshot_target_folders(config)
    scope_key = _filesystem_parts(PurePosixPath(scope))
    materialized = tuple(notes)
    for note in materialized:
        relative = PurePosixPath(note.rel_path)
        relative_key = _filesystem_parts(relative)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.casefold() != _NOTE_SUFFIX
            or relative_key[: len(scope_key)] != scope_key
            or len(relative_key) <= len(scope_key)
        ):
            raise OrganizationConfigurationError(
                f"snapshot note is outside organization scope: {note.rel_path!r}"
            )
        if note.size_bytes < 0:
            raise OrganizationConfigurationError(
                f"snapshot note has negative size: {note.rel_path!r}"
            )
    return _plan_snapshots(
        vault_root=vault_root,
        scope=scope,
        organization=active,
        target_folders=targets,
        notes=materialized,
    )


def plan_organization(
    vault_root: Path,
    config: VaultConfig,
    *,
    settings: Settings | None = None,
    freshness_days: int | None = None,
    today: date | None = None,
) -> OrganizationPlan:
    """Measure the gap between a vault and the organization intent it declares.

    A vault whose sidecar carries no ``organization`` block yields an empty plan
    without reading a single note, so the feature stays inert for every vault
    published before it existed.

    ``freshness_days`` adds the informative freshness list, measured against
    ``today`` (the UTC calendar date when omitted). A vault without rules still
    answers the option, with an empty list, so the JSON field is present
    whenever the caller asked for it.
    """
    organization = config.organization
    if organization is None or not organization.rules:
        return OrganizationPlan(
            vault_root=str(vault_root.expanduser().resolve()),
            scope=None,
            scanned=0,
            governed=0,
            unmatched=0,
            deviations=(),
            skipped=(),
            freshness_days=freshness_days,
            freshness=None if freshness_days is None else (),
        )

    context = _prepare_context(vault_root, config, settings or get_settings())
    snapshots: list[OrganizationNoteSnapshot] = []
    candidates = _discover_note_paths(context)
    for path in _authorize_note_paths(candidates, context):
        relative = path.relative_to(context.vault_root)
        rel_path = relative.as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
            size_bytes = path.stat().st_size
        except (OSError, UnicodeDecodeError) as exc:
            snapshots.append(
                OrganizationNoteSnapshot(
                    rel_path=rel_path,
                    size_bytes=0,
                    tags=(),
                    calendar_date=None,
                    skipped_reason=type(exc).__name__,
                )
            )
            continue
        try:
            metadata, body = parse(raw)
        except (FrontmatterError, ValueError) as exc:
            snapshots.append(
                OrganizationNoteSnapshot(
                    rel_path=rel_path,
                    size_bytes=size_bytes,
                    tags=(),
                    calendar_date=None,
                    skipped_reason=str(exc),
                )
            )
            continue
        snapshots.append(snapshot_note(rel_path, size_bytes, metadata, body))
    return _plan_snapshots(
        vault_root=context.vault_root,
        scope=context.scope_rel_path,
        organization=organization,
        target_folders=context.target_folders,
        notes=snapshots,
        freshness_days=freshness_days,
        today=today,
    )
