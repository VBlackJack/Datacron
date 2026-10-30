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
resolved: it lies inside the runbook vault (``DATACRON_RUNBOOK_VAULT``, else
``_runbooks`` beside ``_memory``), which itself lies inside the served vault;
its first folder under the runbook vault is named like a runbook; it is outside
the protected paths the vault declares and outside every excluded directory; and
it is a Markdown file. The generic write
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
    RUNBOOKS_SUBFOLDER_NAME,
    RunbooksConfig,
)
from datacron.core.paths import (
    canonical_memory_root_rel_path,
    is_memory_root_folder,
    is_memory_root_rel_path,
    is_within,
)

__all__ = [
    "RunbookScopeError",
    "RunbookTarget",
    "admit_runbook_write",
    "default_runbook_vault",
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


def default_runbook_vault(vault_root: Path, memory_root: Path | None) -> Path:
    """Return the runbook vault used when none is configured: ``_runbooks`` beside ``_memory``.

    ``_memory`` lives in the memory vault when one is configured, at the vault root
    otherwise, and ``_runbooks`` sits next to it either way.
    """
    return (memory_root or vault_root).expanduser().resolve() / RUNBOOKS_SUBFOLDER_NAME


def runbook_vault_folder(
    vault_root: Path, runbook_vault: Path, *, memory_root: Path | None = None
) -> str:
    """Return the runbook vault as a vault-relative folder, or refuse it.

    The runbook vault must lie inside the served vault: its files then share the
    vault's paths, locks and operation journal, and a recovery after a crash
    resolves them where they were written. In the memory vault only
    ``_runbooks`` (or a folder under it) is served; in the docs vault any folder
    is, the vault root itself included, except the ones the memory vault masks.
    """
    root = vault_root.expanduser().resolve()
    folder = runbook_vault.expanduser().resolve()
    memory = memory_root.expanduser().resolve() if memory_root is not None else None
    if memory is not None and is_within(folder, memory):
        parts = folder.relative_to(memory).parts
        if not parts or parts[0].casefold() != RUNBOOKS_SUBFOLDER_NAME.casefold():
            raise RunbookScopeError(
                f"DATACRON_RUNBOOK_VAULT in the memory vault must be {RUNBOOKS_SUBFOLDER_NAME} "
                f"or a folder under it; {runbook_vault} is not"
            )
        return PurePosixPath(RUNBOOKS_SUBFOLDER_NAME, *parts[1:]).as_posix()
    if not is_within(folder, root):
        raise RunbookScopeError(
            "DATACRON_RUNBOOK_VAULT must be a folder inside the served vault; "
            f"{runbook_vault} is not"
        )
    parts = folder.relative_to(root).parts
    if memory is not None and parts and is_memory_root_folder(parts[0]):
        raise RunbookScopeError(
            f"DATACRON_RUNBOOK_VAULT {runbook_vault} is masked by the memory vault {memory}"
        )
    # The vault root itself is the empty folder, not ".", so paths join without a dot.
    return PurePosixPath(*parts).as_posix() if parts else ""


def _physical(rel_path: str, *, vault_root: Path, memory_root: Path | None) -> Path:
    """Map a vault-relative path to the root that holds it, as the writer maps it."""
    if memory_root is not None and is_memory_root_rel_path(rel_path):
        return memory_root / canonical_memory_root_rel_path(rel_path)
    return vault_root / rel_path


def admit_runbook_write(
    path: Path | str,
    *,
    vault_root: Path,
    runbook_vault: Path | None,
    runbooks: RunbooksConfig,
    memory_root: Path | None = None,
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
    folder = runbook_vault_folder(vault_root, runbook_vault, memory_root=memory_root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = _physical(str(path), vault_root=vault_root, memory_root=memory_root)
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
    return RunbookTarget(
        directory=PurePosixPath(*(part for part in (folder, parts[0]) if part)).as_posix(),
        slug=slug,
        relative=PurePosixPath(*parts[1:]).as_posix(),
        resolved=resolved,
    )
