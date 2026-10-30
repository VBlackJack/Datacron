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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, final

from datacron.core.config import (
    RUNBOOK_MARKDOWN_SUFFIX,
    RUNBOOK_NAMING_SERVICE,
    RUNBOOK_SLUG_PATTERN,
    OrganizationConfig,
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
_NAMESPACE_SEPARATOR: Final[str] = "/"
_CREATE_HINT: Final[str] = (
    "To start the runbook of a project named in the conversation, repeat with create_runbook=true"
)


class RunbookRoutingError(ValueError):
    """A capture whose destination cannot be decided without a guess."""


@final
@dataclass(frozen=True, slots=True)
class RunbookLocation:
    """One runbook folder, existing or to be created from its template."""

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


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _location(
    runbooks: RunbooksConfig, folder: str, name: str, slug: str, *, exists: bool
) -> RunbookLocation:
    return RunbookLocation(
        template=runbooks.template_for(slug),
        directory=_join(folder, name),
        slug=slug,
        exists=exists,
    )


def _new_location(
    runbooks: RunbooksConfig, folder: str, slug: str, *, create: bool, reference: str
) -> RunbookLocation:
    if not create:
        raise RunbookRoutingError(f"subject {reference!r} has no runbook yet. {_CREATE_HINT}")
    if not RUNBOOK_SLUG_PATTERN.match(slug):
        raise RunbookRoutingError(
            f"subject {reference!r} does not give a usable runbook name ({slug!r}); use "
            f"lowercase letters, digits and hyphens ({RUNBOOK_SLUG_PATTERN.pattern})"
        )
    return _location(runbooks, folder, runbooks.directory_name(slug), slug, exists=False)


def resolve_runbook(
    subject: str,
    runbooks: RunbooksConfig,
    organization: OrganizationConfig | None,
    *,
    vault_folder: str,
    children: Sequence[str],
    fold_case: bool,
    create: bool = False,
) -> RunbookLocation:
    """Return the runbook ``subject`` names, or refuse with the candidates.

    ``subject`` is a project name, a registered subject (tag, last segment or
    alias), or a runbook folder path inside the runbook vault. ``vault_folder`` is
    the runbook vault, vault-relative; ``children`` are the names of its folders.

    A runbook that does not exist yet is returned with ``exists=False`` only when
    ``create`` is true: the name of a new runbook comes from the project named in
    the conversation, and asking for it explicitly is what keeps a typo from
    starting one.
    """
    reference = subject.strip() if isinstance(subject, str) else ""
    if not reference:
        raise RunbookRoutingError("subject must not be empty")
    known = [
        (name, slug)
        for name in children
        if (slug := runbooks.directory_slug(name, fold_case=fold_case)) is not None
    ]
    tags = matching_subject_tags(reference, organization)
    path = reference.replace("\\", _NAMESPACE_SEPARATOR).strip(_NAMESPACE_SEPARATOR)
    if _NAMESPACE_SEPARATOR in path and not tags:
        return _by_path(path, runbooks, vault_folder, known, fold_case=fold_case, create=create)
    if len(tags) > 1:
        raise RunbookRoutingError(
            f"subject {reference!r} is ambiguous; it matches the subjects {_candidates(tags)}"
        )
    slugs = [reference]
    if tags:
        registered = next(
            item
            for item in (organization.tags.subjects if organization and organization.tags else ())
            if item.tag == tags[0]
        )
        slugs = [tags[0].rsplit(_NAMESPACE_SEPARATOR, 1)[-1], *registered.aliases]
    wanted = {slug.casefold() for slug in slugs}
    matches = [(name, slug) for name, slug in known if slug.casefold() in wanted]
    if len(matches) > 1:
        raise RunbookRoutingError(
            f"subject {reference!r} matches several runbooks: "
            f"{_candidates([_join(vault_folder, name) for name, _ in matches])}. "
            "Repeat with the runbook folder path"
        )
    if matches:
        name, slug = matches[0]
        return _location(runbooks, vault_folder, name, slug, exists=True)
    if not create:
        raise RunbookRoutingError(
            f"subject {reference!r} names no existing runbook; runbooks: "
            f"{_candidates([_join(vault_folder, name) for name, _ in known])}. {_CREATE_HINT}"
        )
    return _new_location(
        runbooks, vault_folder, slugs[0].casefold(), create=create, reference=reference
    )


def _by_path(
    path: str,
    runbooks: RunbooksConfig,
    vault_folder: str,
    known: list[tuple[str, str]],
    *,
    fold_case: bool,
    create: bool,
) -> RunbookLocation:
    prefix = f"{vault_folder}{_NAMESPACE_SEPARATOR}" if vault_folder else ""
    inside = _fold(path, fold_case=fold_case).startswith(_fold(prefix, fold_case=fold_case))
    name = path[len(prefix) :] if inside else ""
    if not inside or not name or _NAMESPACE_SEPARATOR in name:
        raise RunbookRoutingError(
            f"subject {path!r} is not a runbook folder of the runbook vault "
            f"({vault_folder or 'the vault root'})"
        )
    for known_name, known_slug in known:
        if _fold(known_name, fold_case=fold_case) == _fold(name, fold_case=fold_case):
            return _location(runbooks, vault_folder, known_name, known_slug, exists=True)
    slug = runbooks.directory_slug(name, fold_case=fold_case)
    if slug is None:
        raise RunbookRoutingError(
            f"subject {path!r} is not named like a runbook ({runbooks.directory_naming!r})"
        )
    if not create:
        raise RunbookRoutingError(f"runbook {path!r} does not exist yet. {_CREATE_HINT}")
    return _location(runbooks, vault_folder, name, slug, exists=False)


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
    routed = template.routing.get(kind)
    if routed is None:
        raise RunbookRoutingError(
            f"the template routes no role for kind {kind!r}; repeat with target_role, one of "
            f"{_candidates(declared)}"
        )
    return routed


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
    if not RUNBOOK_SLUG_PATTERN.match(name):
        raise RunbookRoutingError(
            "quickref service must be a lowercase name "
            f"({RUNBOOK_SLUG_PATTERN.pattern}); got {service!r}"
        )
    stem = quickref.naming.replace(f"{{{RUNBOOK_NAMING_SERVICE}}}", name)
    return f"{location.directory}/{quickref.folder}/{stem}{RUNBOOK_MARKDOWN_SUFFIX}"
