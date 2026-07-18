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
"""PEP 440 normalization contract for Datacron's source CalVer."""

from __future__ import annotations

from packaging.version import Version

from datacron import __version__


def test_zero_padded_calver_has_documented_pep440_form() -> None:
    assert str(Version("2026.0718.01")) == "2026.718.1"


def test_package_version_has_stable_pep440_public_form() -> None:
    normalized = Version(__version__).public

    assert str(Version(normalized)) == normalized
