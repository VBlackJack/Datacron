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
"""Versioned Compare-And-Swap (CAS) configuration contract for CLI and Companion."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from pydantic import ValidationError

from datacron.core.config import (
    VALID_DURABILITY_MODES,
    Settings,
    VaultConfig,
    get_settings,
)
from datacron.core.durability import atomic_durable_write
from datacron.core.paths import sidecar_vault_config
from datacron.i18n import SUPPORTED_LOCALES

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ALLOWED_CLI_TIMEOUTS",
    "CONFIG_CONTRACT_VERSION",
    "EXIT_CAS_CONFLICT",
    "EXIT_INTERNAL_ERROR",
    "EXIT_LOCK_TIMEOUT",
    "EXIT_OK",
    "EXIT_VALIDATION_ERROR",
    "HASH_ABSENT",
    "CompanionProfile",
    "CompanionProfileSnapshot",
    "ConfigCasConflictError",
    "ConfigContractError",
    "ConfigValidationError",
    "VaultConfigSnapshot",
    "companion_config_path",
    "read_companion_profile",
    "read_vault_config_snapshot",
    "write_companion_profile_cas",
    "write_vault_config_cas",
]

CONFIG_CONTRACT_VERSION: Final[int] = 1
HASH_ABSENT: Final[str] = "absent"

EXIT_OK: Final[int] = 0
EXIT_INTERNAL_ERROR: Final[int] = 1
EXIT_LOCK_TIMEOUT: Final[int] = 2
EXIT_VALIDATION_ERROR: Final[int] = 6
EXIT_CAS_CONFLICT: Final[int] = 9

ALLOWED_CLI_TIMEOUTS: Final[tuple[int, ...]] = (15, 30, 60, 120)
DEFAULT_CLI_TIMEOUT_SECONDS: Final[int] = 30
ENV_COMPANION_CONFIG: Final[str] = "DATACRON_COMPANION_CONFIG"
_APP_DIR_NAME: Final[str] = "Datacron"
_COMPANION_FILENAME: Final[str] = "companion.json"


class ConfigContractError(ValueError):
    """Base error for versioned configuration contract operations."""

    exit_code: int = EXIT_INTERNAL_ERROR


class ConfigValidationError(ConfigContractError):
    """Raised when configuration values fail schema validation."""

    exit_code: int = EXIT_VALIDATION_ERROR


class ConfigCasConflictError(ConfigContractError):
    """Raised when Compare-And-Swap hash verification detects an external edit."""

    exit_code: int = EXIT_CAS_CONFLICT


@dataclass(frozen=True, slots=True)
class VaultConfigSnapshot:
    """Versioned CAS snapshot of a vault's `.datacron/VAULT.yaml`."""

    contract_version: int
    config_path: str
    present: bool
    content_hash: str
    valid: bool
    values: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize snapshot to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompanionProfile:
    """Persistent user profile for Datacron Companion."""

    vault_path: str = ""
    memory_vault_path: str = ""
    extra_read_vaults: dict[str, str] = field(default_factory=dict)
    ui_language: str = "fr"
    cli_timeout_seconds: int = DEFAULT_CLI_TIMEOUT_SECONDS
    enable_write: bool = True
    durability: str = "best-effort"


