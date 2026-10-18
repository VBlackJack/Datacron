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
from typing import Any, Final

__all__ = [
    "ENV_MEMORY_ROOT",
    "ENV_READ_ONLY",
    "ENV_READ_PATHS",
    "ENV_VAULT_ROOT",
    "ENV_WRITE_PATHS",
    "env_flag_enabled",
    "env_names_vault",
    "merge_server_entry",
    "merge_server_env",
    "split_env_paths",
]

ENV_VAULT_ROOT: Final[str] = "DATACRON_VAULT_ROOT"
ENV_MEMORY_ROOT: Final[str] = "DATACRON_MEMORY_ROOT"
ENV_READ_PATHS: Final[str] = "DATACRON_READ_PATHS"
ENV_WRITE_PATHS: Final[str] = "DATACRON_WRITE_PATHS"
ENV_READ_ONLY: Final[str] = "DATACRON_READ_ONLY"

# Allowlists that only make sense inside the vault(s) they were written for.
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


def env_names_vault(env: Mapping[str, object], vault_root: Path) -> bool:
    """Return whether ``env`` was written for ``vault_root``."""
    value = env.get(ENV_VAULT_ROOT)
    return isinstance(value, str) and _normalized(value) == _normalized(str(vault_root))


def _paths_inside_any(value: str, vault_roots: Iterable[str]) -> list[str]:
    roots = [_normalized(root) for root in vault_roots if root]
    if not roots:
        return []
    return [
        part
        for part in split_env_paths(value)
        if any(_normalized(part).is_relative_to(root) for root in roots)
    ]


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

    When ``supplied`` names a vault root or memory root other than the one
    ``existing`` was written for, preserved allowlist entries outside the active
    vault root(s) are dropped, and the variable with them when nothing is left.
    """
    removed_keys = frozenset(removed)
    merged: dict[str, object] = {
        key: value for key, value in existing.items() if key not in removed_keys
    }
    new_root = supplied.get(ENV_VAULT_ROOT)
    old_root = existing.get(ENV_VAULT_ROOT)
    new_memory_root = supplied.get(ENV_MEMORY_ROOT)
    old_memory_root = existing.get(ENV_MEMORY_ROOT)
    vault_changed = new_root is not None and (
        not isinstance(old_root, str) or _normalized(old_root) != _normalized(new_root)
    )
    memory_changed = (
        new_memory_root is not None
        and (
            not isinstance(old_memory_root, str)
            or _normalized(old_memory_root) != _normalized(new_memory_root)
        )
    ) or (ENV_MEMORY_ROOT in removed_keys and old_memory_root is not None)
    if (vault_changed or memory_changed) and new_root is not None:
        active_roots: list[str] = [new_root]
        if new_memory_root is not None:
            active_roots.append(new_memory_root)
        elif isinstance(old_memory_root, str) and ENV_MEMORY_ROOT not in removed_keys:
            active_roots.append(old_memory_root)
        for key in _VAULT_BOUND_PATH_KEYS:
            value = merged.get(key)
            if key in supplied or not isinstance(value, str):
                continue
            kept = _paths_inside_any(value, active_roots)
            if kept:
                merged[key] = os.pathsep.join(kept)
            else:
                del merged[key]
    merged.update(supplied)
    _keep_memory_root_readable(merged, supplied)
    return merged


def _keep_memory_root_readable(merged: dict[str, object], supplied: Mapping[str, str]) -> None:
    """Add a preserved memory root to a read allowlist this run supplied.

    A rerun that did not name the memory vault kept ``DATACRON_MEMORY_ROOT``
    from the entry while laying its own docs-only ``DATACRON_READ_PATHS`` over
    it, and the server refuses to start when the memory root is outside the
    read allowlist. A read allowlist the caller did not supply is the user's and
    is left alone.
    """
    if ENV_READ_PATHS not in supplied or ENV_MEMORY_ROOT in supplied:
        return
    memory_root = merged.get(ENV_MEMORY_ROOT)
    read_value = merged.get(ENV_READ_PATHS)
    if not isinstance(memory_root, str) or not memory_root.strip():
        return
    if not isinstance(read_value, str):
        return
    if _paths_inside_any(memory_root, split_env_paths(read_value)):
        return
    merged[ENV_READ_PATHS] = os.pathsep.join([*split_env_paths(read_value), memory_root.strip()])


def merge_server_entry(
    existing: object,
    entry: dict[str, Any],
    removed_env: Iterable[str] = (),
) -> dict[str, Any]:
    """Return ``entry`` laid over the Datacron entry a client config already holds.

    The entry was replaced wholesale, so re-running setup dropped
    DATACRON_WRITE_PATHS and turned a writable vault read-only without a word,
    together with any proxy variable, ``disabled`` flag or ``autoApprove`` list
    the user had added. What this call supplies still wins; every other key and
    environment variable is kept. ``removed_env`` names what an explicit "off"
    drops; see :func:`merge_server_env`. Every client writer, Claude Desktop's
    included, goes through here.
    """
    if not isinstance(existing, dict):
        return entry
    merged: dict[str, Any] = {**existing, **{k: v for k, v in entry.items() if k != "env"}}
    existing_env = existing.get("env")
    supplied_env = entry.get("env", {})
    env = merge_server_env(
        existing_env if isinstance(existing_env, dict) else {},
        supplied_env if isinstance(supplied_env, dict) else {},
        removed=removed_env,
    )
    if env or isinstance(existing_env, dict):
        merged["env"] = env
    return merged
