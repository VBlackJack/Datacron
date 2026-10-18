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
"""Regressions for the 2026-10-18 audit, Companion and migration lot (L6).

D-1, D-2, D-4, D-9, D-12, D-13, D-14, D-16, D-17 and M. New Companion symbols are imported
inside the tests that need them, so each test fails on its own against the previous code.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from datacron import migration as migration_module
from datacron.bootstrap import initialize_vault
from datacron.companion import api as companion_api
from datacron.companion.api import (
    _build_runtime_settings,
    companion_i18n_bundle,
    get_companion_state,
    run_companion_migration,
    run_companion_search,
    run_companion_sync,
    save_vault_config,
)
from datacron.companion.server import (
    TOKEN_HEADER_NAME,
    CompanionHTTPServer,
    create_companion_server,
)
from datacron.companion.ui import render_companion_html
from datacron.config_contract import (
    CompanionProfile,
    ConfigValidationError,
    read_companion_profile,
    read_vault_config_snapshot,
    write_companion_profile_cas,
    write_vault_config_cas,
)
from datacron.core.config import (
    MEMORY_SUBFOLDER_NAME,
    OPLOG_DIR_NAME,
    OPLOG_PENDING_DIR_NAME,
    SIDECAR_DIR_NAME,
    Settings,
    reset_settings_cache,
)
from datacron.core.vault_writer import FilesystemVaultWriter, VaultLockBusyError
from datacron.i18n import t
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.migration import MigrationError, MigrationResult, migrate_vault_to_dual

_SHORT_LOCK_TIMEOUT_SECONDS = 0.2
_VALID_VAULT_ID = "01J00000000000000000000777"
_PROFILE_ENV_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")
_CORE_HAS_ALLOW_RESET = (
    "allow_reset" in __import__("inspect").signature(write_vault_config_cas).parameters
)


@pytest.fixture(autouse=True)
def _isolated_user_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every per-user config location at a temp dir: no run may touch a real client."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    for name in _PROFILE_ENV_VARS:
        monkeypatch.setenv(name, str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))


def _profile(tmp_path: Path, vault: Path, **extra: Any) -> Path:
    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault), **extra},
        path=profile_path,
        expect_absent=True,
    )
    return profile_path


def _vault_yaml(vault: Path) -> Path:
    return vault / SIDECAR_DIR_NAME / "VAULT.yaml"


@contextmanager
def _running_server(profile_path: Path) -> Iterator[tuple[CompanionHTTPServer, int, str]]:
    server, _url, token = create_companion_server(port=0, profile_path=profile_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, Any, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    all_headers = {"Host": f"127.0.0.1:{port}"}
    if token is not None:
        all_headers[TOKEN_HEADER_NAME] = token
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=payload, headers=all_headers)
    resp = conn.getresponse()
    data = resp.read()
    resp_headers = {key.lower(): value for key, value in resp.getheaders()}
    conn.close()
    try:
        return resp.status, json.loads(data), resp_headers
    except json.JSONDecodeError:
        return resp.status, data.decode("utf-8", errors="replace"), resp_headers


def _js_function(html: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\(.*?\n}}\n", html, flags=re.DOTALL)
    assert match is not None, name
    return match.group(0)


# ---------------------------------------------------------------------------
# D-1 / E-1: the VAULT.yaml save sends only the edited fields and refuses invalid files
# ---------------------------------------------------------------------------


def test_vault_config_save_refuses_invalid_file_and_keeps_its_identity(tmp_path: Path) -> None:
    """An invalid VAULT.yaml is never rewritten from the default snapshot values."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    cfg = _vault_yaml(vault)
    broken = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    broken["vault_id"] = _VALID_VAULT_ID
    broken["history_mode"] = "bogus"
    cfg.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")
    before = cfg.read_bytes()
    profile_path = _profile(tmp_path, vault)
    snap = read_vault_config_snapshot(vault)
    assert snap.valid is False

    with pytest.raises(ConfigValidationError):
        asyncio.run(
            save_vault_config(
                {"expected_hash": snap.content_hash, "values": {"archive_tags": ["meta/old"]}},
                profile_path=profile_path,
            )
        )
    assert cfg.read_bytes() == before


