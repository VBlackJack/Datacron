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
"""Blocking invariants for Datacron Calendar Version normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacron import __version__
from datacron.core.versioning import normalize_calver

pytestmark = pytest.mark.invariants

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2026.0718.03", "2026.718.3"),
        ("2026.0718.00", "2026.718.0"),
        ("2026.0105.01", "2026.105.1"),
    ],
)
def test_normalize_calver(version: str, expected: str) -> None:
    assert normalize_calver(version) == expected


@pytest.mark.parametrize(
    "version",
    ["2026.718.03", "2026.0718.3", "v2026.0718.03", "2026.0230.03"],
)
def test_normalize_calver_rejects_invalid_input(version: str) -> None:
    with pytest.raises(ValueError, match="invalid Datacron CalVer"):
        normalize_calver(version)


def test_server_descriptor_versions_match_package_version() -> None:
    raw: object = json.loads((_REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    packages = raw.get("packages")
    assert isinstance(packages, list)
    assert packages
    assert isinstance(packages[0], dict)

    expected = normalize_calver(__version__)
    assert raw["version"] == expected
    assert packages[0]["version"] == expected
