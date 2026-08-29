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

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, final

from datacron.core.config import (
    SIDECAR_DIR_NAME,
    OrganizationRule,
    VaultConfig,
)
from datacron.core.frontmatter import FrontmatterError, extract_tags, parse
from datacron.organization.rules import matches_naming, resolve_rule

__all__ = [
    "Deviation",
    "DeviationKind",
    "OrganizationPlan",
    "SkippedNote",
    "plan_organization",
]

_NOTE_SUFFIX: Final[str] = ".md"
_BYTES_PER_KB: Final[int] = 1024


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


def _is_excluded(rel_parts: tuple[str, ...], excluded_folders: frozenset[str]) -> bool:
    """Skip the sidecar, dot-directories and vault-declared exclusions."""
    return any(
        part == SIDECAR_DIR_NAME or part.startswith(".") or part in excluded_folders
        for part in rel_parts
    )


def _iter_note_paths(vault_root: Path, config: VaultConfig) -> list[Path]:
    """Collect candidate notes in a stable, filesystem-independent order."""
    excluded_folders = frozenset(config.excluded_folders)
    excluded_files = frozenset(config.excluded_files)
    found: list[Path] = []
    for path in vault_root.rglob(f"*{_NOTE_SUFFIX}"):
        if not path.is_file():
            continue
        relative = path.relative_to(vault_root)
        if _is_excluded(relative.parts[:-1], excluded_folders):
            continue
        if path.name in excluded_files:
            continue
        found.append(path)
    # Sorting here is what makes the whole pass deterministic; rglob order is
    # an artefact of the filesystem and differs between machines and runs.
    return sorted(found, key=lambda item: item.relative_to(vault_root).as_posix())


def _evaluate(
    rel_path: str,
    stem: str,
    folder: str,
    size_bytes: int,
    rule: OrganizationRule,
) -> list[Deviation]:
    """Measure one governed note against its rule."""
    found: list[Deviation] = []
    if folder != rule.folder:
        found.append(
            Deviation(
                rel_path=rel_path,
                kind=DeviationKind.WRONG_FOLDER,
                tag=rule.tag,
                detail=f"in {folder or '(vault root)'}",
                expected=rule.folder,
            )
        )
    if not matches_naming(stem, rule.naming):
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


def plan_organization(vault_root: Path, config: VaultConfig) -> OrganizationPlan:
    """Measure the gap between a vault and the organization intent it declares.

    A vault whose sidecar carries no ``organization`` block yields an empty plan
    without reading a single note, so the feature stays inert for every vault
    published before it existed.
    """
    organization = config.organization
    if organization is None or not organization.rules:
        return OrganizationPlan(
            vault_root=str(vault_root),
            scanned=0,
            governed=0,
            unmatched=0,
            deviations=(),
            skipped=(),
        )

    deviations: list[Deviation] = []
    skipped: list[SkippedNote] = []
    scanned = 0
    governed = 0
    unmatched = 0

    for path in _iter_note_paths(vault_root, config):
        relative = path.relative_to(vault_root)
        rel_path = relative.as_posix()
        scanned += 1
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(SkippedNote(rel_path=rel_path, reason=type(exc).__name__))
            continue
        try:
            metadata, body = parse(raw)
        except FrontmatterError as exc:
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
                size_bytes=path.stat().st_size,
                rule=rule,
            )
        )

    return OrganizationPlan(
        vault_root=str(vault_root),
        scanned=scanned,
        governed=governed,
        unmatched=unmatched,
        deviations=tuple(sorted(deviations, key=lambda item: item.sort_key)),
        skipped=tuple(sorted(skipped, key=lambda item: item.rel_path)),
    )