def test_vault_config_save_sends_only_the_edited_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer receives the delta, never the snapshot values merged with it."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    profile_path = _profile(tmp_path, vault)
    snap = read_vault_config_snapshot(vault)
    captured: list[dict[str, Any]] = []
    real_writer = write_vault_config_cas

    def _spy(vault_root: Path, values: Any, **kwargs: Any) -> Any:
        captured.append(dict(values))
        return real_writer(vault_root, values, **kwargs)

    monkeypatch.setattr(companion_api, "write_vault_config_cas", _spy)
    asyncio.run(
        save_vault_config(
            {"expected_hash": snap.content_hash, "values": {"excluded_folders": ["Drafts"]}},
            profile_path=profile_path,
        )
    )
    assert captured == [{"excluded_folders": ["Drafts"]}]
    after = yaml.safe_load(_vault_yaml(vault).read_text(encoding="utf-8"))
    assert after["vault_id"] == snap.values["vault_id"]
    assert after["excluded_folders"] == ["Drafts"]


def test_vault_config_reset_is_explicit(tmp_path: Path) -> None:
    """An unparsable VAULT.yaml is replaced only when the request asks for a reset."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    cfg = _vault_yaml(vault)
    cfg.write_text("vault_id: VID2\nexcluded_folders: [a, b\n", encoding="utf-8")
    before = cfg.read_bytes()
    profile_path = _profile(tmp_path, vault)
    snap = read_vault_config_snapshot(vault)

    with pytest.raises(ConfigValidationError):
        asyncio.run(
            save_vault_config(
                {"expected_hash": snap.content_hash, "values": {"excluded_folders": ["z"]}},
                profile_path=profile_path,
            )
        )
    assert cfg.read_bytes() == before


@pytest.mark.skipif(
    not _CORE_HAS_ALLOW_RESET,
    reason="the core VAULT.yaml writer takes allow_reset from lot L1 on",
)
def test_vault_config_explicit_reset_replaces_an_invalid_file(tmp_path: Path) -> None:
    """With the reset flag the Companion passes allow_reset and the file is replaced."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    cfg = _vault_yaml(vault)
    cfg.write_text("vault_id: VID2\nexcluded_folders: [a, b\n", encoding="utf-8")
    profile_path = _profile(tmp_path, vault)
    snap = read_vault_config_snapshot(vault)
    result = asyncio.run(
        save_vault_config(
            {
                "expected_hash": snap.content_hash,
                "values": {"excluded_folders": ["z"]},
                "reset": True,
            },
            profile_path=profile_path,
        )
    )
    assert result["vault_config_snapshot"]["valid"] is True
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["excluded_folders"] == ["z"]


# ---------------------------------------------------------------------------
# D-2: the save is based on the hash the Settings view was loaded with
# ---------------------------------------------------------------------------


def test_vault_config_save_uses_the_loaded_hash_and_reports_external_edits(
    tmp_path: Path,
) -> None:
    """The UI does not chain the topology save, and an external edit returns 409."""
    html = render_companion_html("tok")
    save_fn = _js_function(html, "saveVaultConfig")
    assert "saveTopology" not in save_fn
    assert "VAULT_CFG_BASE.hash" in save_fn

    vault = tmp_path / "vault"
    initialize_vault(vault)
    profile_path = _profile(tmp_path, vault)
    with _running_server(profile_path) as (_server, port, token):
        _status, state, _h = _request(port, "GET", "/api/state", token=token)
        loaded_hash = state["vault_config_snapshot"]["content_hash"]
        cfg = _vault_yaml(vault)
        external = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        external["excluded_folders"] = ["EXTERNAL_EDIT"]
        cfg.write_text(yaml.safe_dump(external, sort_keys=False), encoding="utf-8")
        status, _data, _h = _request(
            port,
            "POST",
            "/api/vault-config",
            token=token,
            body={"expected_hash": loaded_hash, "values": {"excluded_folders": ["mine"]}},
        )
    assert status == 409
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["excluded_folders"] == ["EXTERNAL_EDIT"]


# ---------------------------------------------------------------------------
# D-4 (decision 2): the environment memory root belongs to the environment vault root
# ---------------------------------------------------------------------------


