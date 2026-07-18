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

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.durability import DurabilityStatus, WritePolicy, probe_directory_durability


def test_probe_uses_existing_directory_without_creating_entries(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.txt"
    anchor.write_bytes(b"unchanged")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    status = probe_directory_durability(tmp_path)

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert status.backend
    assert isinstance(status.directory_flush_supported, bool)
    assert before == after == {"anchor.txt": b"unchanged"}


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
