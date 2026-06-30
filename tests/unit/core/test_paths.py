# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.paths`."""

from __future__ import annotations

from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.paths import (
    PathConfinementError,
    assert_within_paths,
    assert_within_read_paths,
    assert_within_write_paths,
    is_within,
    sidecar_dir,
    sidecar_index_db,
    sidecar_index_dir,
    sidecar_vault_config,
)


class TestIsWithin:
    def test_match(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b.txt"
        nested.parent.mkdir(parents=True)
        nested.write_text("x", encoding="utf-8")
        assert is_within(nested, tmp_path)

    def test_outside(self, tmp_path: Path) -> None:
        outside = tmp_path / "a"
        sibling = tmp_path / "b"
        outside.mkdir()
        sibling.mkdir()
        assert not is_within(sibling, outside)


class TestAssertWithinPaths:
    def test_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "ok.md"
        target.write_text("hi", encoding="utf-8")
        resolved = assert_within_paths(target, [tmp_path], kind="read")
        assert resolved == target.resolve()

    def test_rejected(self, tmp_path: Path) -> None:
        outside = Path("/").resolve()
        with pytest.raises(PathConfinementError):
            assert_within_paths(outside, [tmp_path])

    def test_empty_roots(self, tmp_path: Path) -> None:
        with pytest.raises(PathConfinementError):
            assert_within_paths(tmp_path, [])


class TestSettingsBacked:
    def test_read_paths(self, tmp_path: Path) -> None:
        settings = Settings(read_paths=[tmp_path])
        nested = tmp_path / "note.md"
        nested.write_text("x", encoding="utf-8")
        assert assert_within_read_paths(nested, settings=settings) == nested.resolve()

    def test_write_paths_empty_denies_all(self, tmp_path: Path) -> None:
        settings = Settings(write_paths=[])
        with pytest.raises(PathConfinementError, match="No write paths are configured"):
            assert_within_write_paths(tmp_path / "anywhere.md", settings=settings)

    def test_write_paths_allowed(self, tmp_path: Path) -> None:
        settings = Settings(write_paths=[tmp_path])
        nested = tmp_path / "note.md"
        assert assert_within_write_paths(nested, settings=settings) == nested.resolve()

    def test_write_paths_rejected_outside_roots(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        settings = Settings(write_paths=[allowed])
        with pytest.raises(PathConfinementError, match="outside the allowed write roots"):
            assert_within_write_paths(outside / "note.md", settings=settings)


class TestSidecarHelpers:
    def test_layout(self, tmp_path: Path) -> None:
        assert sidecar_dir(tmp_path) == tmp_path.resolve() / ".datacron"
        assert sidecar_index_dir(tmp_path) == tmp_path.resolve() / ".datacron" / "index"
        assert (
            sidecar_index_db(tmp_path) == tmp_path.resolve() / ".datacron" / "index" / "datacron.db"
        )
        assert sidecar_vault_config(tmp_path) == tmp_path.resolve() / ".datacron" / "VAULT.yaml"
