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
"""Path-case folding follows the vault's filesystem, not the operating system."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from datacron.core import case_folding
from datacron.organization import manifest as manifest_module
from datacron.organization import planner as planner_module
from datacron.organization.tags import path_within_scope


def _force_probe(monkeypatch: pytest.MonkeyPatch, *, folds: bool) -> None:
    monkeypatch.setattr(case_folding, "filesystem_folds_case", lambda _root: folds)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_manifest_path_keys_follow_the_probe_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Keys folded only when os.name was "nt": a macOS volume kept two spellings apart.

    The probe decides on every platform; forcing it either way must win over the
    platform, which is what the old os.name test could not do.
    """
    monkeypatch.setattr(case_folding, "sys", SimpleNamespace(platform=platform))

    _force_probe(monkeypatch, folds=True)
    with case_folding.case_folding_for(tmp_path):
        assert manifest_module._path_belongs_to_organization_scope("Memory/x.md", "memory")

    _force_probe(monkeypatch, folds=False)
    with case_folding.case_folding_for(tmp_path):
        assert not manifest_module._path_belongs_to_organization_scope("Memory/x.md", "memory")


def test_planner_and_tag_scope_take_the_filesystem_answer() -> None:
    assert planner_module._filesystem_parts(PurePosixPath("Memory/X"), fold_case=True) == (
        "memory",
        "x",
    )
    assert planner_module._filesystem_parts(PurePosixPath("Memory/X"), fold_case=False) == (
        "Memory",
        "X",
    )
    assert path_within_scope("Memory/x.md", "memory", fold_case=True)
    assert not path_within_scope("Memory/x.md", "memory", fold_case=False)


def test_the_probe_measures_the_volume_it_is_given(tmp_path: Path) -> None:
    """The probe agrees with a direct observation of the same directory."""
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    observed = (tmp_path / "caseprobe").exists()

    assert case_folding.filesystem_folds_case(probe) is observed
    assert case_folding.filesystem_folds_case(tmp_path) is observed


def test_without_a_cased_name_the_platform_default_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "0123"
    bare.mkdir()
    (bare / "42").mkdir()
    monkeypatch.setattr(case_folding, "sys", SimpleNamespace(platform="darwin"))
    assert case_folding.filesystem_folds_case(bare) is True
    case_folding._probe.cache_clear()
    monkeypatch.setattr(case_folding, "sys", SimpleNamespace(platform="linux"))
    assert case_folding.filesystem_folds_case(bare) is False


def test_outside_a_vault_block_the_platform_default_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(case_folding, "sys", SimpleNamespace(platform="darwin"))
    assert case_folding.fold_path_key("Memory/X.md") == "memory/x.md"
    monkeypatch.setattr(case_folding, "sys", SimpleNamespace(platform="linux"))
    assert case_folding.fold_path_key("Memory/X.md") == "Memory/X.md"
