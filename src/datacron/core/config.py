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

All reserved keys are defined in ``docs/agent-briefs/01-contracts.md`` §4 and
must use the ``DATACRON_`` prefix. The :func:`get_settings` accessor returns a
cached singleton; tests may override it via :func:`reset_settings_cache`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_LOG_LEVEL: Final[str] = "INFO"
DEFAULT_LOG_DIR: Final[Path] = Path.home() / ".datacron" / "logs"
DEFAULT_MAX_RESULT_TOKENS: Final[int] = 8000
DEFAULT_MAX_RESULT_COUNT: Final[int] = 20
DEFAULT_RIPGREP_PATH: Final[str] = "rg"
DEFAULT_CHUNK_MAX_TOKENS: Final[int] = 1024
# get_note(full) budget, decoupled from the search budget (max_result_tokens).
# Search returns many snippets and must stay bounded; reading one note can be
# generous, so a single get_note returns most notes whole while pagination
# (offset/limit/next_offset) remains the safety valve for pathologically large notes.
DEFAULT_GET_NOTE_MAX_TOKENS: Final[int] = 25000
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
VAULT_CONFIG_FILENAME: Final[str] = "VAULT.yaml"
LOG_FILENAME_PATTERN: Final[str] = "datacron_{date}.log"
LOG_FORMAT: Final[str] = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class VaultConfig(BaseModel):
    """Typed model for ``.datacron/VAULT.yaml``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    datacron_version: str | None = None
    vault_id: str | None = None
    created: str | None = None
    encoding: str = "utf-8"
    line_endings: str = "lf"
    folders: dict[str, str] = Field(default_factory=dict)
    excluded_folders: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FOLDERS))
    excluded_files: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FILES))

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
    ``.env`` file in the current working directory. All reserved keys are
    listed in ``docs/agent-briefs/01-contracts.md`` §4.
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
    vault_root: Path | None = Field(default=None)
    max_result_tokens: int = Field(default=DEFAULT_MAX_RESULT_TOKENS, ge=1)
    max_result_count: int = Field(default=DEFAULT_MAX_RESULT_COUNT, ge=1)
    ripgrep_path: str = Field(default=DEFAULT_RIPGREP_PATH)
    chunk_max_tokens: int = Field(default=DEFAULT_CHUNK_MAX_TOKENS, ge=1)
    get_note_max_tokens: int = Field(default=DEFAULT_GET_NOTE_MAX_TOKENS, ge=1)

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

    @field_validator("vault_root", mode="before")
    @classmethod
    def _expand_vault_root(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser().resolve()


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
