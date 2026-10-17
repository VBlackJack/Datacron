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
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from datacron import __version__
from datacron.bootstrap import initialize_vault
from datacron.config_contract import (
    ALLOWED_CLI_TIMEOUTS,
    CompanionProfile,
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
    _validate_extra_vault_prefix,
    get_settings,
    load_vault_config,
)
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.core.scope import admits_note_path
from datacron.core.security import SecretRedactor
from datacron.core.vault import build_configured_reader
from datacron.graph_health import analyze_vault_graph
from datacron.i18n import t
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.indexing.reconcile import reconcile
from datacron.installers.mcp_clients import (
    SCOPE_PROJECT,
    SCOPE_USER,
    discover_targets,
)
from datacron.mcp.server import build_app
from datacron.mcp.tools.read import _list_notes_impl
from datacron.mcp.tools.search import search_regex_impl, search_text_impl
from datacron.memory_compaction import DEFAULT_COMPACTION_DAYS, compact_vault_memory
from datacron.migration import migrate_vault_to_dual
from datacron.setup_wizard import (
    CLIENT_CHOICES,
    INSTALL_SCOPE_CHOICES,
    SetupPlan,
    default_write_paths,
    run_setup,
)

__all__ = [
    "companion_i18n_bundle",
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
    "save_companion_topology",
    "save_vault_config",
    "search_companion",
]

_MAX_HISTORY_RECORDS: Final[int] = 25


def _parse_bounded_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