@dataclass(frozen=True, slots=True)
class CompanionProfileSnapshot:
    """Versioned CAS snapshot of the Companion user profile."""

    contract_version: int
    config_path: str
    present: bool
    content_hash: str
    valid: bool
    profile: CompanionProfile
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize snapshot to a JSON-compatible dictionary."""
        return asdict(self)


def _sha256_prefixed(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _verify_cas_precondition(
    current_hash: str,
    *,
    present: bool,
    expected_hash: str | None,
    expect_absent: bool,
) -> None:
    if expect_absent and present:
        raise ConfigCasConflictError(f"CAS conflict: expected absent file, found {current_hash}")
    if expected_hash is not None and expected_hash != current_hash:
        raise ConfigCasConflictError(
            f"CAS conflict: expected {expected_hash}, found {current_hash}"
        )


_ALLOWED_VAULT_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    set(VaultConfig.model_fields) | {"archive_tags"}
)


def _dump_vault_config_values(
    cfg: VaultConfig,
    override_archive_tags: list[str] | None = None,
) -> dict[str, Any]:
    dumped = cfg.model_dump(mode="json")
    dumped["archive_tags"] = (
        override_archive_tags if override_archive_tags is not None else sorted(cfg.archive_tags)
    )
    return dumped


def read_vault_config_snapshot(
    vault_root: Path,
    *,
    settings: Settings | None = None,
) -> VaultConfigSnapshot:
    """Read `.datacron/VAULT.yaml` and return its content hash and validated values."""
    resolved_root = vault_root.expanduser().resolve()
    active_settings = settings or get_settings()
    cfg_path = sidecar_vault_config(resolved_root, settings=active_settings)
    if not cfg_path.is_file():
        default_cfg = VaultConfig()
        return VaultConfigSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(cfg_path),
            present=False,
            content_hash=HASH_ABSENT,
            valid=True,
            values=_dump_vault_config_values(default_cfg),
            errors=[],
        )
    raw_bytes = cfg_path.read_bytes()
    content_hash = _sha256_prefixed(raw_bytes)
    try:
        decoded = yaml.safe_load(raw_bytes.decode("utf-8"))
        payload = {} if decoded is None else decoded
        if not isinstance(payload, dict):
            return VaultConfigSnapshot(
                contract_version=CONFIG_CONTRACT_VERSION,
                config_path=str(cfg_path),
                present=True,
                content_hash=content_hash,
                valid=False,
                values=_dump_vault_config_values(VaultConfig()),
                errors=["VAULT.yaml top-level value must be a mapping"],
            )
        raw_archive = payload.get("archive_tags")
        custom_archive = (
            [str(t).strip() for t in raw_archive if str(t).strip()]
            if isinstance(raw_archive, list)
            else None
        )
        validated = VaultConfig.model_validate(payload)
        return VaultConfigSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(cfg_path),
            present=True,
            content_hash=content_hash,
            valid=True,
            values=_dump_vault_config_values(validated, custom_archive),
            errors=[],
        )
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        return VaultConfigSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(cfg_path),
            present=True,
            content_hash=content_hash,
            valid=False,
            values=_dump_vault_config_values(VaultConfig()),
            errors=[str(exc)],
        )


def write_vault_config_cas(
    vault_root: Path,
    values: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    expected_hash: str | None = None,
    expect_absent: bool = False,
) -> VaultConfigSnapshot:
    """Validate and atomically write `.datacron/VAULT.yaml` under CAS protection."""
    raw_dict = dict(values)
    unknown_keys = sorted(set(raw_dict) - _ALLOWED_VAULT_CONFIG_KEYS)
    if unknown_keys:
        raise ConfigValidationError(f"Unknown VAULT.yaml fields: {unknown_keys}")
    custom_archive: list[str] | None = None
    if "archive_tags" in raw_dict:
        raw_arch = raw_dict.pop("archive_tags")
        if not isinstance(raw_arch, list) or not all(isinstance(x, str) for x in raw_arch):
            raise ConfigValidationError("archive_tags must be a list of strings")
        custom_archive = [x.strip() for x in raw_arch if x.strip()]
    try:
        validated = VaultConfig.model_validate(raw_dict)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ConfigValidationError(str(exc)) from exc

    current = read_vault_config_snapshot(vault_root, settings=settings)
    _verify_cas_precondition(
        current.content_hash,
        present=current.present,
        expected_hash=expected_hash,
        expect_absent=expect_absent,
    )
    cfg_path = Path(current.config_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    dumped_dict = _dump_vault_config_values(validated, custom_archive)
    rendered = yaml.safe_dump(
        dumped_dict,
        sort_keys=False,
        allow_unicode=True,
    )
    raw_bytes = rendered.encode("utf-8")
    atomic_durable_write(cfg_path, raw_bytes)
    return VaultConfigSnapshot(
        contract_version=CONFIG_CONTRACT_VERSION,
        config_path=str(cfg_path),
        present=True,
        content_hash=_sha256_prefixed(raw_bytes),
        valid=True,
        values=dumped_dict,
        errors=[],
    )


def companion_config_path() -> Path:
    """Resolve the persistent user profile path for Datacron Companion."""
    override = os.environ.get(ENV_COMPANION_CONFIG, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data) / _APP_DIR_NAME / _COMPANION_FILENAME).resolve()
    return (Path.home() / ".config" / "datacron" / _COMPANION_FILENAME).resolve()


def _validate_companion_profile(raw: Mapping[str, Any]) -> CompanionProfile:
    lang = str(raw.get("ui_language", "fr")).strip().lower()
    if lang not in SUPPORTED_LOCALES:
        raise ConfigValidationError(f"Unsupported ui_language: {lang!r}")
    timeout_raw = raw.get("cli_timeout_seconds", DEFAULT_CLI_TIMEOUT_SECONDS)
    if not isinstance(timeout_raw, int) or isinstance(timeout_raw, bool):
        raise ConfigValidationError("cli_timeout_seconds must be an integer")
    if timeout_raw not in ALLOWED_CLI_TIMEOUTS:
        raise ConfigValidationError(
            f"cli_timeout_seconds must be one of {ALLOWED_CLI_TIMEOUTS}; got {timeout_raw}"
        )
    durability = str(raw.get("durability", "best-effort")).strip()
    if durability not in VALID_DURABILITY_MODES:
        raise ConfigValidationError(f"Unsupported durability mode: {durability!r}")
    extra_raw = raw.get("extra_read_vaults") or {}
    if not isinstance(extra_raw, dict):
        raise ConfigValidationError("extra_read_vaults must be a mapping")
    extra_clean = {
        str(k).strip(): str(v).strip()
        for k, v in extra_raw.items()
        if str(k).strip() and str(v).strip()
    }
    return CompanionProfile(
        vault_path=str(raw.get("vault_path", "")).strip(),
        memory_vault_path=str(raw.get("memory_vault_path", "")).strip(),
        extra_read_vaults=extra_clean,
        ui_language=lang,
        cli_timeout_seconds=timeout_raw,
        enable_write=bool(raw.get("enable_write", True)),
        durability=durability,
    )


def read_companion_profile(path: Path | None = None) -> CompanionProfileSnapshot:
    """Read the persistent Companion user profile and compute its CAS hash."""
    target = (path or companion_config_path()).expanduser().resolve()
    if not target.is_file():
        return CompanionProfileSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(target),
            present=False,
            content_hash=HASH_ABSENT,
            valid=True,
            profile=CompanionProfile(),
            errors=[],
        )
    raw_bytes = target.read_bytes()
    content_hash = _sha256_prefixed(raw_bytes)
    try:
        decoded = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ConfigValidationError("Companion profile root must be a JSON object")
        profile = _validate_companion_profile(decoded)
        return CompanionProfileSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(target),
            present=True,
            content_hash=content_hash,
            valid=True,
            profile=profile,
            errors=[],
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ConfigValidationError) as exc:
        return CompanionProfileSnapshot(
            contract_version=CONFIG_CONTRACT_VERSION,
            config_path=str(target),
            present=True,
            content_hash=content_hash,
            valid=False,
            profile=CompanionProfile(),
            errors=[str(exc)],
        )


def write_companion_profile_cas(
    values: Mapping[str, Any],
    *,
    path: Path | None = None,
    expected_hash: str | None = None,
    expect_absent: bool = False,
) -> CompanionProfileSnapshot:
    """Validate and atomically write the Companion user profile under CAS protection."""
    profile = _validate_companion_profile(values)
    current = read_companion_profile(path)
    _verify_cas_precondition(
        current.content_hash,
        present=current.present,
        expected_hash=expected_hash,
        expect_absent=expect_absent,
    )
    target = Path(current.config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(asdict(profile), indent=2, sort_keys=True) + "\n"
    raw_bytes = rendered.encode("utf-8")
    atomic_durable_write(target, raw_bytes)
    return CompanionProfileSnapshot(
        contract_version=CONFIG_CONTRACT_VERSION,
        config_path=str(target),
        present=True,
        content_hash=_sha256_prefixed(raw_bytes),
        valid=True,
        profile=profile,
        errors=[],
    )
