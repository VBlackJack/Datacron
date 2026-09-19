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
"""Path confinement primitives.

All filesystem access performed by Datacron MCP tools must route through
:func:`assert_within_read_paths` (or :func:`assert_within_write_paths` once
write tools land post-Phase-0). The Phase-0 invariant: any path outside the
configured roots is rejected before it reaches the filesystem.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final, Literal

from datacron.core.config import (
    INDEX_DB_FILENAME,
    INDEX_DIR_NAME,
    SIDECAR_DIR_NAME,
    VAULT_CONFIG_FILENAME,
    Settings,
    get_settings,
)

__all__ = [
    "PathConfinementError",
    "assert_vault_rel_path",
    "assert_within_paths",
    "assert_within_read_paths",
    "assert_within_resolved_roots",
    "assert_within_write_paths",
    "is_within",
    "read_ulid_mappings",
    "read_ulid_sidecar_strict",
    "reject_duplicate_json_keys",
    "reject_nonfinite_json_constant",
    "sidecar_dir",
    "sidecar_index_db",
    "sidecar_index_dir",
    "sidecar_vault_config",
    "strip_extended_length_prefix",
]

AccessKind = Literal["read", "write"]


class PathConfinementError(PermissionError):
    """Raised when a path falls outside the configured allowed roots."""


_EXTENDED_LENGTH_PREFIX: Final = "\\\\?\\"
_EXTENDED_LENGTH_UNC_PREFIX: Final = "\\\\?\\UNC\\"


def assert_vault_rel_path(rel_path: str) -> str:
    """Refuse a vault-relative path while it is still only a string.

    :func:`assert_within_paths` resolves its argument before comparing it to the
    allowed roots. On Windows that resolution is a ``CreateFileW`` call, so a
    caller-supplied UNC path reaches the network before confinement rejects it.
    Callers screen the raw string here so the escape never becomes I/O.

    The component rules also keep Win32 from silently rewriting a path: a
    directory component ending in a space or a dot is created without it, which
    leaves a journal record naming a path that can never be resolved again.

    Args:
        rel_path: Candidate vault-relative path. The empty string denotes the
            vault root and is accepted.

    Returns:
        ``rel_path`` unchanged, so callers can wrap an argument in place.

    Raises:
        PathConfinementError: If the path names a drive, a UNC share or an absolute
            location, traverses out of the vault, carries a control character,
            or ends a component with a dot or a space.
    """
    if not rel_path:
        return rel_path
    if any(ord(character) < 32 for character in rel_path):
        raise PathConfinementError("Vault-relative path must not contain control characters.")
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("//"):
        raise PathConfinementError(f"Vault-relative path must not name a UNC share: {rel_path!r}")
    windows_path = PureWindowsPath(normalized)
    if windows_path.drive or windows_path.is_absolute():
        raise PathConfinementError(
            f"Vault-relative path must not be absolute or name a drive: {rel_path!r}"
        )
    posix_path = PurePosixPath(normalized)
    if posix_path.is_absolute():
        raise PathConfinementError(f"Vault-relative path must not be absolute: {rel_path!r}")
    for part in posix_path.parts:
        if part in {"", ".", ".."}:
            raise PathConfinementError(
                f"Vault-relative path must not traverse directories: {rel_path!r}"
            )
        if part.endswith((" ", ".")):
            raise PathConfinementError(
                f"Vault-relative path components must not end with a dot or a space: {rel_path!r}"
            )
    return rel_path


def strip_extended_length_prefix(path: Path) -> Path:
    """Return ``path`` without the Windows extended-length prefix.

    ``Path.resolve`` returns the ``\\\\?\\`` form whenever an intermediate
    component exists but the leaf does not. Comparing that form against a plain
    vault root fails, so a single unresolvable journal record would otherwise
    wedge recovery for the whole vault.
    """
    text = str(path)
    if text.startswith(_EXTENDED_LENGTH_UNC_PREFIX):
        return Path("\\\\" + text[len(_EXTENDED_LENGTH_UNC_PREFIX) :])
    if text.startswith(_EXTENDED_LENGTH_PREFIX):
        return Path(text[len(_EXTENDED_LENGTH_PREFIX) :])
    return path


def read_ulid_mappings(
    path: Path,
    *,
    require_string_pairs: bool = False,
    invalid_object_is_empty: bool = False,
) -> dict[str, str]:
    """Read a ULID sidecar while preserving the caller's validation policy."""
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        if invalid_object_is_empty:
            return {}
        raise ValueError(f"ULID sidecar {path} is not a JSON object (found {type(data).__name__}).")
    if require_string_pairs:
        return {
            rel_path: note_id
            for rel_path, note_id in data.items()
            if isinstance(rel_path, str) and isinstance(note_id, str)
        }
    return {str(rel_path): str(note_id) for rel_path, note_id in data.items()}


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while refusing ambiguous duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def reject_nonfinite_json_constant(value: str) -> object:
    """Reject Python's non-standard NaN and infinity JSON extensions."""
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def read_ulid_sidecar_strict(path: Path, *, max_bytes: int) -> tuple[bytes, dict[str, str]]:
    """Read a ULID sidecar for compare-and-set: its exact bytes and its string pairs.

    Stricter than :func:`read_ulid_mappings`: the sidecar must be a regular file of at
    most ``max_bytes``, nonempty, strict UTF-8, and one JSON object of string pairs
    without duplicate keys or non-finite constants. The caller resolves the path chain
    (symlink refusal) before calling, and maps ``OSError`` and ``ValueError`` to its own
    error class.
    """
    if not path.is_file():
        raise ValueError(f"ULID sidecar is not a regular file: {path.name}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"ULID sidecar {path.name} is {size} bytes; limit is {max_bytes}")
    with path.open("rb") as stream:
        raw_bytes = stream.read(max_bytes + 1)
    if len(raw_bytes) > max_bytes:
        raise ValueError(
            f"ULID sidecar {path.name} exceeds the {max_bytes}-byte bounded-read limit"
        )
    if not raw_bytes:
        raise ValueError(f"ULID sidecar must not be empty: {path.name}")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"ULID sidecar {path.name} is not strict UTF-8: {exc}") from exc
    parsed = json.loads(
        text,
        object_pairs_hook=reject_duplicate_json_keys,
        parse_constant=reject_nonfinite_json_constant,
    )
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValueError(f"ULID sidecar must contain only string pairs: {path.name}")
    return raw_bytes, {
        key: value
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def is_within(path: Path, root: Path) -> bool:
    """Return ``True`` if ``path`` is the same as or a descendant of ``root``.

    Both arguments are resolved (symlinks followed). No I/O check is performed
    on existence - this is a pure path-relationship test.
    """
    try:
        _resolve(path).relative_to(_resolve(root))
    except ValueError:
        return False
    return True


def assert_within_resolved_roots(
    path: Path,
    resolved_roots: Sequence[Path],
    *,
    kind: AccessKind = "read",
) -> Path:
    """Resolve ``path`` and confirm it lies within one of the already-resolved roots.

    :func:`assert_within_paths` resolves its roots on every call. A scope whose root
    was resolved when it was built then paid a second realpath per authorized path,
    for an answer it already had: resolving an absolute resolved path returns it
    unchanged. That doubled the cost of every path this product authorizes, and a
    sweep over an indexed vault pays it once per note.

    Callers must pass roots they have already resolved. Passing an unresolved root
    here would compare a resolved path against an unresolved one and refuse
    legitimate paths, so the roots that reach this are the ones a constructor
    resolved, never a raw setting.
    """
    resolved = _resolve(path)
    if not resolved_roots:
        raise PathConfinementError(
            f"No {kind} paths are configured; access to {resolved} is denied."
        )
    for root in resolved_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        else:
            return resolved
    pretty_roots = ", ".join(str(root) for root in resolved_roots)
    raise PathConfinementError(
        f"Path {resolved} is outside the allowed {kind} roots [{pretty_roots}]."
    )


def assert_within_paths(
    path: Path,
    allowed_roots: Iterable[Path],
    *,
    kind: AccessKind = "read",
) -> Path:
    """Resolve ``path`` and confirm it lies within one of ``allowed_roots``.

    Args:
        path: Candidate filesystem path.
        allowed_roots: Iterable of roots that bound legitimate access.
        kind: Used only in the error message (``"read"`` or ``"write"``).

    Returns:
        The resolved absolute :class:`Path`.

    Raises:
        PathConfinementError: If ``path`` is outside every root, or if the
            ``allowed_roots`` iterable is empty.
    """
    resolved = _resolve(path)
    roots = [_resolve(r) for r in allowed_roots]
    if not roots:
        raise PathConfinementError(
            f"No {kind} paths are configured; access to {resolved} is denied."
        )
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        else:
            return resolved
    pretty_roots = ", ".join(str(r) for r in roots)
    raise PathConfinementError(
        f"Path {resolved} is outside the allowed {kind} roots [{pretty_roots}]."
    )


def assert_within_read_paths(path: Path, settings: Settings | None = None) -> Path:
    """Validate ``path`` against ``DATACRON_READ_PATHS``."""
    resolved_settings = settings or get_settings()
    return assert_within_paths(path, resolved_settings.read_paths, kind="read")


def assert_within_write_paths(path: Path, settings: Settings | None = None) -> Path:
    """Validate ``path`` against ``DATACRON_WRITE_PATHS``."""
    resolved_settings = settings or get_settings()
    return assert_within_paths(path, resolved_settings.write_paths, kind="write")


def sidecar_dir(vault_root: Path) -> Path:
    """Return the ``.datacron/`` sidecar directory under ``vault_root``."""
    return _resolve(vault_root) / SIDECAR_DIR_NAME


def sidecar_index_dir(vault_root: Path) -> Path:
    """Return the ``.datacron/index/`` directory under ``vault_root``."""
    return sidecar_dir(vault_root) / INDEX_DIR_NAME


def sidecar_index_db(vault_root: Path) -> Path:
    """Return the canonical SQLite index path under ``vault_root``."""
    return sidecar_index_dir(vault_root) / INDEX_DB_FILENAME


def sidecar_vault_config(vault_root: Path) -> Path:
    """Return the ``.datacron/VAULT.yaml`` path under ``vault_root``."""
    return sidecar_dir(vault_root) / VAULT_CONFIG_FILENAME
