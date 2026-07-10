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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from datacron.core.config import Settings
from datacron.core.logger import get_logger

__all__ = [
    "DurabilityStatus",
    "DurabilityUnavailableError",
    "ReadOnlyModeError",
    "WritePolicy",
    "flush_directory_entry",
    "probe_directory_durability",
]

_LOGGER = get_logger(__name__)
_WINDOWS_MAX_PATH: Final[int] = 32768


class ReadOnlyModeError(PermissionError):
    """Raised when a write reaches the certified read-only server."""


class DurabilityUnavailableError(PermissionError):
    """Raised when strict mode cannot prove directory-entry durability."""


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
    """Fail closed for read-only or unsupported strict durability modes."""

    def __init__(self, settings: Settings, durability: DurabilityStatus) -> None:
        self._settings = settings
        self._durability = durability

    @property
    def writes_allowed(self) -> bool:
        if self._settings.read_only:
            return False
        return not (
            self._settings.durability == "strict" and not self._durability.directory_flush_supported
        )

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


def flush_directory_entry(path: Path) -> bool:
    """Flush directory metadata through the native platform primitive."""
    if os.name == "nt":
        return _flush_windows_directory(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def _filesystem_backend(path: Path) -> str:
    if os.name != "nt":
        return f"{platform.system().lower()}-filesystem"
    return _windows_filesystem_name(path)


def _windows_filesystem_name(path: Path) -> str:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

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
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

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
