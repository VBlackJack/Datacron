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
"""Tests for :mod:`datacron.mcp.server`."""

from __future__ import annotations

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.paths import PathConfinementError
from datacron.mcp.server import build_app


class TestBuildAppReadPaths:
    def test_read_paths_allow_vault_inside_allowed_root(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        vault = allowed / "vault"
        vault.mkdir(parents=True)
        settings = Settings(read_paths=[allowed], vault_root=vault)

        app = build_app(settings=settings, vault_root=vault)

        assert app.vault_root == vault.resolve()

    def test_read_paths_reject_vault_outside_allowed_root(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        settings = Settings(read_paths=[allowed], vault_root=outside)

        with pytest.raises(PathConfinementError, match="outside the allowed read roots"):
            build_app(settings=settings, vault_root=outside)

    def test_empty_read_paths_keep_vault_root_as_implicit_boundary(self, tmp_path: Path) -> None:
        vault = tmp_path / "outside-any-allowlist"
        vault.mkdir()
        settings = Settings(read_paths=[], vault_root=vault)

        app = build_app(settings=settings, vault_root=vault)

        assert app.vault_root == vault.resolve()