def test_environment_memory_root_applies_only_to_the_environment_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as the server: the environment memory root is ignored for a foreign vault.

    Foreign means DATACRON_VAULT_ROOT is set and names another vault; with no
    environment vault root, the environment memory root applies.
    """
    env_vault = tmp_path / "env-vault"
    env_memory = tmp_path / "env-memory"
    other_vault = tmp_path / "other-vault"
    for root in (env_vault, env_memory, other_vault):
        root.mkdir()
    monkeypatch.setenv("DATACRON_VAULT_ROOT", str(env_vault))
    monkeypatch.setenv("DATACRON_MEMORY_ROOT", str(env_memory))
    monkeypatch.setenv("DATACRON_READ_PATHS", str(env_vault))
    reset_settings_cache()

    vault_root, settings = _build_runtime_settings(CompanionProfile(vault_path=str(other_vault)))
    assert vault_root == other_vault.resolve()
    assert settings.memory_root is None

    _root, same_settings = _build_runtime_settings(CompanionProfile(vault_path=str(env_vault)))
    assert same_settings.memory_root == env_memory.resolve()

    profile_path = _profile(tmp_path, env_vault)
    state = asyncio.run(get_companion_state(profile_path))
    # The editable field shows the stored profile, so Save Topology cannot persist the
    # environment value; the effective memory root is reported separately.
    assert state["profile_snapshot"]["profile"]["memory_vault_path"] == ""
    assert state["readiness"]["memory_source"] == "environment"
    assert state["readiness"]["vault_mode"] == "dual"

    monkeypatch.delenv("DATACRON_VAULT_ROOT")
    reset_settings_cache()
    _root, unset_settings = _build_runtime_settings(CompanionProfile(vault_path=str(other_vault)))
    assert unset_settings.memory_root == env_memory.resolve()


# ---------------------------------------------------------------------------
# D-13: a language switch saves the preference alone
# ---------------------------------------------------------------------------


def test_language_switch_never_initializes_a_vault(tmp_path: Path) -> None:
    """The language preference has its own route; Ctrl+S and the switch skip the topology."""
    typed_vault = tmp_path / "typo-vault"
    profile_path = _profile(tmp_path, typed_vault)
    with _running_server(profile_path) as (_server, port, token):
        _status, state, _h = _request(port, "GET", "/api/state", token=token)
        status, data, _h = _request(
            port,
            "POST",
            "/api/preferences",
            token=token,
            body={
                "expected_hash": state["profile_snapshot"]["content_hash"],
                "preferences": {"ui_language": "en"},
            },
        )
    assert status == 200, data
    assert not typed_vault.exists()
    stored = read_companion_profile(profile_path).profile
    assert stored.ui_language == "en"
    assert stored.vault_path == str(typed_vault)
    html = render_companion_html("tok")
    assert "saveTopology" not in _js_function(html, "saveLanguage")


# ---------------------------------------------------------------------------
# D-9: the regex folder filter is a folder, not a file-name glob
# ---------------------------------------------------------------------------


def test_regex_folder_filter_becomes_a_recursive_glob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare folder in the Search view maps to `<folder>/**` for search_regex."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    (vault / "Architecture").mkdir()
    (vault / "Architecture" / "a.md").write_text("# A\n\nneedle\n", encoding="utf-8")
    profile_path = _profile(tmp_path, vault)
    asyncio.run(run_companion_sync({"rebuild": True}, profile_path=profile_path))
    seen: list[str | None] = []

    async def _spy(_app: Any, *, pattern: str, glob: str | None, limit: int) -> dict[str, Any]:
        seen.append(glob)
        return {"results": [], "pattern": pattern, "limit": limit}

    monkeypatch.setattr(companion_api, "search_regex_impl", _spy)
    for folder in ("Architecture", "Architecture/", "Architecture/*.md"):
        asyncio.run(
            run_companion_search(
                {"query": "needle", "mode": "regex", "folder": folder},
                profile_path=profile_path,
            )
        )
    assert seen == ["Architecture/**", "Architecture/**", "Architecture/*.md"]


# ---------------------------------------------------------------------------
# D-12, D-16, D-17: server hardening and catalog messages
# ---------------------------------------------------------------------------


def test_server_rejects_bad_content_length_serializes_and_localizes(tmp_path: Path) -> None:
    """Non-numeric Content-Length is a 400, index routes are exclusive, errors follow the UI."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    profile_path = _profile(tmp_path, vault, ui_language="fr")
    with _running_server(profile_path) as (server, port, token):
        status, _data, _h = _request(
            port,
            "POST",
            "/api/notes",
            token=token,
            raw=b"",
            headers={"Content-Length": "abc"},
        )
        assert status == 400

        status, data, _h = _request(
            port,
            "POST",
            "/api/search",
            token=token,
            body={"query": "x", "limit": "abc"},
        )
        assert status == 400
        assert data["error"] == t("companion.errors.integer_required", locale="fr", field="limit")

        assert server.exclusive_lock.acquire(blocking=False)
        try:
            status, data, _h = _request(
                port, "POST", "/api/sync", token=token, body={"rebuild": False}
            )
        finally:
            server.exclusive_lock.release()
        assert status == 409
        assert data["error"] == t("companion.errors.busy", locale="fr")

        status, html, headers = _request(port, "GET", "/")
    assert status == 200
    csp = headers["content-security-policy"]
    assert "form-action 'self'" in csp
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in html
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    handler_class = server.RequestHandlerClass
    assert getattr(handler_class, "timeout", None)


