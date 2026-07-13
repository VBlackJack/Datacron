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
"""Runtime configuration loaded from environment variables and ``.env``.

Reserved runtime keys must use the ``DATACRON_`` prefix. The
:func:`get_settings` accessor returns a cached singleton; tests may override it
via :func:`reset_settings_cache`.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from datacron.core.query_expansion import default_query_expansion, normalize_term_map

DEFAULT_LOG_LEVEL: Final[str] = "INFO"
DEFAULT_LOG_DIR: Final[Path] = Path.home() / ".datacron" / "logs"
DEFAULT_MAX_RESULT_TOKENS: Final[int] = 8000
DEFAULT_MAX_RESULT_COUNT: Final[int] = 20
TOKEN_ESTIMATE_CHARS_PER_TOKEN: Final[int] = 4
TEMPORAL_OVERFETCH_FACTOR: Final[int] = 3
SUPERSEDED_DEMOTION_FACTOR: Final[float] = 0.1
CONFIDENCE_PENALTY: Final[dict[str, float]] = {"low": 0.7, "needs_verification": 0.5}
DEFAULT_RIPGREP_PATH: Final[str] = "rg"
DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH: Final[int] = 512
DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS: Final[float] = 2.0
# Bounded wait for a contended vault advisory lock (and the sidecar index
# busy-wait) before giving up. Mirrors the historical 5s SQLite busy timeout so
# a single source of truth governs "how long to wait on a busy vault resource".
DEFAULT_VAULT_LOCK_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_CHUNK_MAX_TOKENS: Final[int] = 1024
# get_note(full) budget, decoupled from the search budget (max_result_tokens).
# Search returns many snippets and must stay bounded; reading one note can be
# generous, so a single get_note returns most notes whole while pagination
# (offset/limit/next_offset) remains the safety valve for pathologically large notes.
DEFAULT_GET_NOTE_MAX_TOKENS: Final[int] = 25000
DEFAULT_HISTORY_RETENTION_DAYS: Final[int] = 30
DEFAULT_HISTORY_MODE: Final[str] = "full"
DEFAULT_REDACT_SECRETS: Final[str] = "all"
DEFAULT_DURABILITY_MODE: Final[str] = "best-effort"
DEFAULT_SCRUB_NOTES_PER_SECOND: Final[float] = 50.0
DEFAULT_SCRUB_MEBIBYTES_PER_SECOND: Final[float] = 16.0
DEFAULT_SCRUB_MAX_DURATION_SECONDS: Final[float] = 30.0
DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES: Final[int] = 25
DEFAULT_SCRUB_CHECKPOINT_PATH: Final[Path] = Path(".datacron/scrubber/checkpoint.json")
DEFAULT_SCRUB_CANARY_DIR: Final[Path] = Path(".datacron/scrubber/canaries")
DEFAULT_SCRUB_CANARIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "exact-byte-lf.md",
        "# Datacron integrity canary\n\nformat: utf-8-lf\nsequence: 0123456789abcdef\n",
    ),
    (
        "exact-byte-crlf.md",
        "# Datacron integrity canary\r\n\r\nformat: utf-8-crlf\r\nsequence: fedcba9876543210\r\n",
    ),
)
DEFAULT_EXCLUDED_FOLDERS: Final[tuple[str, ...]] = (
    "_attachments",
    "zzz_Corbeille",
    "_trash",
    "_archive",
)
DEFAULT_EXCLUDED_FILES: Final[tuple[str, ...]] = ("00_INDEX.md",)

SIDECAR_DIR_NAME: Final[str] = ".datacron"
INDEX_DIR_NAME: Final[str] = "index"
INDEX_DB_FILENAME: Final[str] = "datacron.db"
HISTORY_DIR_NAME: Final[str] = "history"
OPLOG_DIR_NAME: Final[str] = "oplog"
OPLOG_PENDING_DIR_NAME: Final[str] = "pending"
VAULT_CONFIG_FILENAME: Final[str] = "VAULT.yaml"
LOG_FILENAME_PATTERN: Final[str] = "datacron_{date}.log"
LOG_FORMAT: Final[str] = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
VALID_SECRET_REDACTION_POLICIES: Final[frozenset[str]] = frozenset(
    {"off", "log", "retrieval", "all"}
)
VALID_DURABILITY_MODES: Final[frozenset[str]] = frozenset({"strict", "best-effort"})


class VaultConfig(BaseModel):
    """Typed model for ``.datacron/VAULT.yaml``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    datacron_version: str | None = None
    vault_id: str | None = None
    created: str | None = None
    encoding: str = "utf-8"
    line_endings: str = "lf"
    history_retention_days: int = Field(default=DEFAULT_HISTORY_RETENTION_DAYS, ge=1)
    history_mode: str = DEFAULT_HISTORY_MODE
    folders: dict[str, str] = Field(default_factory=dict)
    excluded_folders: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FOLDERS))
    excluded_files: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FILES))
    query_expansion: dict[str, list[str]] = Field(default_factory=default_query_expansion)

    @field_validator("line_endings", mode="before")
    @classmethod
    def _normalize_line_endings(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"lf", "crlf"}:
            raise ValueError("line_endings must be 'lf' or 'crlf'")
        return normalized

    @field_validator("history_mode", mode="before")
    @classmethod
    def _normalize_history_mode(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"full", "redacted"}:
            raise ValueError("history_mode must be 'full' or 'redacted'")
        return normalized

    @field_validator("excluded_folders", mode="before")
    @classmethod
    def _normalize_excluded_folders(cls, value: object) -> list[str]:
        if value is None or value == "":
            return list(DEFAULT_EXCLUDED_FOLDERS)
        if not isinstance(value, list):
            raise TypeError("excluded_folders must be a list of folder names")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("excluded_files", mode="before")
    @classmethod
    def _normalize_excluded_files(cls, value: object) -> list[str]:
        if value is None or value == "":
            return list(DEFAULT_EXCLUDED_FILES)
        if not isinstance(value, list):
            raise TypeError("excluded_files must be a list of file names")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("query_expansion", mode="before")
    @classmethod
    def _normalize_query_expansion(cls, value: object) -> dict[str, list[str]]:
        if value is None or value == "":
            return default_query_expansion()
        if not isinstance(value, dict):
            raise TypeError("query_expansion must be a mapping of terms to term lists")
        raw_map: dict[str, list[str]] = {}
        for raw_term, raw_equivalents in value.items():
            if not isinstance(raw_equivalents, list):
                raise TypeError("query_expansion values must be lists of terms")
            term = str(raw_term).strip()
            if not term:
                continue
            raw_map[term] = [str(item).strip() for item in raw_equivalents if str(item).strip()]
        return normalize_term_map(raw_map)


