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
"""Merge the environment of an MCP server entry a client config already holds.

Re-running setup keeps the settings it is not told about: an omitted option
leaves the existing value, so an upgrade does not turn a writable vault
read-only. Two things used to be missing from that rule. An explicit "off"
had no way to reach the file, so ``DATACRON_READ_ONLY`` and
``DATACRON_WRITE_PATHS`` could only ever be added; and a preserved allowlist
survived a change of vault, still pointing into the old one. Both installers
merge through :func:`merge_server_env` so they cannot drift apart again.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "ENV_READ_ONLY",
    "ENV_READ_PATHS",
    "ENV_VAULT_ROOT",
    "ENV_WRITE_PATHS",
    "env_flag_enabled",
    "merge_server_env",
    "split_env_paths",
]

ENV_VAULT_ROOT: Final[str] = "DATACRON_VAULT_ROOT"
ENV_READ_PATHS: Final[str] = "DATACRON_READ_PATHS"
ENV_WRITE_PATHS: Final[str] = "DATACRON_WRITE_PATHS"
ENV_READ_ONLY: Final[str] = "DATACRON_READ_ONLY"

# Allowlists that only make sense inside the vault they were written for.
_VAULT_BOUND_PATH_KEYS: Final[tuple[str, ...]] = (ENV_WRITE_PATHS, ENV_READ_PATHS)
# Spellings pydantic-settings accepts as a true boolean.
_TRUE_ENV_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on", "t", "y"})


def split_env_paths(value: str) -> list[str]:
    """Split one path-list variable with the platform separator, dropping blanks."""
    return [part.strip() for part in value.split(os.pathsep) if part.strip()]


def env_flag_enabled(value: object) -> bool:
    """Return whether an environment value reads as a true boolean."""
    return isinstance(value, str) and value.strip().casefold() in _TRUE_ENV_VALUES


def _normalized(path_text: str) -> Path:
    resolved = Path(path_text).expanduser().resolve(strict=False)
    return Path(os.path.normcase(str(resolved)))


def _paths_inside(value: str, vault_root: str) -> list[str]:
    root = _normalized(vault_root)
    return [part for part in split_env_paths(value) if _normalized(part).is_relative_to(root)]


def merge_server_env(
    existing: Mapping[str, object],
    supplied: Mapping[str, str],
    *,
    removed: Iterable[str] = (),
) -> dict[str, object]:
    """Lay ``supplied`` over ``existing`` and return the environment to write.

    Args:
        existing: The environment the client config already holds.
        supplied: What this run sets; it always wins.
        removed: Keys this run explicitly turns off; they are dropped from
            ``existing`` rather than kept.

    When ``supplied`` names a vault root other than the one ``existing`` was
    written for, preserved allowlist entries outside the new vault are dropped,
    and the variable with them when nothing is left.
    """
    removed_keys = frozenset(removed)
    merged: dict[str, object] = {
        key: value for key, value in existing.items() if key not in removed_keys
    }
    new_root = supplied.get(ENV_VAULT_ROOT)
    old_root = existing.get(ENV_VAULT_ROOT)
    vault_changed = new_root is not None and (
        not isinstance(old_root, str) or _normalized(old_root) != _normalized(new_root)
    )
    if vault_changed and new_root is not None:
        for key in _VAULT_BOUND_PATH_KEYS:
            value = merged.get(key)
            if key in supplied or not isinstance(value, str):
                continue
            kept = _paths_inside(value, new_root)
            if kept:
                merged[key] = os.pathsep.join(kept)
            else:
                del merged[key]
    merged.update(supplied)
    return merged
