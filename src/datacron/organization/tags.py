#
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
"""Tag policy evaluation for a vault that declares ``organization.tags``.

The policy is entirely vault-declared: which namespace carries the placement
tag, which placement tags may double as markers, which namespace names a
subject, the closed subject registry with its aliases, and which other
namespaces are admitted. Datacron ships no taxonomy. A vault without the block
is unaffected: :func:`evaluate_tag_policy` returns nothing.

One evaluation yields zero or more violations. The same function serves the
planner (reported as deviations), ``create_note_ai`` (refused creation) and
the organization manifest (refused validation). Sharing the evaluation keeps
the tag judgement identical; the surfaces still differ in what they judge
(the planner also measures placement, naming and size) and in how they admit
a path, which :func:`path_within_scope` aligns for the writer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, final

from datacron.core.config import OrganizationConfig, OrganizationTagPolicy

__all__ = [
    "TAG_POLICY_ERROR_CODE",
    "TagPolicyError",
    "TagViolation",
    "TagViolationKind",
    "evaluate_tag_policy",
    "format_violations",
    "path_within_scope",
]

TAG_POLICY_ERROR_CODE: Final[str] = "tag_policy_violation"


class TagViolationKind(StrEnum):
    """The three ways a note's tags can break the declared policy."""

    UNGOVERNED = "UNGOVERNED"
    UNKNOWN_TAG = "UNKNOWN_TAG"
    TAG_CARDINALITY = "TAG_CARDINALITY"


@final
@dataclass(frozen=True, slots=True)
class TagViolation:
    """One measured breach of the declared tag policy."""

    kind: TagViolationKind
    tag: str
    detail: str
    expected: str | None = None


class TagPolicyError(ValueError):
    """Raised by writers when a note's tags break the declared policy."""

    code: Final[str] = TAG_POLICY_ERROR_CODE

    def __init__(self, rel_path: str, violations: tuple[TagViolation, ...]) -> None:
        self.rel_path = rel_path
        self.violations = violations
        super().__init__(f"{rel_path}: {format_violations(violations)}")


def _canonical_parts(rel_path: str) -> tuple[str, ...]:
    """Collapse separators and ``.`` segments; case follows the filesystem contract."""
    parts = tuple(
        part
        for part in PurePosixPath(rel_path.replace("\\", "/")).parts
        if part not in {"", ".", "/"}
    )
    if os.name == "nt":
        return tuple(part.casefold() for part in parts)
    return parts


def path_within_scope(rel_path: str, scope: str) -> bool:
    """True when ``rel_path`` lies strictly inside ``scope``, both vault-relative.

    ``./_memory/x.md`` and ``_memory//x.md`` are the same path as
    ``_memory/x.md``; a lexical prefix test would let the first two escape a
    guard that the planner and the manifest apply after canonicalization. A
    ``..`` segment is not resolved here and makes the answer ``False``: callers
    that accept user paths must first normalize them against the vault root
    (the writer guard does), because a ``False`` from this function means
    "not judged", never "refused".
    """
    path_parts = _canonical_parts(rel_path)
    scope_parts = _canonical_parts(scope)
    if ".." in path_parts or ".." in scope_parts or not scope_parts:
        return False
    return len(path_parts) > len(scope_parts) and path_parts[: len(scope_parts)] == scope_parts


@final
@dataclass(frozen=True, slots=True)
class _Vocabulary:
    """The declared policy, lowercased once for the whole evaluation."""

    rule_tags: tuple[str, ...]
    markers: frozenset[str]
    placement_namespace: str
    subject_namespace: str | None
    subject_tags: frozenset[str]
    alias_owner: dict[str, str]
    allowed_namespaces: frozenset[str]
    exempt: frozenset[str]

    @classmethod
    def build(cls, organization: OrganizationConfig, policy: OrganizationTagPolicy) -> _Vocabulary:
        subject_namespace = (
            policy.subject_namespace.lower() if policy.subject_namespace is not None else None
        )
        return cls(
            rule_tags=tuple(rule.tag.lower() for rule in organization.rules),
            markers=frozenset(marker.lower() for marker in policy.markers),
            placement_namespace=policy.placement_namespace.lower(),
            subject_namespace=subject_namespace,
            subject_tags=frozenset(subject.tag.lower() for subject in policy.subjects),
            alias_owner={
                alias.lower(): subject.tag.lower()
                for subject in policy.subjects
                for alias in subject.aliases
            },
            allowed_namespaces=frozenset(name.lower() for name in policy.allowed_namespaces),
            exempt=frozenset(tag.lower() for tag in policy.subject_exempt_tags),
        )

    @property
    def declared_namespaces(self) -> str:
        names = {self.placement_namespace} | self.allowed_namespaces
        if self.subject_namespace is not None:
            names.add(self.subject_namespace)
        return ", ".join(sorted(names))