def load_vault_config(path: Path) -> VaultConfig | None:
    """Load ``.datacron/VAULT.yaml`` from ``path`` if it exists."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping; found {type(data).__name__}.")
    return VaultConfig.model_validate(data)


def _split_path_list(value: str | list[str | Path] | None) -> list[Path]:
    """Parse the OS-dependent path separator into a list of resolved Paths.

    Empty entries are dropped. ``~`` is expanded. Each entry is resolved to an
    absolute path. The input may already be a list (for programmatic
    construction in tests).
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts: list[str] = [p for p in value.split(os.pathsep) if p.strip()]
        return [Path(p).expanduser().resolve() for p in parts]
    return [Path(p).expanduser().resolve() for p in value]


@final
class Settings(BaseSettings):
    """Datacron runtime settings.

    Loaded from environment variables prefixed ``DATACRON_`` and an optional
    ``.env`` file in the current working directory. All reserved runtime keys
    use the ``DATACRON_`` namespace.
    """

    model_config = SettingsConfigDict(
        env_prefix="DATACRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    log_level: str = Field(default=DEFAULT_LOG_LEVEL)
    log_dir: Path = Field(default=DEFAULT_LOG_DIR)
    read_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    write_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    vault_root: Path | None = Field(default=None)
    max_result_tokens: int = Field(default=DEFAULT_MAX_RESULT_TOKENS, ge=1)
    max_result_count: int = Field(default=DEFAULT_MAX_RESULT_COUNT, ge=1)
    ripgrep_path: str = Field(default=DEFAULT_RIPGREP_PATH)
    regex_fallback_max_pattern_length: int = Field(
        default=DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH,
        ge=1,
    )
    regex_fallback_timeout_seconds: float = Field(
        default=DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS,
        gt=0,
    )
    vault_lock_timeout_seconds: float = Field(
        default=DEFAULT_VAULT_LOCK_TIMEOUT_SECONDS,
        gt=0,
    )
    chunk_max_tokens: int = Field(default=DEFAULT_CHUNK_MAX_TOKENS, ge=1)
    get_note_max_tokens: int = Field(default=DEFAULT_GET_NOTE_MAX_TOKENS, ge=1)
    redact_secrets: str = DEFAULT_REDACT_SECRETS
    secret_redaction_patterns: Annotated[list[str], NoDecode] = Field(default_factory=list)
    read_only: bool = False
    durability: str = DEFAULT_DURABILITY_MODE
    scrub_notes_per_second: float = Field(default=DEFAULT_SCRUB_NOTES_PER_SECOND, gt=0)
    scrub_mebibytes_per_second: float = Field(
        default=DEFAULT_SCRUB_MEBIBYTES_PER_SECOND,
        gt=0,
    )
    scrub_max_duration_seconds: float = Field(
        default=DEFAULT_SCRUB_MAX_DURATION_SECONDS,
        gt=0,
    )
    scrub_checkpoint_interval_notes: int = Field(
        default=DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES,
        ge=1,
    )
    scrub_checkpoint_path: Path = DEFAULT_SCRUB_CHECKPOINT_PATH
    scrub_canary_dir: Path = DEFAULT_SCRUB_CANARY_DIR
    scrub_canaries: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_SCRUB_CANARIES)
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        if not isinstance(value, str):
            return DEFAULT_LOG_LEVEL
        normalized = value.strip().upper()
        if normalized not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Invalid DATACRON_LOG_LEVEL: {value!r}. "
                f"Expected one of {sorted(VALID_LOG_LEVELS)}."
            )
        return normalized

    @field_validator("log_dir", mode="before")
    @classmethod
    def _expand_log_dir(cls, value: object) -> Path:
        if value is None or value == "":
            return DEFAULT_LOG_DIR
        return Path(str(value)).expanduser()

    @field_validator("read_paths", mode="before")
    @classmethod
    def _parse_read_paths(cls, value: object) -> list[Path]:
        if value is None or isinstance(value, str):
            return _split_path_list(value)
        if isinstance(value, list):
            return _split_path_list(value)
        raise TypeError(f"Unsupported type for read_paths: {type(value).__name__}")

    @field_validator("write_paths", mode="before")
    @classmethod
    def _parse_write_paths(cls, value: object) -> list[Path]:
        if value is None or isinstance(value, str):
            return _split_path_list(value)
        if isinstance(value, list):
            return _split_path_list(value)
        raise TypeError(f"Unsupported type for write_paths: {type(value).__name__}")

    @field_validator("vault_root", mode="before")
    @classmethod
    def _expand_vault_root(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser().resolve()

    @field_validator("redact_secrets", mode="before")
    @classmethod
    def _normalize_redact_secrets(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_SECRET_REDACTION_POLICIES:
            raise ValueError(
                f"DATACRON_REDACT_SECRETS must be one of {sorted(VALID_SECRET_REDACTION_POLICIES)}"
            )
        return normalized

    @field_validator("secret_redaction_patterns", mode="before")
    @classmethod
    def _parse_secret_redaction_patterns(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "DATACRON_SECRET_REDACTION_PATTERNS must be a JSON string list"
                ) from exc
            value = decoded
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError("secret_redaction_patterns must be a list of regex strings")
        patterns = [item for item in value if item]
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid secret redaction regex: {pattern!r}") from exc
        return patterns

    @field_validator("durability", mode="before")
    @classmethod
    def _normalize_durability(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_DURABILITY_MODES:
            raise ValueError(f"DATACRON_DURABILITY must be one of {sorted(VALID_DURABILITY_MODES)}")
        return normalized

    @field_validator("scrub_checkpoint_path", "scrub_canary_dir", mode="before")
    @classmethod
    def _normalize_scrub_relative_path(cls, value: object) -> Path:
        path = Path(str(value))
        if path.is_absolute() or path.drive or ".." in path.parts or path == Path("."):
            raise ValueError("scrubber paths must be non-empty vault-relative paths")
        return path

    @field_validator("scrub_canaries", mode="before")
    @classmethod
    def _parse_scrub_canaries(cls, value: object) -> dict[str, str]:
        if value is None or value == "":
            return dict(DEFAULT_SCRUB_CANARIES)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("DATACRON_SCRUB_CANARIES must be a JSON string mapping") from exc
        if not isinstance(value, dict) or not value:
            raise TypeError("scrub_canaries must be a non-empty mapping of paths to content")
        canaries: dict[str, str] = {}
        for raw_name, raw_content in value.items():
            if not isinstance(raw_name, str) or not isinstance(raw_content, str):
                raise TypeError("scrub_canaries keys and values must be strings")
            name = Path(raw_name)
            if name.is_absolute() or name.drive or ".." in name.parts or name == Path("."):
                raise ValueError("scrub canary names must be safe relative paths")
            canaries[name.as_posix()] = raw_content
        return canaries


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    The result is cached. Tests should call :func:`reset_settings_cache` after
    mutating ``os.environ`` so the next call re-reads the environment.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the :func:`get_settings` cache.

    Intended for test setup/teardown only.
    """
    get_settings.cache_clear()
