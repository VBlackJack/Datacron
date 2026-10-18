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
"""Backend API controller for the Datacron Companion graphical control panel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from datacron import __version__
from datacron.bootstrap import initialize_vault
from datacron.config_contract import (
    ALLOWED_CLI_TIMEOUTS,
    CompanionProfile,
    CompanionProfileSnapshot,
    ConfigCasConflictError,
    ConfigValidationError,
    VaultConfigSnapshot,
    read_companion_profile,
    read_vault_config_snapshot,
    write_companion_profile_cas,
    write_vault_config_cas,
)
from datacron.core.config import (
    VALID_DURABILITY_MODES,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
    validate_extra_vault_prefix,
)
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.core.scope import admits_note_path
from datacron.core.security import SecretRedactor
from datacron.core.vault import build_configured_reader
from datacron.graph_health import analyze_vault_graph
from datacron.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, t
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.indexing.reconcile import reconcile
from datacron.installers.mcp_clients import (
    SCOPE_PROJECT,
    SCOPE_USER,
    InstallOutcome,
    discover_targets,
)
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import list_notes_impl
from datacron.mcp.tools.search import search_regex_impl, search_text_impl
from datacron.memory_compaction import DEFAULT_COMPACTION_DAYS, compact_vault_memory
from datacron.migration import MigrationError, MigrationResult, migrate_vault_to_dual
from datacron.setup_wizard import (
    CLIENT_ALL,
    CLIENT_CHOICES,
    CLIENT_NONE,
    INSTALL_SCOPE_CHOICES,
    INSTALL_SCOPE_PROJECT,
    INSTALL_SCOPE_USER,
    SetupPlan,
    default_write_paths,
    existing_client_settings,
    run_setup,
)

__all__ = [
    "companion_i18n_bundle",
    "companion_locale",
    "get_companion_graph_health",
    "get_companion_history",
    "get_companion_state",
    "list_companion_notes",
    "run_companion_compaction",
    "run_companion_graph_health",
    "run_companion_mcp_setup",
    "run_companion_memory_compaction",
    "run_companion_migration",
    "run_companion_search",
    "run_companion_sync",
    "save_companion_preferences",
    "save_companion_topology",
    "save_vault_config",
    "search_companion",
]

_MAX_HISTORY_RECORDS: Final[int] = 25
_NOTES_DEFAULT_LIMIT: Final[int] = 50
_NOTES_MAX_LIMIT: Final[int] = 200
_NOTES_MAX_OFFSET: Final[int] = 100_000
_SEARCH_DEFAULT_LIMIT: Final[int] = 15
_SEARCH_MAX_LIMIT: Final[int] = 100
_HISTORY_MAX_LIMIT: Final[int] = 200
_COMPACTION_MAX_DAYS: Final[int] = 3650
# A profile written by somebody else while a migration ran is re-read and merged once more
# before the migration reports the profile as not updated.
_PROFILE_WRITE_ATTEMPTS: Final[int] = 2
_PREFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"ui_language", "cli_timeout_seconds", "durability"}
)
_GLOB_METACHARACTERS: Final[str] = "*?["
_MEMORY_SOURCE_PROFILE: Final[str] = "profile"
_MEMORY_SOURCE_ENVIRONMENT: Final[str] = "environment"
_MEMORY_SOURCE_NONE: Final[str] = "none"


def companion_locale(profile_path: Path | None = None) -> str:
    """Return the Companion user's UI language, falling back to the default locale."""
    try:
        lang = read_companion_profile(profile_path).profile.ui_language
    except (OSError, ValueError):
        return DEFAULT_LOCALE
    return lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _parse_bounded_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
    locale: str = DEFAULT_LOCALE,
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(t("companion.errors.integer_required", locale=locale, field=field_name))
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            t("companion.errors.integer_required", locale=locale, field=field_name)
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(
            t(
                "companion.errors.integer_range",
                locale=locale,
                field=field_name,
                minimum=minimum,
                maximum=maximum,
            )
        )
    return parsed


