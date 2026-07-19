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
"""Tests for native directory durability capability probing."""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path

import pytest

from datacron.core import durability
from datacron.core.config import Settings
from datacron.core.durability import DurabilityStatus, WritePolicy, probe_directory_durability
from datacron.core.hashing import sha256_bytes


class _SimulatedWindowsError(PermissionError):
    def __init__(self, winerror: int) -> None:
        super().__init__(errno.EACCES, f"simulated Windows error {winerror}")
        self.winerror = winerror


def _windows_os_error(winerror: int) -> OSError:
    return _SimulatedWindowsError(winerror)


def test_probe_uses_existing_directory_without_creating_entries(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.txt"
    anchor.write_bytes(b"unchanged")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    status = probe_directory_durability(tmp_path)

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert status.backend
    assert isinstance(status.directory_flush_supported, bool)
    assert before == after == {"anchor.txt": b"unchanged"}


def test_atomic_write_retries_transient_windows_access_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    real_replace = os.replace
    attempts = 0
    sleeps: list[float] = []

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _windows_os_error(5)
        real_replace(source, destination)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(os, "replace", flaky_replace)

    returned_hash = durability.atomic_durable_write(target, b"new exact bytes")

    assert target.read_bytes() == b"new exact bytes"
    assert returned_hash == sha256_bytes(b"new exact bytes")
    assert attempts == 3
    assert sleeps == [0.005, 0.01]


@pytest.mark.parametrize("winerror", [32, 33])
def test_replace_retries_other_transient_windows_sharing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.md"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    real_replace = os.replace
    attempts = 0
    sleeps: list[float] = []

    def flaky_replace(current_source: Path, current_destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _windows_os_error(winerror)
        real_replace(current_source, current_destination)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(os, "replace", flaky_replace)

    durability._replace_with_windows_retry(source, destination)

    assert destination.read_bytes() == b"new"
    assert attempts == 2
    assert sleeps == [durability._REPLACE_RETRY_INITIAL_SLEEP_SECONDS]


@pytest.mark.parametrize(
    ("platform_name", "winerror"),
    [("win32", 2), ("linux", 5)],
)
def test_replace_does_not_retry_non_transient_or_non_windows_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    winerror: int,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def failing_replace(_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise _windows_os_error(winerror)

    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(PermissionError) as raised:
        durability._replace_with_windows_retry(tmp_path / "source", tmp_path / "destination")

    assert getattr(raised.value, "winerror", None) == winerror
    assert attempts == 1
    assert sleeps == []


def test_replace_retry_exhaustion_is_bounded_and_reraises_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    sleeps: list[float] = []
    raised_errors: list[OSError] = []

    def failing_replace(_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        error = _windows_os_error(5)
        raised_errors.append(error)
        raise error

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(PermissionError) as raised:
        durability._replace_with_windows_retry(tmp_path / "source", tmp_path / "destination")

    assert raised.value is raised_errors[-1]
    assert attempts == durability._REPLACE_RETRY_MAX_ATTEMPTS
    assert len(sleeps) == durability._REPLACE_RETRY_MAX_ATTEMPTS - 1
    assert sleeps == sorted(sleeps)
    assert max(sleeps) == durability._REPLACE_RETRY_MAX_SLEEP_SECONDS
    assert sum(sleeps) < 1.0


@pytest.mark.parametrize(
    (
        "read_only",
        "write_paths_configured",
        "durability_mode",
        "directory_flush_supported",
        "expected_policy_allowed",
        "expected_effective",
    ),
    [
        (False, True, "best-effort", True, True, True),
        (False, False, "best-effort", True, True, False),
        (False, True, "strict", False, False, False),
        (True, True, "best-effort", True, False, False),
    ],
)
def test_write_policy_reports_independent_and_effective_gates(
    tmp_path: Path,
    *,
    read_only: bool,
    write_paths_configured: bool,
    durability_mode: str,
    directory_flush_supported: bool,
    expected_policy_allowed: bool,
    expected_effective: bool,
) -> None:
    write_paths = [tmp_path] if write_paths_configured else []
    settings = Settings(
        read_paths=[tmp_path],
        write_paths=write_paths,
        vault_root=tmp_path,
        read_only=read_only,
        durability=durability_mode,
    )
    policy = WritePolicy(
        settings,
        DurabilityStatus(
            backend="test",
            directory_flush_supported=directory_flush_supported,
        ),
    )

    assert policy.writes_allowed is expected_policy_allowed
    assert policy.write_paths_configured is write_paths_configured
    assert policy.effective_writes_enabled is expected_effective
