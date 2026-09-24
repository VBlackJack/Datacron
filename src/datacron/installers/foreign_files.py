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
"""Replace a file that belongs to the user's editor, not to Datacron.

Client configs and agent instruction files were each rewritten by their own
copy of the same few lines, and the copies had drifted: only the instruction
writer refused a symlink, and none of them noticed that nothing had changed. A
second identical setup run therefore refreshed the ``.datacron-backup`` copy
with the already-rewritten file, so the comments the first run dropped were lost
for good, and a config kept in a dotfiles repository behind a link was replaced
by a regular file that the repository no longer tracked.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Final

from datacron.core.durability import atomic_durable_write

__all__ = ["BACKUP_SUFFIX", "replace_foreign_file"]

BACKUP_SUFFIX: Final[str] = ".datacron-backup"
_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400


def _link_target(path: Path) -> str | None:
    """Describe what ``path`` links to, or return ``None`` when it is not a link."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    attributes = getattr(status, "st_file_attributes", 0)
    if stat.S_ISLNK(status.st_mode):
        return os.readlink(path)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return "a reparse point"
    return None


def replace_foreign_file(
    path: Path,
    payload: bytes,
    *,
    error: Callable[[str], Exception],
) -> bool:
    """Write ``payload`` to ``path`` unless it already holds exactly those bytes.

    A link is refused rather than replaced: ``os.replace`` onto it writes a
    regular file over the link. The previous content is copied next to the file
    before a real change, and only then. Returns whether the file was written.

    Args:
        path: The foreign file to replace.
        payload: The complete new content.
        error: Builds the exception the caller's module raises.

    Raises:
        Exception: Whatever ``error`` builds, when ``path`` is a link or cannot
            be inspected.
    """
    try:
        target = _link_target(path)
    except OSError as exc:
        raise error(f"cannot inspect {path}: {exc}") from exc
    if target is not None:
        raise error(
            f"{path} is a link to {target}; Datacron will not replace a link with a "
            "regular file. Edit the target directly, or remove the link first."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return False
        shutil.copy2(path, path.with_name(f"{path.name}{BACKUP_SUFFIX}"))
    atomic_durable_write(path, payload)
    return True