_COMPANION_I18N_SECTIONS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "app",
        ("title", "subtitle", "connected", "refresh", "save", "language_label", "busy"),
    ),
    (
        "nav",
        ("home", "vaults", "search", "graph", "mcp", "history", "settings"),
    ),
    (
        "home",
        (
            "headline",
            "status_ready",
            "status_unconfigured",
            "status_dual",
            "status_single",
            "cta_configure",
            "cta_sync",
            "cta_search",
            "card_notes",
            "card_chunks",
            "card_generation",
            "card_mode",
            "getting_started_title",
            "step1_title",
            "step2_title",
            "step3_title",
            "step4_title",
        ),
    ),
    (
        "vaults",
        (
            "headline",
            "docs_vault_label",
            "memory_vault_label",
            "memory_from_env_hint",
            "extra_vaults_label",
            "save_topology",
            "sync_now",
            "rebuild_now",
            "migrate_title",
            "migrate_desc",
            "migrate_keep_source",
            "migrate_reconfigure_clients",
            "migrate_button",
            "migrate_cleanup_confirm",
            "migrate_clients_reminder",
            "cleanup_only_button",
            "cleanup_only_confirm",
            "result_verified",
            "result_resumed",
            "result_indexed",
            "result_source_cleaned",
            "result_source_kept",
            "result_clients",
            "result_warnings",
            "result_mode",
            "result_stats",
        ),
    ),
    (
        "search",
        (
            "headline",
            "query_placeholder",
            "folder_placeholder",
            "mode_label",
            "mode_fts",
            "mode_regex",
            "run_button",
            "clear_button",
            "empty_state",
            "preview_title",
        ),
    ),
    (
        "graph",
        (
            "headline",
            "analyze_button",
            "summary_title",
            "total_notes",
            "total_links",
            "resolved_links",
            "broken_links_title",
            "orphan_notes_title",
            "hub_notes_title",
            "cross_vault_title",
            "none_found",
            "compaction_title",
            "compaction_days_label",
            "compaction_preview_btn",
            "compaction_apply_btn",
            "compaction_apply_confirm",
        ),
    ),
    (
        "mcp",
        (
            "headline",
            "client_label",
            "scope_label",
            "enable_write_label",
            "configure_button",
            "status_configured",
            "status_not_configured",
            "status_installed",
            "status_failed",
            "errors_title",
        ),
    ),
    (
        "history",
        ("headline", "reload_button", "empty_log"),
    ),
    (
        "settings",
        (
            "headline",
            "excluded_folders_label",
            "archive_tags_label",
            "query_expansion_label",
            "durability_label",
            "timeout_label",
            "save_vault_config_btn",
            "cas_hash_label",
            "config_valid",
            "config_invalid",
            "config_absent",
            "errors_title",
            "reset_label",
            "reset_confirm",
            "query_expansion_invalid",
            "saved",
            "nothing_to_save",
        ),
    ),
)


def companion_i18n_bundle(lang: str) -> dict[str, dict[str, str]]:
    """Build the localized string dictionary for the Companion frontend."""
    bundle: dict[str, dict[str, str]] = {}
    for section, keys in _COMPANION_I18N_SECTIONS:
        bundle[section] = {}
        for key in keys:
            full_key = f"companion.{section}.{key}"
            if full_key == "companion.app.connected":
                bundle[section][key] = t(full_key, locale=lang, version=__version__)
            else:
                bundle[section][key] = t(full_key, locale=lang)
    return bundle


def _resolve_runtime_roots(
    profile: CompanionProfile,
    env_settings: Settings,
) -> tuple[Path | None, Path | None, str]:
    """Resolve the docs vault, the memory vault and where the memory vault came from.

    The environment memory root follows the server's rule (core.paths.effective_memory_root):
    it applies when the environment sets no vault root, or sets the vault this profile
    uses. Bound to a foreign vault it would point that vault's sidecar and index at an
    unrelated memory vault, so an empty profile memory field then means Single-Vault.
    """
    env_vault = env_settings.vault_root.expanduser().resolve() if env_settings.vault_root else None
    vault_str = profile.vault_path or (str(env_vault) if env_vault else "")
    if not vault_str:
        return None, None, _MEMORY_SOURCE_NONE
    vault_root = Path(vault_str).expanduser().resolve()
    memory_root: Path | None = None
    source = _MEMORY_SOURCE_NONE
    if profile.memory_vault_path:
        memory_root = Path(profile.memory_vault_path).expanduser().resolve()
        source = _MEMORY_SOURCE_PROFILE
    elif env_settings.memory_root is not None and env_vault in (None, vault_root):
        memory_root = env_settings.memory_root.expanduser().resolve()
        source = _MEMORY_SOURCE_ENVIRONMENT
    if memory_root is not None and memory_root == vault_root:
        return vault_root, None, _MEMORY_SOURCE_NONE
    return vault_root, memory_root, source


