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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, final

from datacron.core.config import (
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

__all__ = [
    "Deviation",
    "DeviationKind",
    "OrganizationConfigurationError",
    "OrganizationPlan",
    "SkippedNote",
    "plan_organization",
]

_NOTE_SUFFIX: Final[str] = ".md"
_BYTES_PER_KB: Final[int] = 1024


class OrganizationConfigurationError(ValueError):
    """Raised before scanning when the declared organization is unsafe."""


class DeviationKind(StrEnum):
    """The three gaps this lot reports. Nothing else is a deviation."""

    WRONG_FOLDER = "WRONG_FOLDER"
    NAMING = "NAMING"
    OVER_SIZE = "OVER_SIZE"


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
class OrganizationPlan:
    """The full read-only result of one planning pass."""

    vault_root: str
    scope: str | None
    scanned: int
    governed: int
    unmatched: int
    deviations: tuple[Deviation, ...]
    skipped: tuple[SkippedNote, ...]

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
            resolved_folder = assert_within_paths(resolved_folder, [scope_root], kind="read")
        except (PathConfinementError, RuntimeError) as exc:
            raise OrganizationConfigurationError(
                f"organization rule folder escapes scope {organization.scope!r}: {rule.folder!r}"
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


def _frontmatter_calendar_date(metadata: Mapping[str, object]) -> str | None:
    """Return the first usable local calendar date from created then updated."""
    for key in ("created", "updated"):
        value = metadata.get(key)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        try:
            return datetime.fromisoformat(candidate).date().isoformat()
        except ValueError:
            try:
                return date.fromisoformat(candidate).isoformat()
            except ValueError:
                continue
    return None


def _evaluate(
    rel_path: str,
    stem: str,
    folder: str,
    size_bytes: int,
    rule: OrganizationRule,
    expected_folder: str,
    calendar_date: str | None,
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
    return found


def plan_organization(
    vault_root: Path,
    config: VaultConfig,
    *,
    settings: Settings | None = None,
) -> OrganizationPlan:
    """Measure the gap between a vault and the organization intent it declares.

    A vault whose sidecar carries no ``organization`` block yields an empty plan
    without reading a single note, so the feature stays inert for every vault
    published before it existed.
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
        )

    context = _prepare_context(vault_root, config, settings or get_settings())
    deviations: list[Deviation] = []
    skipped: list[SkippedNote] = []
    scanned = 0
    governed = 0
    unmatched = 0

    candidates = _discover_note_paths(context)
    for path in _authorize_note_paths(candidates, context):
        relative = path.relative_to(context.vault_root)
        rel_path = relative.as_posix()
        scanned += 1
        try:
            raw = path.read_text(encoding="utf-8")
            size_bytes = path.stat().st_size
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(SkippedNote(rel_path=rel_path, reason=type(exc).__name__))
            continue
        try:
            metadata, body = parse(raw)
        except (FrontmatterError, ValueError) as exc:
            skipped.append(SkippedNote(rel_path=rel_path, reason=str(exc)))
            continue

        rule = resolve_rule(extract_tags(metadata, body), organization)
        if rule is None:
            # Out of scope, not a deviation. Never invent a placement here.
            unmatched += 1
            continue
        governed += 1
        deviations.extend(
            _evaluate(
                rel_path=rel_path,
                stem=relative.stem,
                folder="" if relative.parent == Path() else relative.parent.as_posix(),
                size_bytes=size_bytes,
                rule=rule,
                expected_folder=context.target_folders[rule.tag],
                calendar_date=_frontmatter_calendar_date(metadata),
            )
        )

    return OrganizationPlan(
        vault_root=str(context.vault_root),
        scope=context.scope_rel_path,
        scanned=scanned,
        governed=governed,
        unmatched=unmatched,
        deviations=tuple(sorted(deviations, key=lambda item: item.sort_key)),
        skipped=tuple(sorted(skipped, key=lambda item: item.rel_path)),
    )
