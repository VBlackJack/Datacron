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
"""Directory-flush capability probing and centralized write policy."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final
from uuid import uuid4

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

from datacron.core.config import Settings
from datacron.core.hashing import sha256_bytes
from datacron.core.logger import get_logger

__all__ = [
    "DurabilityStatus",
    "DurabilityUnavailableError",
    "ReadOnlyModeError",
    "WritePolicy",
    "atomic_durable_write",
    "durable_flush_directory",
    "flush_directory_entry",
    "probe_directory_durability",
]

_LOGGER = get_logger(__name__)
_WINDOWS_MAX_PATH: Final[int] = 32768
FaultInjector = Callable[[str], None]


class ReadOnlyModeError(PermissionError):
    """Raised when a write reaches the certified read-only server."""


class DurabilityUnavailableError(PermissionError):
    """Raised when strict mode cannot prove directory-entry durability."""


def atomic_durable_write(
    path: Path,
    data: bytes,
    *,
    fault_injector: FaultInjector | None = None,
) -> str:
    """Atomically replace ``path`` with durable exact ``data`` and return its SHA-256.

    The caller must create ``path.parent`` first. The temporary file is always a
    sibling so the replacement stays on one filesystem. On Windows, directory
    flushing uses a Win32 directory handle because ``os.open`` rejects directories.
    A target-file fsync is the logged degraded fallback for filesystems that reject
    directory flushing.
    """
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _inject(fault_injector, "before_temp_open")
    try:
        with temp_path.open("xb") as temp_file:
            _inject(fault_injector, "after_temp_open")
            temp_file.write(data)
            _inject(fault_injector, "after_temp_write")
            temp_file.flush()
            _inject(fault_injector, "after_temp_flush")
            os.fsync(temp_file.fileno())
            _inject(fault_injector, "after_temp_fsync")

        os.replace(temp_path, path)
        _inject(fault_injector, "after_replace")
        if not _flush_directory_or_false(path.parent):
            _fsync_file(path)
            _LOGGER.warning(
                "Parent-directory fsync unavailable for %s; used degraded target-file fsync",
                path,
            )
        _inject(fault_injector, "after_directory_fsync")
    finally:
        temp_path.unlink(missing_ok=True)
    return sha256_bytes(data)


def durable_flush_directory(path: Path) -> None:
    """Durably flush directory metadata, with the documented Windows fallback."""
    if not _flush_directory_or_false(path):
        _LOGGER.warning("Directory fsync unavailable for metadata update under %s", path)


def _flush_directory_or_false(path: Path) -> bool:
    try:
        return flush_directory_entry(path)
    except OSError as exc:
        _LOGGER.warning("Directory fsync failed for %s: %s", path, exc)
        return False


def _fsync_file(path: Path) -> None:
    # Windows requires a writable handle for os.fsync/FlushFileBuffers.
    with path.open("r+b") as file_handle:
        os.fsync(file_handle.fileno())


def _inject(fault_injector: FaultInjector | None, point: str) -> None:
    if fault_injector is not None:
        fault_injector(point)


@dataclass(frozen=True)
class DurabilityStatus:
    """Observed capability of the current vault filesystem backend."""

    backend: str
    directory_flush_supported: bool

    def to_dict(self, *, mode: str) -> dict[str, object]:
        return {
            "backend": self.backend,
            "directory_flush_supported": self.directory_flush_supported,
            "mode": mode,
        }


@final
class WritePolicy:
    """Evaluate the independent mode, durability, and configured-path write gates."""

    def __init__(self, settings: Settings, durability: DurabilityStatus) -> None:
        self._settings = settings
        self._durability = durability

    @property
    def writes_allowed(self) -> bool:
        """Return whether mode and durability policy permit writes."""
        if self._settings.read_only:
            return False
        return not (
            self._settings.durability == "strict" and not self._durability.directory_flush_supported
        )

    @property
    def write_paths_configured(self) -> bool:
        """Return whether at least one target root is configured for writes."""
        return bool(self._settings.write_paths)

    @property
    def effective_writes_enabled(self) -> bool:
        """Return whether policy and configured-path gates both permit writes."""
        return self.writes_allowed and self.write_paths_configured

    def ensure_writable(self) -> None:
        if self._settings.read_only:
            raise ReadOnlyModeError(
                "writes are disabled by certified read-only mode (DATACRON_READ_ONLY=true)"
            )
        if self._settings.durability == "strict" and not self._durability.directory_flush_supported:
            raise DurabilityUnavailableError(
                "strict durability refuses writes because directory flush is unsupported "
                f"on backend {self._durability.backend}"
            )
        if not self._durability.directory_flush_supported:
            _LOGGER.warning(
                "BEST-EFFORT DURABILITY: directory flush is unsupported on backend %s; "
                "write is permitted without directory-entry durability proof",
                self._durability.backend,
            )


def probe_directory_durability(path: Path) -> DurabilityStatus:
    """Probe an existing directory without creating or deleting an entry."""
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"durability probe directory does not exist: {resolved}")
    backend = _filesystem_backend(resolved)
    try:
        supported = flush_directory_entry(resolved)
    except OSError as exc:
        supported = False
        _LOGGER.warning(
            "Directory durability probe failed on backend %s at %s: %s",
            backend,
            resolved,
            exc,
        )
    if supported:
        _LOGGER.info("Directory durability probe passed (backend=%s)", backend)
    else:
        _LOGGER.warning("Directory durability probe unsupported (backend=%s)", backend)
    return DurabilityStatus(backend=backend, directory_flush_supported=supported)


if sys.platform == "win32":

    def flush_directory_entry(path: Path) -> bool:
        """Flush directory metadata through the native Windows primitive."""
        return _flush_windows_directory(path)

    def _filesystem_backend(path: Path) -> str:
        return _windows_filesystem_name(path)

else:

    def flush_directory_entry(path: Path) -> bool:
        """Flush directory metadata through the native POSIX primitive."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True

    def _filesystem_backend(path: Path) -> str:
        return f"{platform.system().lower()}-filesystem"


if sys.platform == "win32":

    def _windows_filesystem_name(path: Path) -> str:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_volume_path_name = kernel32.GetVolumePathNameW
        get_volume_path_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_volume_path_name.restype = wintypes.BOOL
        get_volume_information = kernel32.GetVolumeInformationW
        get_volume_information.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPDWORD,
            wintypes.LPDWORD,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        get_volume_information.restype = wintypes.BOOL

        volume_path = ctypes.create_unicode_buffer(_WINDOWS_MAX_PATH)
        if not get_volume_path_name(str(path), volume_path, len(volume_path)):
            return "windows-unknown"
        filesystem_name = ctypes.create_unicode_buffer(256)
        serial = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        if not get_volume_information(
            volume_path.value,
            None,
            0,
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        ):
            return "windows-unknown"
        return filesystem_name.value.lower() or "windows-unknown"

    def _flush_windows_directory(path: Path) -> bool:
        generic_write = 0x40000000
        file_share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        invalid_handle_value = wintypes.HANDLE(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = [wintypes.HANDLE]
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            generic_write,
            file_share_all,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        if handle == invalid_handle_value:
            error = ctypes.get_last_error()
            _LOGGER.warning(
                "CreateFileW could not open directory %s for flush: winerror=%s",
                path,
                error,
            )
            return False
        try:
            if not flush_file_buffers(handle):
                error = ctypes.get_last_error()
                _LOGGER.warning(
                    "FlushFileBuffers failed for directory %s: winerror=%s",
                    path,
                    error,
                )
                return False
            return True
        finally:
            close_handle(handle)