def _build_runtime_settings(profile: CompanionProfile) -> tuple[Path | None, Settings]:
    env_settings = get_settings()
    vault_root, memory_root, _source = _resolve_runtime_roots(profile, env_settings)
    if vault_root is None:
        return None, env_settings
    extra_map: dict[str, Path] = {}
    for raw_prefix, raw_path in profile.extra_read_vaults.items():
        if not str(raw_path).strip():
            continue
        try:
            clean_prefix = validate_extra_vault_prefix(raw_prefix)
        except (TypeError, ValueError):
            continue
        extra_map[clean_prefix] = Path(raw_path).expanduser().resolve()
    read_paths = [vault_root]
    if memory_root is not None and memory_root not in read_paths:
        read_paths.append(memory_root)
    for extra_root in extra_map.values():
        if extra_root not in read_paths:
            read_paths.append(extra_root)
    write_paths = (
        default_write_paths(vault_root, memory_root=memory_root) if profile.enable_write else []
    )
    updated = env_settings.model_copy(
        update={
            "vault_root": vault_root,
            "memory_root": memory_root,
            "extra_read_vaults": extra_map,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "durability": profile.durability,
        }
    )
    return vault_root, updated


def _require_vault(vault_root: Path | None, locale: str) -> Path:
    if vault_root is None or not vault_root.is_dir():
        raise ValueError(t("companion.errors.vault_required", locale=locale))
    return vault_root


async def _read_index_stats(vault_root: Path | None, settings: Settings) -> dict[str, int]:
    if vault_root is None or not vault_root.is_dir():
        return {"note_count": 0, "chunk_count": 0, "generation": 0}
    db_path = sidecar_index_db(vault_root, settings=settings)
    if not db_path.is_file():
        return {"note_count": 0, "chunk_count": 0, "generation": 0}
    store = SQLiteFTS5Store()
    await store.open(db_path, read_only=True)
    try:
        stats = await store.stats()
        return {
            "note_count": stats.note_count,
            "chunk_count": stats.chunk_count,
            "generation": stats.generation,
        }
    finally:
        await store.close()


def _discover_mcp_targets(vault_root: Path | None) -> list[dict[str, Any]]:
    scopes = (SCOPE_USER, SCOPE_PROJECT) if vault_root is not None else (SCOPE_USER,)
    targets = discover_targets(
        scopes=scopes,
        project_dir=vault_root if vault_root is not None else Path.cwd(),
    )
    return [
        {
            "client_id": target.client_id,
            "display_name": target.display_name,
            "scope": target.scope,
            "config_path": str(target.config_path),
            "already_configured": target.config_path.is_file(),
        }
        for target in targets
    ]


async def get_companion_state(profile_override: Path | None = None) -> dict[str, Any]:
    """Assemble the complete initial state for the Companion frontend."""
    profile_snap = read_companion_profile(profile_override)
    profile = profile_snap.profile
    env_settings = get_settings()
    vault_root, settings = _build_runtime_settings(profile)
    _vault, _memory, memory_source = _resolve_runtime_roots(profile, env_settings)
    # The editable fields show what the profile stores, never a value inherited from
    # the environment: saving the topology back must not persist the environment.
    effective_profile = CompanionProfile(
        vault_path=str(vault_root) if vault_root else profile.vault_path,
        memory_vault_path=profile.memory_vault_path,
        extra_read_vaults=dict(profile.extra_read_vaults),
        ui_language=profile.ui_language,
        cli_timeout_seconds=profile.cli_timeout_seconds,
        enable_write=profile.enable_write,
        durability=profile.durability,
    )
    vault_cfg_snap = (
        read_vault_config_snapshot(vault_root, settings=settings)
        if vault_root is not None and vault_root.is_dir()
        else VaultConfigSnapshot(
            contract_version=1,
            config_path="",
            present=False,
            content_hash="absent",
            valid=True,
            values=VaultConfig().model_dump(mode="json"),
            errors=[],
        )
    )
    index_stats = await _read_index_stats(vault_root, settings)
    is_dual = vault_root is not None and settings.memory_root is not None
    vault_ready = vault_root is not None and vault_root.is_dir()
    return {
        "version": __version__,
        "profile_snapshot": {
            **profile_snap.to_dict(),
            "profile": asdict(effective_profile),
        },
        "vault_config_snapshot": vault_cfg_snap.to_dict(),
        "readiness": {
            "vault_ready": vault_ready,
            "vault_mode": "dual" if is_dual else ("single" if vault_ready else "unconfigured"),
            "index_stats": index_stats,
            "memory_root": str(settings.memory_root) if is_dual else None,
            "memory_source": memory_source if is_dual else _MEMORY_SOURCE_NONE,
        },
        "mcp_targets": _discover_mcp_targets(vault_root),
        "choices": {
            "clients": list(CLIENT_CHOICES),
            "scopes": list(INSTALL_SCOPE_CHOICES),
            "durability_modes": sorted(VALID_DURABILITY_MODES),
            "timeouts": list(ALLOWED_CLI_TIMEOUTS),
        },
        "i18n": companion_i18n_bundle(effective_profile.ui_language),
    }