# ---------------------------------------------------------------------------
# D-14: fallback labels in the HTML, confirmations, structured panels, focus
# ---------------------------------------------------------------------------


def test_html_carries_fallback_labels_confirmations_and_safe_defaults() -> None:
    """Labels render without /api/state; destructive actions confirm; keep_source is on."""
    bundle = companion_i18n_bundle("en")
    html = render_companion_html("tok", i18n=bundle, lang="en", nonce="n0nce")
    assert '<html lang="en">' in html
    assert f">{bundle['vaults']['save_topology']}</button>" in html
    assert re.search(r'id="chk-keep-source" checked', html)
    assert re.search(r'id="chk-migrate-clients" checked', html)
    assert "window.confirm" in _js_function(html, "runMigration")
    assert "compaction_apply_confirm" in html
    assert "heading.focus()" in _js_function(html, "activateTab")
    assert "JSON.stringify(out" not in _js_function(html, "renderGraph")
    assert "JSON.stringify(\n    state.mcp_targets" not in html


# ---------------------------------------------------------------------------
# M: migration lock, atomic cleanup, resume, client reconfiguration
# ---------------------------------------------------------------------------


def _single_vault(root: Path, note_text: str = "# Fact\n\nv1\n") -> Path:
    initialize_vault(root)
    (root / MEMORY_SUBFOLDER_NAME).mkdir()
    (root / MEMORY_SUBFOLDER_NAME / "fact.md").write_text(note_text, encoding="utf-8")
    (root / "guide.md").write_text("# Guide\n", encoding="utf-8")
    return root


def test_migration_refuses_pending_operations(tmp_path: Path) -> None:
    """A pending journal entry stops the migration before anything is copied."""
    docs = _single_vault(tmp_path / "docs")
    pending_dir = docs / SIDECAR_DIR_NAME / OPLOG_DIR_NAME / OPLOG_PENDING_DIR_NAME
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "op-1.json").write_text("{}", encoding="utf-8")
    memory = tmp_path / "memory"

    with pytest.raises(MigrationError):
        asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=False))
    assert not (memory / MEMORY_SUBFOLDER_NAME / "fact.md").exists()
    assert (docs / MEMORY_SUBFOLDER_NAME / "fact.md").is_file()


def test_migration_holds_the_source_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No single-vault writer can take the mutation lock between copy and cleanup."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    observed: list[str] = []
    real_rebuild = rebuild_index_atomic

    async def _probing_rebuild(*args: Any, **kwargs: Any) -> Any:
        settings = Settings(
            vault_root=docs,
            read_paths=[docs],
            write_paths=[docs],
            vault_lock_timeout_seconds=_SHORT_LOCK_TIMEOUT_SECONDS,
        )
        writer = FilesystemVaultWriter(docs.resolve(), settings)
        try:
            with writer._advisory_lock("mutation"):
                observed.append("acquired")
        except VaultLockBusyError:
            observed.append("busy")
        return await real_rebuild(*args, **kwargs)

    monkeypatch.setattr("datacron.migration.rebuild_index_atomic", _probing_rebuild)
    asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))
    assert observed == ["busy"]


