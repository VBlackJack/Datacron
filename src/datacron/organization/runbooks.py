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
"""Where a runbook capture goes: subject to runbook, kind to role, role to file.

Every step refuses rather than guesses, with the candidates the caller can pick
from, the same contract as ``remember``. Nothing here touches the filesystem: the
caller lists the runbook folders and file stems that exist and passes them in, so
the routing is decided on data the caller has already confined and admitted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, final

from datacron.core.config import (
    RUNBOOK_MARKDOWN_SUFFIX,
    RUNBOOK_NAMING_SERVICE,
    RUNBOOK_NAMING_SLUG,
    OrganizationConfig,
    RunbookRoot,
    RunbooksConfig,
    RunbookTemplate,
)
from datacron.organization.subject_state import matching_subject_tags

__all__ = [
    "RUNBOOK_MAX_CANDIDATES",
    "RunbookFile",
    "RunbookLocation",
    "RunbookRoutingError",
    "quickref_rel_path",
    "resolve_role",
    "resolve_role_file",
    "resolve_runbook",
]

RUNBOOK_MAX_CANDIDATES: Final[int] = 10
_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_NAMESPACE_SEPARATOR: Final[str] = "/"


class RunbookRoutingError(ValueError):
    """A capture whose destination cannot be decided without a guess."""


@final
@dataclass(frozen=True, slots=True)
class RunbookLocation:
    """One runbook folder, existing or to be created from its template."""

    root: RunbookRoot
    template: RunbookTemplate
    directory: str
    slug: str
    exists: bool


@final
@dataclass(frozen=True, slots=True)
class RunbookFile:
    """The file that plays a role in a runbook, existing or to be created."""

    role: str
    rel_path: str
    exists: bool


def _candidates(values: Sequence[str]) -> str:
    shown = list(values[:RUNBOOK_MAX_CANDIDATES])
    omitted = len(values) - len(shown)
    suffix = f" (and {omitted} more)" if omitted else ""
    return (", ".join(shown) or "none") + suffix


def _fold(value: str, *, fold_case: bool) -> str:
    return value.casefold() if fold_case else value


def _directory_name(root: RunbookRoot, slug: str) -> str:
    return root.directory_naming.replace(f"{{{RUNBOOK_NAMING_SLUG}}}", slug)


def _location(
    runbooks: RunbooksConfig, root: RunbookRoot, name: str, slug: str, *, exists: bool
) -> RunbookLocation:
    return RunbookLocation(
        root=root,
        template=runbooks.templates[root.template],
        directory=f"{root.folder}/{name}",
        slug=slug,
        exists=exists,
    )


def _existing_runbooks(
    runbooks: RunbooksConfig, children: Mapping[str, Sequence[str]], *, fold_case: bool
) -> list[tuple[RunbookRoot, str, str]]:
    """Return (root, folder name, slug) for every child folder named like a runbook."""
    found: list[tuple[RunbookRoot, str, str]] = []
    for root in runbooks.roots:
        for name in children.get(root.folder, ()):
            slug = root.directory_slug(name, fold_case=fold_case)
            if slug is not None:
                found.append((root, name, slug))
    return found


def _by_path(
    reference: str,
    runbooks: RunbooksConfig,
    existing: list[tuple[RunbookRoot, str, str]],
    *,
    fold_case: bool,
) -> RunbookLocation:
    path = reference.replace("\\", _NAMESPACE_SEPARATOR).strip(_NAMESPACE_SEPARATOR)
    folder, _, name = path.rpartition(_NAMESPACE_SEPARATOR)
    for root in runbooks.roots:
        if _fold(root.folder, fold_case=fold_case) != _fold(folder, fold_case=fold_case):
            continue
        slug = root.directory_slug(name, fold_case=fold_case)
        if slug is None:
            break
        for known_root, known_name, known_slug in existing:
            if known_root is root and _fold(known_name, fold_case=fold_case) == _fold(
                name, fold_case=fold_case
            ):
                return _location(runbooks, root, known_name, known_slug, exists=True)
        return _location(runbooks, root, name, slug, exists=False)
    raise RunbookRoutingError(
        f"subject {reference!r} is not a runbook folder of a declared root; runbook roots: "
        f"{_candidates([root.folder for root in runbooks.roots])}"
    )


def resolve_runbook(
    subject: str,
    runbooks: RunbooksConfig,
    organization: OrganizationConfig | None,
    children: Mapping[str, Sequence[str]],
    *,
    fold_case: bool,
) -> RunbookLocation:
    """Return the runbook ``subject`` names, or refuse with the candidates.

    ``subject`` is a runbook folder path (``CPDC/wsus-runbook``), a registered
    subject (tag, last segment or alias) or the slug of an existing runbook.
    ``children`` maps each declared root folder to the names of its child folders.

    A runbook that does not exist yet is returned with ``exists=False`` only when
    the subject is a runbook folder path or a registered subject: an unknown word
    never creates a runbook, so a typo cannot start one.
    """
    reference = subject.strip() if isinstance(subject, str) else ""
    if not reference:
        raise RunbookRoutingError("subject must not be empty")
    existing = _existing_runbooks(runbooks, children, fold_case=fold_case)
    if _NAMESPACE_SEPARATOR in reference.replace("\\", _NAMESPACE_SEPARATOR) and not (
        matching_subject_tags(reference, organization)
    ):
        return _by_path(reference, runbooks, existing, fold_case=fold_case)
    tags = matching_subject_tags(reference, organization)
    if len(tags) > 1:
        raise RunbookRoutingError(
            f"subject {reference!r} is ambiguous; it matches the subjects {_candidates(tags)}"
        )
    slugs = [reference]
    if tags:
        tag = tags[0]
        registered = next(
            item
            for item in (organization.tags.subjects if organization and organization.tags else ())
            if item.tag == tag
        )
        slugs = [tag.rsplit(_NAMESPACE_SEPARATOR, 1)[-1], *registered.aliases]
    wanted = {_fold(slug, fold_case=True) for slug in slugs}
    matches = [item for item in existing if item[2].casefold() in wanted]
    if len(matches) == 1:
        root, name, slug = matches[0]
        return _location(runbooks, root, name, slug, exists=True)
    if len(matches) > 1:
        raise RunbookRoutingError(
            f"subject {reference!r} matches several runbooks: "
            f"{_candidates([f'{root.folder}/{name}' for root, name, _ in matches])}. "
            "Repeat with the runbook folder path"
        )
    known = [f"{root.folder}/{name}" for root, name, _ in existing]
    if not tags:
        raise RunbookRoutingError(
            f"subject {reference!r} names no runbook and no registered subject; "
            f"runbooks: {_candidates(known)}. Repeat with an existing runbook, a registered "
            "subject, or the folder path of a new runbook"
        )
    slug = slugs[0].casefold()
    if not _SLUG_PATTERN.match(slug):
        raise RunbookRoutingError(
            f"subject {tags[0]!r} does not give a usable runbook name ({slug!r}); repeat with "
            "the folder path of the new runbook"
        )
    if len(runbooks.roots) > 1:
        raise RunbookRoutingError(
            f"subject {tags[0]!r} has no runbook yet and several roots could hold it: "
            f"{_candidates([root.folder for root in runbooks.roots])}. Repeat with the folder "
            "path of the new runbook"
        )
    root = runbooks.roots[0]
    return _location(runbooks, root, _directory_name(root, slug), slug, exists=False)


def resolve_role(kind: str, target_role: str | None, template: RunbookTemplate) -> str:
    """Return the role a capture of ``kind`` goes to, explicit role first."""
    declared = sorted(template.roles)
    if target_role is not None and target_role.strip():
        role = target_role.strip()
        if role not in template.roles:
            raise RunbookRoutingError(
                f"role {role!r} is not declared by the template; roles: {_candidates(declared)}"
            )
        return role
    role = template.routing.get(kind)
    if role is None:
        raise RunbookRoutingError(
            f"the template routes no role for kind {kind!r}; repeat with target_role, one of "
            f"{_candidates(declared)}"
        )
    return role


def resolve_role_file(location: RunbookLocation, role: str, stems: Sequence[str]) -> RunbookFile:
    """Return the file of ``role``: the one existing stem its pattern matches, or a new one.

    ``stems`` are the Markdown file stems at the top of the runbook folder.
    """
    declared = location.template.roles[role]
    matching = sorted(stem for stem in stems if declared.matches(stem))
    if len(matching) > 1:
        raise RunbookRoutingError(
            f"several files of {location.directory} play the role {role!r}: "
            f"{_candidates(matching)}. Repeat with target_role naming a role whose pattern "
            "matches one file"
        )
    # The template guarantees a role's own stem matches its pattern, so a file
    # already named like the stem was found above: no match means no file.
    stem = matching[0] if matching else declared.stem
    return RunbookFile(
        role=role,
        rel_path=f"{location.directory}/{stem}{RUNBOOK_MARKDOWN_SUFFIX}",
        exists=bool(matching),
    )


def quickref_rel_path(location: RunbookLocation, service: str) -> str:
    """Return the quickref sheet of ``service``, or refuse when the template has none."""
    quickref = location.template.quickref
    if quickref is None:
        raise RunbookRoutingError(f"the template of {location.directory} declares no quickref")
    name = service.strip().casefold() if isinstance(service, str) else ""
    if not _SLUG_PATTERN.match(name):
        raise RunbookRoutingError(
            f"quickref service must be a lowercase name ({_SLUG_PATTERN.pattern}); got {service!r}"
        )
    stem = quickref.naming.replace(f"{{{RUNBOOK_NAMING_SERVICE}}}", name)
    return f"{location.directory}/{quickref.folder}/{stem}{RUNBOOK_MARKDOWN_SUFFIX}"
