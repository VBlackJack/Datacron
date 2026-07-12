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
    DEFAULT_DURABILITY_MODE,
    DEFAULT_EXCLUDED_FILES,
    DEFAULT_EXCLUDED_FOLDERS,
    DEFAULT_GET_NOTE_MAX_TOKENS,
    DEFAULT_HISTORY_MODE,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_RESULT_COUNT,
    DEFAULT_MAX_RESULT_TOKENS,
    DEFAULT_RIPGREP_PATH,
    DEFAULT_SCRUB_CANARIES,
    DEFAULT_SCRUB_CANARY_DIR,
    DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES,
    DEFAULT_SCRUB_CHECKPOINT_PATH,
    DEFAULT_SCRUB_MAX_DURATION_SECONDS,
    DEFAULT_SCRUB_MEBIBYTES_PER_SECOND,
    DEFAULT_SCRUB_NOTES_PER_SECOND,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
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
        assert settings.get_note_max_tokens == DEFAULT_GET_NOTE_MAX_TOKENS
        assert settings.read_paths == []
        assert settings.write_paths == []
        assert settings.vault_root is None
        assert settings.read_only is False
        assert settings.durability == DEFAULT_DURABILITY_MODE
        assert settings.scrub_notes_per_second == DEFAULT_SCRUB_NOTES_PER_SECOND
        assert settings.scrub_mebibytes_per_second == DEFAULT_SCRUB_MEBIBYTES_PER_SECOND
        assert settings.scrub_max_duration_seconds == DEFAULT_SCRUB_MAX_DURATION_SECONDS
        assert settings.scrub_checkpoint_interval_notes == DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES
        assert settings.scrub_checkpoint_path == DEFAULT_SCRUB_CHECKPOINT_PATH
        assert settings.scrub_canary_dir == DEFAULT_SCRUB_CANARY_DIR
        assert settings.scrub_canaries == dict(DEFAULT_SCRUB_CANARIES)

    def test_vault_config_excluded_folders_default(self) -> None:
        config = VaultConfig()
        assert config.excluded_folders == list(DEFAULT_EXCLUDED_FOLDERS)
        assert config.excluded_files == list(DEFAULT_EXCLUDED_FILES)
        assert config.query_expansion["supervision"] == ["monitoring"]
        assert config.query_expansion["monitoring"] == ["supervision"]
        assert config.query_expansion["validit\u00e9"] == ["validity"]
        assert config.query_expansion["certificate"] == ["certificat"]
        assert config.history_retention_days == DEFAULT_HISTORY_RETENTION_DAYS
        assert config.history_mode == DEFAULT_HISTORY_MODE


class TestEnvLoading:
    def test_env_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATACRON_LOG_LEVEL", "debug")
        settings = Settings()
        assert settings.log_level == "DEBUG"

    def test_invalid_log_level_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATACRON_LOG_LEVEL", "TRACE")
        with pytest.raises(ValidationError):
            Settings()

    def test_env_get_note_max_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATACRON_GET_NOTE_MAX_TOKENS", "12345")
        settings = Settings()
        assert settings.get_note_max_tokens == 12345

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

    def test_write_paths_split_by_os_sep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        raw = os.pathsep.join([str(first), str(second)])
        monkeypatch.setenv("DATACRON_WRITE_PATHS", raw)
        settings = Settings()
        assert settings.write_paths == [first.resolve(), second.resolve()]

    def test_vault_root_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("DATACRON_VAULT_ROOT", str(tmp_path))
        settings = Settings()
        assert settings.vault_root == tmp_path.resolve()

    def test_read_only_and_strict_durability_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DATACRON_READ_ONLY", "true")
        monkeypatch.setenv("DATACRON_DURABILITY", "STRICT")
        settings = Settings()
        assert settings.read_only is True
        assert settings.durability == "strict"

    def test_invalid_durability_mode_raises(self) -> None:
        with pytest.raises(ValidationError, match="DATACRON_DURABILITY"):
            Settings(durability="eventually")

    def test_scrub_canaries_load_from_json_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DATACRON_SCRUB_CANARIES", '{"sentinel.md":"known\\r\\n"}')
        settings = Settings()
        assert settings.scrub_canaries == {"sentinel.md": "known\r\n"}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("scrub_checkpoint_path", "../checkpoint.json"),
            ("scrub_canary_dir", None),
            ("scrub_canaries", {"../outside.md": "unsafe"}),
            ("scrub_notes_per_second", 0),
            ("scrub_max_duration_seconds", 0),
        ],
    )
    def test_scrub_settings_reject_unsafe_or_zero_values(
        self,
        tmp_path: Path,
        field: str,
        value: object,
    ) -> None:
        if field == "scrub_canary_dir":
            value = tmp_path.resolve()
        with pytest.raises(ValidationError):
            Settings.model_validate({field: value})


class TestProgrammatic:
    def test_construct_with_list(self, tmp_path: Path) -> None:
        settings = Settings(read_paths=[tmp_path])
        assert settings.read_paths == [tmp_path.resolve()]

    def test_max_result_count_positive(self) -> None:
        with pytest.raises(ValidationError):
            Settings(max_result_count=0)


class TestVaultConfig:
    @pytest.mark.parametrize("mode", ["full", "redacted"])
    def test_history_mode_accepts_declared_policies(self, mode: str) -> None:
        assert VaultConfig(history_mode=mode.upper()).history_mode == mode

    def test_history_mode_rejects_unknown_policy(self) -> None:
        with pytest.raises(ValidationError, match="history_mode"):
            VaultConfig(history_mode="encrypted-without-a-key-policy")

    def test_history_retention_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="history_retention_days"):
            VaultConfig(history_retention_days=0)

    def test_load_vault_config_applies_excluded_folder_default(self, tmp_path: Path) -> None:
        path = tmp_path / "VAULT.yaml"
        path.write_text("vault_id: 01HQ\n", encoding="utf-8")

        config = load_vault_config(path)

        assert config is not None
        assert config.vault_id == "01HQ"
        assert config.excluded_folders == list(DEFAULT_EXCLUDED_FOLDERS)
        assert config.excluded_files == list(DEFAULT_EXCLUDED_FILES)

    def test_load_vault_config_reads_exclusions(self, tmp_path: Path) -> None:
        path = tmp_path / "VAULT.yaml"
        path.write_text(
            """
excluded_folders:
  - _attachments
  - custom-trash
excluded_files:
  - 00_INDEX.md
  - custom-index.md
""".lstrip(),
            encoding="utf-8",
        )

        config = load_vault_config(path)

        assert config is not None
        assert config.excluded_folders == ["_attachments", "custom-trash"]
        assert config.excluded_files == ["00_INDEX.md", "custom-index.md"]

    def test_load_vault_config_symmetrizes_query_expansion(self, tmp_path: Path) -> None:
        path = tmp_path / "VAULT.yaml"
        path.write_text(
            """
query_expansion:
  supervision:
    - monitoring
""".lstrip(),
            encoding="utf-8",
        )

        config = load_vault_config(path)

        assert config is not None
        assert config.query_expansion["supervision"] == ["monitoring"]
        assert config.query_expansion["monitoring"] == ["supervision"]

    def test_load_vault_config_keeps_explicit_empty_query_expansion(self, tmp_path: Path) -> None:
        path = tmp_path / "VAULT.yaml"
        path.write_text("query_expansion: {}\n", encoding="utf-8")

        config = load_vault_config(path)

        assert config is not None
        assert config.query_expansion == {}


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
