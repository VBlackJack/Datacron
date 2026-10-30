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

A runbook file is writable only when every one of these holds, once its path is
resolved: it lies inside the runbook vault (``DATACRON_RUNBOOK_VAULT``), which
itself lies inside the served vault; its first folder under the runbook vault is
named like a runbook; it is outside the protected paths the vault declares and
outside every excluded directory; and it is a Markdown file. The generic write
tools never consult this module, and the runbook capture never consults
``DATACRON_WRITE_PATHS``: each boundary is audited on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, final

from datacron.core.case_folding import filesystem_folds_case
from datacron.core.config import (
    RUNBOOK_EXCLUDED_DIRECTORIES,
    RUNBOOK_MARKDOWN_SUFFIX,
    RunbooksConfig,
)
from datacron.core.paths import is_within

__all__ = [
    "RunbookScopeError",
    "RunbookTarget",
    "admit_runbook_write",
    "runbook_vault_folder",
]

# Smallest path under the runbook vault that can be a runbook file: <runbook>/<file>.
_MIN_PARTS_UNDER_VAULT: Final[int] = 2


class RunbookScopeError(ValueError):
    """A runbook write outside the runbook boundary.

    Deliberately not a ``PathConfinementError``: the write tools map that one to a
    message about ``DATACRON_WRITE_PATHS``, which the runbook capture never reads.
    """


@final
@dataclass(frozen=True, slots=True)
class RunbookTarget:
    """Where an admitted runbook write lands."""

    directory: str
    slug: str
    relative: str
    resolved: Path


def runbook_vault_folder(vault_root: Path, runbook_vault: Path) -> str:
    """Return the runbook vault as a vault-relative folder, or refuse it.

    The runbook vault must lie inside the served vault: its files then share the
    vault's paths, locks and operation journal, and a recovery after a crash
    resolves them where they were written. The vault root itself is accepted,
    since only folders named like a runbook are ever written.
    """
    root = vault_root.expanduser().resolve()
    folder = runbook_vault.expanduser().resolve()
    if not is_within(folder, root):
        raise RunbookScopeError(
            "DATACRON_RUNBOOK_VAULT must be a folder inside the served vault; "
            f"{runbook_vault} is not"
        )
    parts = folder.relative_to(root).parts
    # The vault root itself is the empty folder, not ".", so paths join without a dot.
    return PurePosixPath(*parts).as_posix() if parts else ""


def admit_runbook_write(
    path: Path | str,
    *,
    vault_root: Path,
    runbook_vault: Path | None,
    runbooks: RunbooksConfig,
) -> RunbookTarget:
    """Return the runbook target of ``path`` or raise :class:`RunbookScopeError`.

    ``path`` is vault-relative or absolute. A missing ``runbook_vault`` refuses
    every write.
    """
    # Refusals name the path as the caller sent it: the resolved host path is
    # never returned, as for every other write refusal.
    given = str(path)
    if runbook_vault is None:
        raise RunbookScopeError(
            f"runbook write refused: {given!r}; DATACRON_RUNBOOK_VAULT is unset"
        )
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = vault_root / candidate
    runbook_vault_folder(vault_root, runbook_vault)
    resolved = candidate.expanduser().resolve()
    base = runbook_vault.expanduser().resolve()
    if not is_within(resolved, base) or resolved == base:
        raise RunbookScopeError(
            f"runbook write refused: {given!r} is outside DATACRON_RUNBOOK_VAULT"
        )
    parts = resolved.relative_to(base).parts
    relative = PurePosixPath(*parts).as_posix()
    if not parts[-1].casefold().endswith(RUNBOOK_MARKDOWN_SUFFIX):
        raise RunbookScopeError(f"runbook write refused: {relative!r} is not a Markdown file")
    if runbooks.protects(parts):
        raise RunbookScopeError(f"runbook write refused: {relative!r} is in a protected path")
    if any(part.casefold() in RUNBOOK_EXCLUDED_DIRECTORIES for part in parts[:-1]):
        raise RunbookScopeError(
            f"runbook write refused: {relative!r} is in a directory runbooks never write into"
        )
    if len(parts) < _MIN_PARTS_UNDER_VAULT:
        raise RunbookScopeError(
            f"runbook write refused: {relative!r} is not inside a runbook folder"
        )
    slug = runbooks.directory_slug(parts[0], fold_case=filesystem_folds_case(base))
    if slug is None:
        raise RunbookScopeError(
            f"runbook write refused: {parts[0]!r} is not named like a runbook "
            f"({runbooks.directory_naming!r})"
        )
    root = vault_root.expanduser().resolve()
    return RunbookTarget(
        directory=PurePosixPath(*(base / parts[0]).relative_to(root).parts).as_posix(),
        slug=slug,
        relative=PurePosixPath(*parts[1:]).as_posix(),
        resolved=resolved,
    )