def test_migration_cleanup_is_all_or_nothing_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup that cannot move `_memory/` puts every entry back and is only a warning.

    The copy and the reindex succeeded, so the call returns the migration; a cleanup-only
    rerun then removes the source.
    """
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    real_replace = os.replace
    memory_source = os.path.normcase(str((docs / MEMORY_SUBFOLDER_NAME).resolve()))

    def _failing_replace(src: Any, dst: Any) -> None:
        if os.path.normcase(os.path.abspath(os.fspath(src))) == memory_source:
            raise PermissionError("a note is open in an editor")
        real_replace(src, dst)

    monkeypatch.setattr("datacron.migration.os.replace", _failing_replace)
    result = asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=False))
    monkeypatch.setattr("datacron.migration.os.replace", real_replace)
    assert result.source_cleaned is False
    assert any("editor" in warning for warning in result.warnings)
    assert result.verified_files >= 1
    assert (docs / MEMORY_SUBFOLDER_NAME / "fact.md").is_file()
    assert _vault_yaml(docs).is_file()
    assert not [p for p in (docs / SIDECAR_DIR_NAME).iterdir() if "tombstone" in p.name]

    cleaned = asyncio.run(migrate_vault_to_dual(docs, memory, cleanup_only=True))
    assert cleaned.source_cleaned is True
    assert not (docs / MEMORY_SUBFOLDER_NAME).exists()
    assert not (docs / SIDECAR_DIR_NAME).exists()
    assert (memory / MEMORY_SUBFOLDER_NAME / "fact.md").read_text(encoding="utf-8").endswith("v1\n")


def test_migration_rollback_failure_names_what_is_left_where(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move that cannot be undone is named with its current location; nothing is deleted."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    real_replace = os.replace
    memory_source = os.path.normcase(str((docs / MEMORY_SUBFOLDER_NAME).resolve()))
    sidecar = os.path.normcase(str((docs / SIDECAR_DIR_NAME).resolve()))

    def _failing_replace(src: Any, dst: Any) -> None:
        src_key = os.path.normcase(os.path.abspath(os.fspath(src)))
        dst_key = os.path.normcase(os.path.abspath(os.fspath(dst)))
        if src_key == memory_source:
            raise PermissionError("a note is open in an editor")
        if "tombstone" in src_key and dst_key == os.path.normcase(
            os.path.join(sidecar, "VAULT.yaml")
        ):
            raise PermissionError("VAULT.yaml is open")
        real_replace(src, dst)

    monkeypatch.setattr("datacron.migration.os.replace", _failing_replace)
    result = asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=False))
    monkeypatch.setattr("datacron.migration.os.replace", real_replace)
    assert result.source_cleaned is False
    stranded = [warning for warning in result.warnings if " <- " in warning]
    assert stranded, result.warnings
    assert "VAULT.yaml" in stranded[0]
    assert (docs / MEMORY_SUBFOLDER_NAME / "fact.md").is_file()


def test_migration_reports_an_incomplete_tombstone_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leftovers of the deletion are a warning, and the source is not reported cleaned."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    monkeypatch.setattr("datacron.migration._delete_tree", lambda _root: ["locked.bin"])
    result = asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=False))
    assert result.source_cleaned is False
    assert any("locked.bin" in warning for warning in result.warnings)


def test_migration_delete_tree_clears_read_only_files(tmp_path: Path) -> None:
    """A read-only file in the tombstone is deleted, not left behind."""
    root = tmp_path / "tombstone"
    (root / "sub").mkdir(parents=True)
    locked = root / "sub" / "ro.md"
    locked.write_text("x", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    assert migration_module._delete_tree(root) == []
    assert not root.exists()


def test_cleanup_only_removes_the_source_after_dual_vault_writes(tmp_path: Path) -> None:
    """After the memory vault is written to, a full rerun refuses but a cleanup-only run works."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))
    oplog = memory / SIDECAR_DIR_NAME / OPLOG_DIR_NAME / "operations.jsonl"
    oplog.parent.mkdir(parents=True, exist_ok=True)
    oplog.write_text('{"after": "switch"}\n', encoding="utf-8")
    (docs / SIDECAR_DIR_NAME / OPLOG_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (docs / SIDECAR_DIR_NAME / OPLOG_DIR_NAME / "operations.jsonl").write_text(
        '{"before": "switch"}\n', encoding="utf-8"
    )
    (memory / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nedited\n", encoding="utf-8")

    cleaned = asyncio.run(migrate_vault_to_dual(docs, memory, cleanup_only=True))
    assert cleaned.source_cleaned is True
    assert not (docs / MEMORY_SUBFOLDER_NAME).exists()
    assert (
        (memory / MEMORY_SUBFOLDER_NAME / "fact.md")
        .read_text(encoding="utf-8")
        .endswith("edited\n")
    )


def test_cleanup_only_refuses_source_files_missing_from_the_memory_vault(tmp_path: Path) -> None:
    """A source note changed after the copy, and not in the memory vault, blocks the cleanup."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    with pytest.raises(MigrationError):
        asyncio.run(migrate_vault_to_dual(docs, memory, cleanup_only=True))

    asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))
    (docs / MEMORY_SUBFOLDER_NAME / "late.md").write_text("# Late\n", encoding="utf-8")
    (docs / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nsource v2\n", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"late\.md") as excinfo:
        asyncio.run(migrate_vault_to_dual(docs, memory, cleanup_only=True))
    assert "fact.md" in str(excinfo.value)
    assert (docs / MEMORY_SUBFOLDER_NAME / "late.md").is_file()


def test_migration_resumes_over_its_own_previous_copy(tmp_path: Path) -> None:
    """A rerun refreshes files it wrote itself, and still refuses somebody else's file."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"
    asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))
    (docs / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nv2\n", encoding="utf-8")

    rerun = asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))
    assert (memory / MEMORY_SUBFOLDER_NAME / "fact.md").read_text(encoding="utf-8").endswith("v2\n")
    assert rerun.resumed_files >= 1

    (memory / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nedited\n", encoding="utf-8")
    (docs / MEMORY_SUBFOLDER_NAME / "fact.md").write_text("# Fact\n\nv3\n", encoding="utf-8")
    with pytest.raises(MigrationError):
        asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=True))