async def save_companion_topology(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and save Companion topology & preferences under CAS protection."""
    locale = companion_locale(profile_path)
    expected_hash = payload.get("expected_hash")
    exp_str = str(expected_hash) if isinstance(expected_hash, str) and expected_hash else None
    values = payload.get("profile")
    if not isinstance(values, Mapping):
        raise ValueError(t("companion.errors.profile_mapping_missing", locale=locale))
    snap = write_companion_profile_cas(
        values,
        path=profile_path,
        expected_hash=exp_str,
    )
    vault_root, settings = _build_runtime_settings(snap.profile)
    if vault_root is not None:
        vault_root.mkdir(parents=True, exist_ok=True)
        sidecar_target = settings.memory_root if settings.memory_root is not None else vault_root
        sidecar_target.mkdir(parents=True, exist_ok=True)
        initialize_vault(sidecar_target)
    return await get_companion_state(profile_path)


def _profile_values(snapshot: CompanionProfileSnapshot, locale: str) -> dict[str, Any]:
    """Return the stored profile as a mapping, refusing to rebuild an invalid one."""
    if snapshot.present and not snapshot.valid:
        raise ConfigValidationError(
            t(
                "companion.errors.profile_invalid",
                locale=locale,
                errors="; ".join(snapshot.errors),
            )
        )
    return asdict(snapshot.profile)


async def save_companion_preferences(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Save UI preferences alone: no topology change and no vault initialization.

    A language switch or a preference change goes through here, so it can never
    persist a typed but unsaved vault path or create a vault at it.
    """
    locale = companion_locale(profile_path)
    preferences = payload.get("preferences")
    if not isinstance(preferences, Mapping):
        raise ValueError(t("companion.errors.preferences_mapping_missing", locale=locale))
    unknown = sorted(set(preferences) - _PREFERENCE_FIELDS)
    if unknown:
        raise ValueError(
            t("companion.errors.unknown_preference", locale=locale, keys=", ".join(unknown))
        )
    snapshot = read_companion_profile(profile_path)
    expected_hash = payload.get("expected_hash")
    exp_str = (
        str(expected_hash)
        if isinstance(expected_hash, str) and expected_hash
        else snapshot.content_hash
    )
    values = _profile_values(snapshot, locale)
    values.update(preferences)
    written = write_companion_profile_cas(values, path=profile_path, expected_hash=exp_str)
    return {
        "profile_snapshot": written.to_dict(),
        "i18n": companion_i18n_bundle(written.profile.ui_language),
    }


def _write_vault_config(
    vault_root: Path,
    values: Mapping[str, Any],
    *,
    settings: Settings,
    expected_hash: str | None,
    reset: bool,
) -> VaultConfigSnapshot:
    # The core writer refuses to replace an invalid file unless allow_reset says so.
    extra: dict[str, Any] = {"allow_reset": True} if reset else {}
    return write_vault_config_cas(
        vault_root,
        values,
        settings=settings,
        expected_hash=expected_hash,
        **extra,
    )


async def save_vault_config(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Save the fields the user edited in `.datacron/VAULT.yaml` under CAS protection.

    Only the edited fields are sent to the writer, which keeps every other key of the
    file as it is on disk. The snapshot values are never merged in: for an invalid file
    they are the defaults, and writing them would replace the vault identity, folders
    and organization with defaults. An invalid file is refused unless the user asks for
    an explicit reset. ``expected_hash`` is the hash the edit was based on, so an
    external edit made since the Settings view was loaded is a CAS conflict.
    """
    profile_snap = read_companion_profile(profile_path)
    locale = companion_locale(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, locale)
    expected_hash = payload.get("expected_hash")
    exp_str = str(expected_hash) if isinstance(expected_hash, str) and expected_hash else None
    values = payload.get("values")
    if not isinstance(values, Mapping):
        raise ValueError(t("companion.errors.values_mapping_missing", locale=locale))
    reset = payload.get("reset") is True
    current = read_vault_config_snapshot(resolved_root, settings=settings)
    if current.present and not current.valid and not reset:
        raise ConfigValidationError(
            t(
                "companion.errors.vault_config_invalid",
                locale=locale,
                errors="; ".join(current.errors),
            )
        )
    if not values and not reset:
        return {"vault_config_snapshot": current.to_dict(), "unchanged": True}
    # A reset is an explicit request to start again from the defaults the Settings
    # view displays for an invalid file, with the user's edits on top.
    to_write: dict[str, Any] = {**current.values, **values} if reset else dict(values)
    vault_snap = _write_vault_config(
        resolved_root,
        to_write,
        settings=settings,
        expected_hash=exp_str,
        reset=reset,
    )
    return {"vault_config_snapshot": vault_snap.to_dict()}


async def run_companion_sync(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Run incremental index reconciliation or atomic full index rebuild."""
    profile_snap = read_companion_profile(profile_path)
    locale = companion_locale(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, locale)
    sidecar_target = settings.memory_root if settings.memory_root is not None else resolved_root
    initialize_vault(sidecar_target)
    cfg_path = sidecar_vault_config(resolved_root, settings=settings)
    vault_cfg = load_vault_config(cfg_path) or VaultConfig()
    full_rebuild = bool(payload.get("rebuild", False))
    if full_rebuild:
        rebuild_stats = await rebuild_index_atomic(resolved_root, settings, vault_cfg)
        return {"mode": "rebuild", "stats": dict(rebuild_stats)}
    db_path = sidecar_index_db(resolved_root, settings=settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    reader = build_configured_reader(
        resolved_root,
        read_only=False,
        settings=settings,
    )
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    store = SQLiteFTS5Store(
        term_map=vault_cfg.query_expansion,
        archive_tags=vault_cfg.archive_tags,
    )
    await store.open(db_path)
    try:
        rec_stats = await reconcile(store, reader, chunker, mtime_gate=False)
        return {"mode": "incremental", "stats": dict(rec_stats)}
    finally:
        await store.close()


def _write_migrated_profile(
    vault_path: Path,
    memory_vault_path: Path,
    *,
    profile_path: Path | None,
    locale: str,
) -> tuple[bool, list[str]]:
    """Point the profile at the migrated layout without ever failing the migration.

    The migration has already happened when this runs. A concurrent profile write is
    merged by re-reading the latest profile and applying the two paths again; when it
    still cannot be written, a warning tells the user what to set by hand.
    """
    for _attempt in range(_PROFILE_WRITE_ATTEMPTS):
        snapshot = read_companion_profile(profile_path)
        if snapshot.present and not snapshot.valid:
            break
        values = asdict(snapshot.profile)
        values["vault_path"] = str(vault_path)
        values["memory_vault_path"] = str(memory_vault_path)
        try:
            write_companion_profile_cas(
                values,
                path=profile_path,
                expected_hash=snapshot.content_hash,
            )
        except ConfigCasConflictError:
            continue
        except (ConfigValidationError, OSError):
            break
        return True, []
    return False, [
        t(
            "companion.migration.profile_not_updated",
            locale=locale,
            vault=vault_path,
            memory_vault=memory_vault_path,
        )
    ]


@dataclass(frozen=True, slots=True)
class _MigrationOptions:
    keep_source: bool
    cleanup_only: bool
    client: str
    install_scope: str


def _migration_options(payload: Mapping[str, Any], locale: str) -> _MigrationOptions:
    cleanup_only = payload.get("cleanup_only") is True
    keep_source = False if cleanup_only else payload.get("keep_source", True) is not False
    client = str(payload.get("client", CLIENT_ALL)).strip()
    if client not in CLIENT_CHOICES:
        raise ValueError(t("companion.errors.unknown_client", locale=locale, client=client))
    install_scope = str(payload.get("install_scope", INSTALL_SCOPE_USER)).strip()
    if install_scope not in INSTALL_SCOPE_CHOICES:
        raise ValueError(t("companion.errors.unknown_scope", locale=locale, scope=install_scope))
    if not keep_source:
        if payload.get("confirm_cleanup") is not True:
            raise MigrationError(t("companion.errors.cleanup_needs_confirmation", locale=locale))
        if client == CLIENT_NONE and not cleanup_only:
            raise MigrationError(t("companion.errors.cleanup_needs_clients", locale=locale))
    return _MigrationOptions(keep_source, cleanup_only, client, install_scope)


def _client_ids_to_reconfigure(vault_root: Path, client: str, install_scope: str) -> list[str]:
    if client != CLIENT_ALL:
        return [client]
    scopes = {
        INSTALL_SCOPE_USER: (SCOPE_USER,),
        INSTALL_SCOPE_PROJECT: (SCOPE_PROJECT,),
    }.get(install_scope, (SCOPE_USER, SCOPE_PROJECT))
    targets = discover_targets(scopes=scopes, project_dir=vault_root)
    return sorted({target.client_id for target in targets})


async def _reconfigure_clients(
    vault_root: Path,
    memory_root: Path,
    *,
    client: str,
    install_scope: str,
    locale: str,
) -> tuple[list[InstallOutcome], list[str], list[str]]:
    """Point the clients at the Dual-Vault layout, each keeping its own write setting.

    The write and read-only settings come from each client's current entry for this
    vault, as the command line does: rewriting an entry must neither grant write access
    to a read-only client nor take it from a writable one. A client with no entry for
    the vault is registered without write access. Clients sharing one setting are
    written in one run; otherwise each client gets its own run.
    """
    groups: dict[tuple[bool, bool], list[str]] = {}
    for client_id in _client_ids_to_reconfigure(vault_root, client, install_scope):
        current = existing_client_settings(vault_root, client_id, install_scope)
        key = (bool(current[0]), bool(current[1])) if current is not None else (False, False)
        groups.setdefault(key, []).append(client_id)
    runs: list[tuple[str, bool, bool]] = []
    if client == CLIENT_ALL and len(groups) == 1:
        (write, read_only), _ids = next(iter(groups.items()))
        runs.append((CLIENT_ALL, write, read_only))
    else:
        runs.extend(
            (client_id, write, read_only)
            for (write, read_only), ids in groups.items()
            for client_id in ids
        )
    installs: list[InstallOutcome] = []
    errors: list[str] = []
    notes: list[str] = []
    for run_client, write, read_only in runs:
        result = await run_setup(
            SetupPlan(
                vault_path=vault_root,
                memory_vault_path=memory_root,
                build_index=False,
                enable_write=write,
                write_paths=default_write_paths(vault_root, memory_root=memory_root)
                if write
                else [],
                client=run_client,
                install_scope=install_scope,
                read_only=True if read_only else None,
            )
        )
        installs.extend(result.client_installs)
        errors.extend(result.client_errors)
        if result.stdio_config is not None:
            notes.append(t("companion.migration.client_manual", locale=locale, client=run_client))
    return installs, errors, notes


def _migration_payload(
    result: MigrationResult,
    *,
    source_cleaned: bool,
    clients_reconfigured: bool,
    installs: list[InstallOutcome],
    client_errors: list[str],
    profile_updated: bool,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "migration": {
            "vault_path": str(result.vault_path),
            "memory_vault_path": str(result.memory_vault_path),
            "copied_sidecar_files": result.copied_sidecar_files,
            "copied_memory_files": result.copied_memory_files,
            "verified_files": result.verified_files,
            "resumed_files": result.resumed_files,
            "indexed_notes": result.indexed_notes,
            "indexed_chunks": result.indexed_chunks,
            "source_cleaned": source_cleaned,
            "clients_reconfigured": clients_reconfigured,
            "client_installs": [
                {
                    "client_id": item.client_id,
                    "display_name": item.display_name,
                    "scope": item.scope,
                    "installed": item.installed,
                }
                for item in installs
            ],
            "client_errors": client_errors,
            "profile_updated": profile_updated,
            "warnings": warnings,
        }
    }


async def _run_engine(
    vault_path: Path,
    memory_path: Path,
    *,
    locale: str,
    **kwargs: Any,
) -> MigrationResult:
    try:
        return await migrate_vault_to_dual(vault_path, memory_path, **kwargs)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(
            t("companion.errors.migration_os_error", locale=locale, detail=str(exc))
        ) from exc


async def run_companion_migration(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the Single-Vault to Dual-Vault migration and update the profile.

    The copy runs with the source kept. The detected MCP clients are then reconfigured
    by default, each keeping its current write setting, and only after that, and only
    when the request confirms it, is the source removed through the engine's
    cleanup-only mode. A cleanup refused at that point is a warning on the successful
    migration. ``cleanup_only`` alone finishes an earlier migration whose source was kept.
    """
    profile_snap = read_companion_profile(profile_path)
    locale = companion_locale(profile_path)
    vault_str = str(payload.get("vault_path") or profile_snap.profile.vault_path).strip()
    mem_str = str(
        payload.get("memory_vault_path") or profile_snap.profile.memory_vault_path
    ).strip()
    if not vault_str or not mem_str:
        raise ValueError(t("companion.errors.migration_paths_required", locale=locale))
    options = _migration_options(payload, locale)
    vault_path, memory_path = Path(vault_str), Path(mem_str)
    if options.cleanup_only:
        cleaned = await _run_engine(vault_path, memory_path, locale=locale, cleanup_only=True)
        return _migration_payload(
            cleaned,
            source_cleaned=cleaned.source_cleaned,
            clients_reconfigured=False,
            installs=[],
            client_errors=[],
            profile_updated=False,
            warnings=list(cleaned.warnings),
        )
    result = await _run_engine(
        vault_path,
        memory_path,
        locale=locale,
        keep_source=True,
        client=CLIENT_NONE,
    )
    warnings = list(result.warnings)
    installs: list[InstallOutcome] = []
    client_errors: list[str] = []
    reconfigure = options.client != CLIENT_NONE
    if reconfigure:
        installs, client_errors, notes = await _reconfigure_clients(
            result.vault_path,
            result.memory_vault_path,
            client=options.client,
            install_scope=options.install_scope,
            locale=locale,
        )
        warnings.extend(notes)
    source_cleaned = False
    if not options.keep_source and client_errors:
        warnings.append(t("migration.warning.cleanup_skipped_client_errors", locale=locale))
    elif not options.keep_source:
        try:
            cleaned = await migrate_vault_to_dual(
                result.vault_path, result.memory_vault_path, cleanup_only=True
            )
        except (MigrationError, OSError) as exc:
            warnings.append(str(exc))
        else:
            source_cleaned = cleaned.source_cleaned
            warnings.extend(cleaned.warnings)
    profile_updated, profile_warnings = _write_migrated_profile(
        result.vault_path,
        result.memory_vault_path,
        profile_path=profile_path,
        locale=locale,
    )
    return _migration_payload(
        result,
        source_cleaned=source_cleaned,
        clients_reconfigured=reconfigure,
        installs=installs,
        client_errors=client_errors,
        profile_updated=profile_updated,
        warnings=[*warnings, *profile_warnings],
    )


async def list_companion_notes(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """List notes in the configured vault(s) with bounded limit and offset."""
    locale = companion_locale(profile_path)
    limit = _parse_bounded_int(
        payload.get("limit"),
        field_name="limit",
        default=_NOTES_DEFAULT_LIMIT,
        minimum=1,
        maximum=_NOTES_MAX_LIMIT,
        locale=locale,
    )
    offset = _parse_bounded_int(
        payload.get("offset"),
        field_name="offset",
        default=0,
        minimum=0,
        maximum=_NOTES_MAX_OFFSET,
        locale=locale,
    )
    folder_raw = str(payload.get("folder") or "").strip()
    folder = folder_raw if folder_raw else None
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        return {"notes": [], "total": 0, "returned": 0}
    app = build_app(settings=settings, vault_root=vault_root)
    db_path = sidecar_index_db(vault_root, settings=settings)
    if db_path.is_file():
        await app.store.open(db_path, read_only=True)
    try:
        return await list_notes_impl(
            app,
            folder=folder,
            tags=None,
            limit=limit,
            offset=offset,
        )
    finally:
        await app.store.close()


def _folder_to_regex_glob(folder: str | None) -> str | None:
    """Map the Search view's folder filter to the vault glob search_regex expects.

    The text search takes a folder; the regex search takes a glob, where a bare
    folder name only matches a file of that name. A value without glob syntax is a
    folder and becomes ``<folder>/**``; a value with glob syntax passes unchanged.
    """
    if folder is None:
        return None
    normalized = folder.replace("\\", "/").strip("/")
    if not normalized:
        return None
    if any(char in normalized for char in _GLOB_METACHARACTERS):
        return normalized
    return f"{normalized}/**"


async def run_companion_search(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Run FTS5 or Regex search across the configured vault(s)."""
    locale = companion_locale(profile_path)
    limit = _parse_bounded_int(
        payload.get("limit"),
        field_name="limit",
        default=_SEARCH_DEFAULT_LIMIT,
        minimum=1,
        maximum=_SEARCH_MAX_LIMIT,
        locale=locale,
    )
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, locale)
    query = str(payload.get("query") or "").strip()
    if not query:
        return {
            "query": "",
            "results": [],
            "returned": 0,
            "limit_applied": limit,
        }
    folder_raw = str(payload.get("folder") or "").strip()
    folder = folder_raw if folder_raw else None
    mode = str(payload.get("mode") or "fts").strip().lower()
    app = build_app(settings=settings, vault_root=resolved_root)
    db_path = sidecar_index_db(resolved_root, settings=settings)
    if not db_path.is_file():
        raise ValueError(t("companion.errors.index_missing", locale=locale))
    await app.store.open(db_path)
    try:
        if mode == "regex":
            return await search_regex_impl(
                app,
                pattern=query,
                glob=_folder_to_regex_glob(folder),
                limit=limit,
            )
        return await search_text_impl(
            app,
            query=query,
            limit=limit,
            include_superseded=False,
            folder=folder,
            tags=None,
            frontmatter=None,
            group_by_note=False,
        )
    finally:
        await app.store.close()


async def run_companion_graph_health(
    _payload: Mapping[str, Any] | None = None,
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Run knowledge-graph link integrity and topology diagnostics."""
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, companion_locale(profile_path))
    report = await analyze_vault_graph(resolved_root, settings=settings)
    payload_dict = report.to_dict()
    if SecretRedactor.retrieval_enabled(settings):
        redactor = SecretRedactor.from_settings(settings)
        payload_dict = redactor.redact_value(payload_dict)
    return payload_dict


async def run_companion_compaction(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Preview or apply smart monthly memory compaction."""
    locale = companion_locale(profile_path)
    days = _parse_bounded_int(
        payload.get("older_than_days"),
        field_name="older_than_days",
        default=DEFAULT_COMPACTION_DAYS,
        minimum=0,
        maximum=_COMPACTION_MAX_DAYS,
        locale=locale,
    )
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, locale)
    dry_run = bool(payload.get("dry_run", True))
    report = await compact_vault_memory(
        resolved_root,
        settings=settings,
        older_than_days=days,
        dry_run=dry_run,
    )
    payload_dict = report.to_dict()
    if SecretRedactor.retrieval_enabled(settings):
        redactor = SecretRedactor.from_settings(settings)
        payload_dict = redactor.redact_value(payload_dict)
    return payload_dict


async def run_companion_mcp_setup(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Configure MCP client registrations using SetupPlan and run_setup."""
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    resolved_root = _require_vault(vault_root, companion_locale(profile_path))
    client = str(payload.get("client", "all")).strip()
    scope = str(payload.get("install_scope", "both")).strip()
    enable_write = bool(payload.get("enable_write", profile_snap.profile.enable_write))
    write_roots = (
        default_write_paths(resolved_root, memory_root=settings.memory_root) if enable_write else []
    )
    res = await run_setup(
        SetupPlan(
            vault_path=resolved_root,
            memory_vault_path=settings.memory_root,
            build_index=False,
            enable_write=enable_write,
            write_paths=write_roots,
            client=client,
            install_scope=scope,
            durability=profile_snap.profile.durability,
        )
    )
    return {
        "installed": [
            {
                "client_id": item.client_id,
                "display_name": item.display_name,
                "scope": item.scope,
                "config_path": str(item.config_path),
                "installed": item.installed,
                "detail": item.detail,
            }
            for item in res.client_installs
        ],
        "errors": list(res.client_errors),
    }


async def get_companion_history(
    _payload: Mapping[str, Any] | None = None,
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Return recent durable operation log records for the configured vault."""
    payload = _payload or {}
    limit = _parse_bounded_int(
        payload.get("limit"),
        field_name="limit",
        default=_MAX_HISTORY_RECORDS,
        minimum=1,
        maximum=_HISTORY_MAX_LIMIT,
        locale=companion_locale(profile_path),
    )
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        return {"operations": []}
    app = build_app(settings=settings, vault_root=vault_root)
    records = await app.vault_writer.list_operations()
    admitted = [rec for rec in records if admits_note_path(app.scope, rec.rel_path)]
    recent = list(reversed(admitted[-limit:]))
    redact = SecretRedactor.retrieval_enabled(app.settings)
    return {
        "operations": [
            {
                "op_id": rec.operation_id,
                "timestamp": rec.timestamp,
                "tool": rec.tool,
                "rel_path": (
                    app.secret_redactor.redact_text(rec.rel_path) if redact else rec.rel_path
                ),
                "note_id": rec.note_id,
                "actor": rec.actor,
                "before_hash": rec.before_hash,
                "after_hash": rec.after_hash,
            }
            for rec in recent
        ]
    }


search_companion = run_companion_search
get_companion_graph_health = run_companion_graph_health
run_companion_memory_compaction = run_companion_compaction