def _normalize(tags: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in tags:
        tag = raw.lstrip("#").strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


def evaluate_tag_policy(
    tags: Iterable[str],
    organization: OrganizationConfig,
) -> tuple[TagViolation, ...]:
    """Return every violation of ``organization.tags`` for one note's effective tags.

    Tags are compared lowercased, the way the planner aggregates them (the
    configuration requires lowercase rule tags whenever a policy is declared,
    so the rule resolver and this evaluation agree). A vault without a
    declared policy yields an empty tuple.
    """
    policy = organization.tags
    if policy is None:
        return ()
    vocabulary = _Vocabulary.build(organization, policy)
    normalized = _normalize(tags)
    violations = list(_placement_violations(normalized, vocabulary))
    subjects_present: list[str] = []
    for tag in normalized:
        violation = _classify(tag, vocabulary, subjects_present)
        if violation is not None:
            violations.append(violation)
    placement = {tag for tag in normalized if tag in vocabulary.rule_tags}
    if len(subjects_present) > 1 and not (placement & vocabulary.exempt):
        violations.append(
            TagViolation(
                kind=TagViolationKind.TAG_CARDINALITY,
                tag=", ".join(subjects_present),
                detail="several subject tags on one note",
                expected="one owning subject; relate the others by wikilink",
            )
        )
    return tuple(violations)


def _placement_violations(tags: list[str], vocabulary: _Vocabulary) -> Iterable[TagViolation]:
    """Allowed placement sets: one rule tag, one marker, or one rule tag plus one marker."""
    placement = [tag for tag in tags if tag in vocabulary.rule_tags]
    if not placement:
        yield TagViolation(
            kind=TagViolationKind.UNGOVERNED,
            tag="",
            detail="no placement tag among the declared rules",
            expected=f"one of: {', '.join(vocabulary.rule_tags)}",
        )
        return
    non_marker = [tag for tag in placement if tag not in vocabulary.markers]
    markers = [tag for tag in placement if tag in vocabulary.markers]
    if len(non_marker) > 1 or len(markers) > 1:
        yield TagViolation(
            kind=TagViolationKind.TAG_CARDINALITY,
            tag=", ".join(placement),
            detail="several placement tags on one note",
            expected="exactly one placement tag, plus one declared marker at most",
        )


def _classify(
    tag: str,
    vocabulary: _Vocabulary,
    subjects_present: list[str],
) -> TagViolation | None:
    """Judge one tag; record it in ``subjects_present`` when it is a registered subject."""
    owner = vocabulary.alias_owner.get(tag)
    if owner is not None:
        # An alias is refused whatever its spelling: bare, in the subject
        # namespace, or in a namespace the vault otherwise admits.
        return TagViolation(
            kind=TagViolationKind.UNKNOWN_TAG,
            tag=tag,
            detail=f"alias of the registered subject {owner}",
            expected=owner,
        )
    if "/" not in tag:
        return None
    return _classify_namespaced(tag, tag.split("/", 1)[0], vocabulary, subjects_present)


def _classify_namespaced(
    tag: str,
    namespace: str,
    vocabulary: _Vocabulary,
    subjects_present: list[str],
) -> TagViolation | None:
    if namespace == vocabulary.placement_namespace:
        return _placement_tag_violation(tag, vocabulary)
    if namespace == vocabulary.subject_namespace:
        if tag in vocabulary.subject_tags:
            subjects_present.append(tag)
            return None
        return _unknown(tag, "not in the declared subject registry", "a registered subject tag")
    if namespace in vocabulary.allowed_namespaces:
        return None
    return _unknown(
        tag,
        f"namespace {namespace!r} is not declared",
        f"one of: {vocabulary.declared_namespaces}",
    )


def _placement_tag_violation(tag: str, vocabulary: _Vocabulary) -> TagViolation | None:
    if tag in vocabulary.rule_tags:
        return None
    return _unknown(
        tag, "not a declared placement tag", f"one of: {', '.join(vocabulary.rule_tags)}"
    )


def _unknown(tag: str, detail: str, expected: str) -> TagViolation:
    return TagViolation(
        kind=TagViolationKind.UNKNOWN_TAG, tag=tag, detail=detail, expected=expected
    )


def format_violations(violations: Iterable[TagViolation]) -> str:
    """Render violations as one line, stable for error messages and reports."""
    parts = []
    for item in violations:
        where = f" [{item.tag}]" if item.tag else ""
        hint = f" (expected {item.expected})" if item.expected else ""
        parts.append(f"{item.kind.value}{where}: {item.detail}{hint}")
    return "; ".join(parts)
