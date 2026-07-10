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

from datacron.core.durability import probe_directory_durability


def test_probe_uses_existing_directory_without_creating_entries(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.txt"
    anchor.write_bytes(b"unchanged")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    status = probe_directory_durability(tmp_path)

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert status.backend
    assert isinstance(status.directory_flush_supported, bool)
    assert before == after == {"anchor.txt": b"unchanged"}
