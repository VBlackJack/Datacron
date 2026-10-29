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
"""Write boundary of the runbook capture.

A runbook file is writable only when every one of these holds: it lies inside
``DATACRON_RUNBOOK_WRITE_PATHS`` once resolved, inside the vault, in a child
folder of a declared runbook root that is named like a runbook, outside the
read-only vault folders and outside every excluded directory, and it is a
Markdown file. The generic write tools never consult this module, and the
runbook capture never consults ``DATACRON_WRITE_PATHS``: each boundary is
audited on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, final

from datacron.core.case_folding import filesystem_folds_case
from datacron.core.config import (
    RUNBOOK_EXCLUDED_DIRECTORIES,
    RUNBOOK_MARKDOWN_SUFFIX,
    RUNBOOK_READ_ONLY_FOLDERS,
    RunbookRoot,
    RunbooksConfig,
)
from datacron.core.paths import PathConfinementError, assert_within_paths

__all__ = ["RunbookScopeError", "RunbookTarget", "admit_runbook_write"]

# Smallest path under a root that can be a runbook file: <runbook folder>/<file>.
_MIN_PARTS_UNDER_ROOT: Final[int] = 2


class RunbookScopeError(ValueError):
    """A runbook write outside the runbook boundary.

    Deliberately not a ``PathConfinementError``: the write tools map that one to a
    message about ``DATACRON_WRITE_PATHS``, which the runbook capture never reads.
    """


@final
@dataclass(frozen=True, slots=True)
class RunbookTarget:
    """Where an admitted runbook write lands."""

    root: RunbookRoot
    directory: str
    slug: str
    relative: str
    resolved: Path


def _key(part: str, *, fold_case: bool) -> str:
    return part.casefold() if fold_case else part


def admit_runbook_write(
    path: Path | str,
    *,
    vault_root: Path,
    runbooks: RunbooksConfig,
    write_paths: Sequence[Path],
) -> RunbookTarget:
    """Return the runbook target of ``path`` or raise :class:`RunbookScopeError`.

    ``path`` is vault-relative or absolute. ``write_paths`` is
    ``Settings.runbook_write_paths``; an empty list refuses every write.
    """
    # Refusals name the path as the caller sent it: the resolved host path is
    # never returned, as for every other write refusal.
    given = str(path)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = vault_root / candidate
    if not write_paths:
        raise RunbookScopeError(
            f"runbook write refused: {given!r}; DATACRON_RUNBOOK_WRITE_PATHS is empty"
        )
    try:
        resolved = assert_within_paths(candidate, write_paths, kind="write")
    except PathConfinementError as exc:
        raise RunbookScopeError(
            f"runbook write refused: {given!r} is outside DATACRON_RUNBOOK_WRITE_PATHS"
        ) from exc
    root = vault_root.expanduser().resolve()
    try:
        parts = resolved.relative_to(root).parts
    except ValueError as exc:
        raise RunbookScopeError(f"runbook write refused: {given!r} is outside the vault") from exc
    relative = PurePosixPath(*parts).as_posix() if parts else ""
    if not parts or not parts[-1].casefold().endswith(RUNBOOK_MARKDOWN_SUFFIX):
        raise RunbookScopeError(f"runbook write refused: {relative!r} is not a Markdown file")
    folded = [part.casefold() for part in parts]
    if folded[0] in RUNBOOK_READ_ONLY_FOLDERS:
        raise RunbookScopeError(f"runbook write refused: {relative!r} is in a read-only folder")
    if any(part in RUNBOOK_EXCLUDED_DIRECTORIES for part in folded[:-1]):
        raise RunbookScopeError(
            f"runbook write refused: {relative!r} is in a directory runbooks never write into"
        )
    fold_case = filesystem_folds_case(root)
    for declared in runbooks.roots:
        root_parts = declared.folder.split("/")
        depth = len(root_parts)
        if len(parts) < depth + _MIN_PARTS_UNDER_ROOT:
            continue
        if [_key(part, fold_case=fold_case) for part in parts[:depth]] != [
            _key(part, fold_case=fold_case) for part in root_parts
        ]:
            continue
        slug = declared.directory_slug(parts[depth], fold_case=fold_case)
        if slug is None:
            raise RunbookScopeError(
                f"runbook write refused: {parts[depth]!r} is not named like a runbook "
                f"({declared.directory_naming!r})"
            )
        return RunbookTarget(
            root=declared,
            directory=PurePosixPath(*parts[: depth + 1]).as_posix(),
            slug=slug,
            relative=PurePosixPath(*parts[depth + 1 :]).as_posix(),
            resolved=resolved,
        )
    raise RunbookScopeError(
        f"runbook write refused: {relative!r} is not inside a declared runbook root"
    )
