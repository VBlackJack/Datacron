# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.config`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from datacron.core.config import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_RESULT_COUNT,
    DEFAULT_MAX_RESULT_TOKENS,
    DEFAULT_RIPGREP_PATH,
    Settings,
    get_settings,
    reset_settings_cache,
)


class TestDefaults:
    def test_defaults_match_contract(self) -> None:
        settings = Settings()
        assert settings.log_level == DEFAULT_LOG_LEVEL
        assert settings.max_result_tokens == DEFAULT_MAX_RESULT_TOKENS
        assert settings.max_result_count == DEFAULT_MAX_RESULT_COUNT
        assert settings.ripgrep_path == DEFAULT_RIPGREP_PATH
        assert settings.chunk_max_tokens == DEFAULT_CHUNK_MAX_TOKENS
        assert settings.read_paths == []
        assert settings.vault_root is None


class TestEnvLoading:
    def test_env_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATACRON_LOG_LEVEL", "debug")
        settings = Settings()
        assert settings.log_level == "DEBUG"

    def test_invalid_log_level_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATACRON_LOG_LEVEL", "TRACE")
        with pytest.raises(ValidationError):
            Settings()

    def test_read_paths_split_by_os_sep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        raw = os.pathsep.join([str(first), str(second)])
        monkeypatch.setenv("DATACRON_READ_PATHS", raw)
        settings = Settings()
        assert settings.read_paths == [first.resolve(), second.resolve()]

    def test_read_paths_ignore_empty_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        only = tmp_path / "only"
        only.mkdir()
        monkeypatch.setenv("DATACRON_READ_PATHS", os.pathsep + str(only) + os.pathsep)
        settings = Settings()
        assert settings.read_paths == [only.resolve()]

    def test_vault_root_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("DATACRON_VAULT_ROOT", str(tmp_path))
        settings = Settings()
        assert settings.vault_root == tmp_path.resolve()


class TestProgrammatic:
    def test_construct_with_list(self, tmp_path: Path) -> None:
        settings = Settings(read_paths=[tmp_path])
        assert settings.read_paths == [tmp_path.resolve()]

    def test_max_result_count_positive(self) -> None:
        with pytest.raises(ValidationError):
            Settings(max_result_count=0)


class TestSingleton:
    def test_cache_hit(self) -> None:
        reset_settings_cache()
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_cache_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_settings_cache()
        first = get_settings()
        monkeypatch.setenv("DATACRON_MAX_RESULT_COUNT", "5")
        reset_settings_cache()
        second = get_settings()
        assert first is not second
        assert second.max_result_count == 5