def test_migration_keeps_the_source_when_client_reconfiguration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clients are reconfigured before cleanup; a failure keeps the source and warns."""
    docs = _single_vault(tmp_path / "docs")
    memory = tmp_path / "memory"

    async def _failing_setup(_plan: Any) -> Any:
        return SimpleNamespace(client_installs=[], client_errors=["claude-desktop: denied"])

    monkeypatch.setattr("datacron.migration.run_setup", _failing_setup)
    result = asyncio.run(migrate_vault_to_dual(docs, memory, keep_source=False, client="all"))
    assert result.source_cleaned is False
    assert result.warnings
    assert (docs / MEMORY_SUBFOLDER_NAME / "fact.md").is_file()


def _fake_result(vault: Path, memory: Path, *, source_cleaned: bool = False) -> MigrationResult:
    return MigrationResult(
        vault_path=vault,
        memory_vault_path=memory,
        copied_sidecar_files=1,
        copied_memory_files=1,
        verified_files=2,
        indexed_notes=1,
        indexed_chunks=1,
        source_cleaned=source_cleaned,
    )


class _FakeClients:
    """Stand-in for client discovery and setup: records plans, never writes a config."""

    def __init__(self, settings: dict[str, tuple[bool, bool] | None]) -> None:
        self.settings = settings
        self.plans: list[Any] = []

    def discover(self, *, scopes: tuple[str, ...], project_dir: Path) -> list[Any]:
        return [SimpleNamespace(client_id=client_id) for client_id in self.settings]

    def existing(self, _vault: Path, client: str, _scope: str) -> tuple[bool, bool] | None:
        return self.settings.get(client)

    async def setup(self, plan: Any) -> Any:
        self.plans.append(plan)
        return SimpleNamespace(client_installs=[], client_errors=[], stdio_config=None)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(companion_api, "discover_targets", self.discover)
        monkeypatch.setattr(companion_api, "existing_client_settings", self.existing)
        monkeypatch.setattr(companion_api, "run_setup", self.setup)


def test_companion_migration_defaults_keep_source_and_reconfigure_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 3: keep the source and reconfigure the detected clients unless told otherwise."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    memory = tmp_path / "memory"
    profile_path = _profile(tmp_path, vault)
    calls: list[dict[str, Any]] = []

    async def _fake(vault_root: Path, memory_root: Path, **kwargs: Any) -> MigrationResult:
        calls.append(kwargs)
        return _fake_result(
            vault_root, memory_root, source_cleaned=bool(kwargs.get("cleanup_only"))
        )

    monkeypatch.setattr(companion_api, "migrate_vault_to_dual", _fake)
    clients = _FakeClients({"cursor": (True, False)})
    clients.install(monkeypatch)
    payload = {"vault_path": str(vault), "memory_vault_path": str(memory)}
    out = asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    assert calls == [{"keep_source": True, "client": "none"}]
    assert [plan.install_scope for plan in clients.plans] == ["user"]
    assert out["migration"]["source_cleaned"] is False

    with pytest.raises(MigrationError):
        asyncio.run(
            run_companion_migration({**payload, "keep_source": False}, profile_path=profile_path)
        )
    assert len(calls) == 1

    confirmed = {**payload, "keep_source": False, "confirm_cleanup": True}
    out = asyncio.run(run_companion_migration(confirmed, profile_path=profile_path))
    assert calls[-1] == {"cleanup_only": True}
    assert out["migration"]["source_cleaned"] is True


def test_companion_migration_keeps_each_client_write_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writable client stays writable, a read-only or unregistered one gets no write access."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    memory = tmp_path / "memory"
    profile_path = _profile(tmp_path, vault, enable_write=True)

    async def _fake(vault_root: Path, memory_root: Path, **_kwargs: Any) -> MigrationResult:
        return _fake_result(vault_root, memory_root)

    monkeypatch.setattr(companion_api, "migrate_vault_to_dual", _fake)
    clients = _FakeClients({"cursor": (True, False), "lmstudio": (False, True), "codex-cli": None})
    clients.install(monkeypatch)
    payload = {"vault_path": str(vault), "memory_vault_path": str(memory)}
    asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    by_client = {plan.client: plan for plan in clients.plans}
    assert set(by_client) == {"cursor", "lmstudio", "codex-cli"}
    assert by_client["cursor"].enable_write is True
    assert by_client["cursor"].write_paths
    assert by_client["lmstudio"].enable_write is False
    assert by_client["lmstudio"].read_only is True
    assert by_client["codex-cli"].enable_write is False
    assert by_client["codex-cli"].write_paths == []

    single = _FakeClients({"cursor": (False, False), "lmstudio": None})
    single.install(monkeypatch)
    asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    assert [(plan.client, plan.enable_write) for plan in single.plans] == [("all", False)]


def test_companion_cleanup_only_is_exposed_and_needs_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Companion finishes a kept-source migration through the engine's cleanup-only mode."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    memory = tmp_path / "memory"
    profile_path = _profile(tmp_path, vault)
    calls: list[dict[str, Any]] = []

    async def _fake(vault_root: Path, memory_root: Path, **kwargs: Any) -> MigrationResult:
        calls.append(kwargs)
        return _fake_result(vault_root, memory_root, source_cleaned=True)

    monkeypatch.setattr(companion_api, "migrate_vault_to_dual", _fake)
    payload = {"vault_path": str(vault), "memory_vault_path": str(memory), "cleanup_only": True}
    with pytest.raises(MigrationError):
        asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    out = asyncio.run(
        run_companion_migration({**payload, "confirm_cleanup": True}, profile_path=profile_path)
    )
    assert calls == [{"cleanup_only": True}]
    assert out["migration"]["source_cleaned"] is True
    html = render_companion_html("tok")
    assert "window.confirm" in _js_function(html, "runCleanupOnly")


def test_companion_migration_maps_os_errors_and_survives_profile_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError is a 400 with recovery guidance; a profile race never hides the result."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    memory = tmp_path / "memory"
    profile_path = _profile(tmp_path, vault, ui_language="en")
    payload = {"vault_path": str(vault), "memory_vault_path": str(memory), "client": "none"}

    async def _denied(*_args: Any, **_kwargs: Any) -> MigrationResult:
        raise PermissionError("access denied: datacron.db")

    monkeypatch.setattr(companion_api, "migrate_vault_to_dual", _denied)
    with pytest.raises(MigrationError, match="access denied") as excinfo:
        asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    assert "run the migration again" in str(excinfo.value)

    async def _racing(vault_root: Path, memory_root: Path, **_kwargs: Any) -> MigrationResult:
        current = read_companion_profile(profile_path)
        write_companion_profile_cas(
            {"vault_path": str(vault), "ui_language": "fr"},
            path=profile_path,
            expected_hash=current.content_hash,
        )
        return _fake_result(vault_root, memory_root)

    monkeypatch.setattr(companion_api, "migrate_vault_to_dual", _racing)
    result = asyncio.run(run_companion_migration(payload, profile_path=profile_path))
    assert result["migration"]["verified_files"] == 2
    stored = read_companion_profile(profile_path).profile
    assert stored.memory_vault_path == str(memory)
    assert stored.ui_language == "fr"