_COMPANION_I18N_SECTIONS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "app",
        ("title", "subtitle", "connected", "refresh", "save", "language_label"),
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
            "extra_vaults_label",
            "save_topology",
            "sync_now",
            "rebuild_now",
            "migrate_title",
            "migrate_desc",
            "migrate_keep_source",
            "migrate_button",
        ),
    ),
    (
        "search",
        (
            "headline",
            "query_placeholder",
            "folder_placeholder",
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
            "broken_links_title",
            "orphan_notes_title",
            "hub_notes_title",
            "cross_vault_title",
            "compaction_title",
            "compaction_days_label",
            "compaction_preview_btn",
            "compaction_apply_btn",
        ),
    ),
    (
        "mcp",
        ("headline", "client_label", "scope_label", "enable_write_label", "configure_button"),
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


def _build_runtime_settings(profile: CompanionProfile) -> tuple[Path | None, Settings]:
    env_settings = get_settings()
    vault_str = profile.vault_path or (
        str(env_settings.vault_root) if env_settings.vault_root else ""
    )
    if not vault_str:
        return None, env_settings
    vault_root = Path(vault_str).expanduser().resolve()
    mem_str = profile.memory_vault_path or (
        str(env_settings.memory_root) if env_settings.memory_root else ""
    )
    memory_root = Path(mem_str).expanduser().resolve() if mem_str else None
    if memory_root is not None and memory_root == vault_root:
        memory_root = None
    extra_map: dict[str, Path] = {}
    for raw_prefix, raw_path in profile.extra_read_vaults.items():
        if not str(raw_path).strip():
            continue
        try:
            clean_prefix = _validate_extra_vault_prefix(raw_prefix)
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
    vault_root, settings = _build_runtime_settings(profile)
    effective_profile = CompanionProfile(
        vault_path=str(vault_root) if vault_root else profile.vault_path,
        memory_vault_path=str(settings.memory_root)
        if settings.memory_root
        else profile.memory_vault_path,
        extra_read_vaults={k: str(v) for k, v in settings.extra_read_vaults.items()}
        if settings.extra_read_vaults
        else profile.extra_read_vaults,
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
    is_dual = settings.memory_root is not None
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
    expected_hash = payload.get("expected_hash")
    exp_str = str(expected_hash) if isinstance(expected_hash, str) and expected_hash else None
    values = payload.get("profile")
    if not isinstance(values, Mapping):
        raise ValueError("Missing 'profile' mapping in request payload")
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


async def save_vault_config(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and atomically save `.datacron/VAULT.yaml` under CAS protection."""
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
    expected_hash = payload.get("expected_hash")
    exp_str = str(expected_hash) if isinstance(expected_hash, str) and expected_hash else None
    values = payload.get("values")
    if not isinstance(values, Mapping):
        raise ValueError("Missing 'values' mapping in request payload")
    current = read_vault_config_snapshot(vault_root, settings=settings)
    merged: dict[str, Any] = dict(current.values)
    merged.update(values)
    vault_snap = write_vault_config_cas(
        vault_root,
        merged,
        settings=settings,
        expected_hash=exp_str,
    )
    return {"vault_config_snapshot": vault_snap.to_dict()}


async def run_companion_sync(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Run incremental index reconciliation or atomic full index rebuild."""
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
    sidecar_target = settings.memory_root if settings.memory_root is not None else vault_root
    initialize_vault(sidecar_target)
    cfg_path = sidecar_vault_config(vault_root, settings=settings)
    vault_cfg = load_vault_config(cfg_path) or VaultConfig()
    full_rebuild = bool(payload.get("rebuild", False))
    if full_rebuild:
        rebuild_stats = await rebuild_index_atomic(vault_root, settings, vault_cfg)
        return {"mode": "rebuild", "stats": dict(rebuild_stats)}
    db_path = sidecar_index_db(vault_root, settings=settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    reader = build_configured_reader(
        vault_root,
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


async def run_companion_migration(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Execute atomic Single-Vault to Dual-Vault migration and update profile."""
    profile_snap = read_companion_profile(profile_path)
    vault_str = str(payload.get("vault_path") or profile_snap.profile.vault_path).strip()
    mem_str = str(
        payload.get("memory_vault_path") or profile_snap.profile.memory_vault_path
    ).strip()
    if not vault_str or not mem_str:
        raise ValueError("Both vault_path and memory_vault_path are required for migration")
    keep_source = bool(payload.get("keep_source", False))
    result = await migrate_vault_to_dual(
        Path(vault_str),
        Path(mem_str),
        keep_source=keep_source,
    )
    updated_profile = asdict(profile_snap.profile)
    updated_profile["vault_path"] = str(result.vault_path)
    updated_profile["memory_vault_path"] = str(result.memory_vault_path)
    write_companion_profile_cas(
        updated_profile,
        path=profile_path,
        expected_hash=profile_snap.content_hash,
    )
    return {
        "migration": {
            "vault_path": str(result.vault_path),
            "memory_vault_path": str(result.memory_vault_path),
            "copied_sidecar_files": result.copied_sidecar_files,
            "copied_memory_files": result.copied_memory_files,
            "verified_files": result.verified_files,
            "indexed_notes": result.indexed_notes,
            "indexed_chunks": result.indexed_chunks,
            "source_cleaned": result.source_cleaned,
        }
    }


async def list_companion_notes(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """List notes in the configured vault(s) with bounded limit and offset."""
    limit = _parse_bounded_int(
        payload.get("limit"),
        field_name="limit",
        default=50,
        minimum=1,
        maximum=200,
    )
    offset = _parse_bounded_int(
        payload.get("offset"),
        field_name="offset",
        default=0,
        minimum=0,
        maximum=100_000,
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
        return await _list_notes_impl(
            app,
            folder=folder,
            tags=None,
            limit=limit,
            offset=offset,
        )
    finally:
        await app.store.close()


async def run_companion_search(
    payload: Mapping[str, Any],
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Run FTS5 or Regex search across the configured vault(s)."""
    limit = _parse_bounded_int(
        payload.get("limit"),
        field_name="limit",
        default=15,
        minimum=1,
        maximum=100,
    )
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
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
    app = build_app(settings=settings, vault_root=vault_root)
    db_path = sidecar_index_db(vault_root, settings=settings)
    if not db_path.is_file():
        raise ValueError("Index database not found; run Synchronize Index first")
    await app.store.open(db_path)
    try:
        if mode == "regex":
            return await search_regex_impl(app, pattern=query, glob=folder, limit=limit)
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
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
    report = await analyze_vault_graph(vault_root, settings=settings)
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
    days = _parse_bounded_int(
        payload.get("older_than_days"),
        field_name="older_than_days",
        default=DEFAULT_COMPACTION_DAYS,
        minimum=0,
        maximum=3650,
    )
    profile_snap = read_companion_profile(profile_path)
    vault_root, settings = _build_runtime_settings(profile_snap.profile)
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
    dry_run = bool(payload.get("dry_run", True))
    report = await compact_vault_memory(
        vault_root,
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
    if vault_root is None or not vault_root.is_dir():
        raise ValueError("Configure a valid primary documentation vault first")
    client = str(payload.get("client", "all")).strip()
    scope = str(payload.get("install_scope", "both")).strip()
    enable_write = bool(payload.get("enable_write", profile_snap.profile.enable_write))
    write_roots = (
        default_write_paths(vault_root, memory_root=settings.memory_root) if enable_write else []
    )
    res = await run_setup(
        SetupPlan(
            vault_path=vault_root,
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
        maximum=200,
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
