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
"""Regression tests for Audit 2026-10-17 Lot 4 (Companion, CLI, and Setup)."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
import pytest
from typer.testing import CliRunner

from datacron.bootstrap import initialize_vault
from datacron.cli import app
from datacron.companion.api import (
    _build_runtime_settings,
    get_companion_graph_health,
    get_companion_history,
    run_companion_memory_compaction,
    run_companion_migration,
    run_companion_sync,
    search_companion,
)
from datacron.companion.server import TOKEN_HEADER_NAME, create_companion_server
from datacron.companion.ui import render_companion_html
from datacron.config_contract import (
    CompanionProfile,
    read_companion_profile,
    write_companion_profile_cas,
    write_vault_config_cas,
)
from datacron.core.operation_log import OperationJournal, OperationRecord
from datacron.i18n import t
from datacron.migration import MigrationResult
from datacron.setup_wizard import SetupPlan, run_setup


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    data: dict[str, Any]
    headers: dict[str, str]


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> _HttpResponse:
    headers: dict[str, str] = {}
    if token is not None:
        headers[TOKEN_HEADER_NAME] = token
    raw_body: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw_body = json.dumps(body).encode("utf-8")
    req = Request(url, data=raw_body, headers=headers, method=method)  # noqa: S310
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return _HttpResponse(
                status=int(resp.status),
                data=json.loads(resp.read().decode("utf-8")),
                headers=resp_headers,
            )
    except HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return _HttpResponse(
            status=int(exc.code),
            data=json.loads(exc.read().decode("utf-8")),
            headers=resp_headers,
        )


def test_companion_runtime_settings_sanitizes_memory_and_extra_vaults(tmp_path: Path) -> None:
    """AUD-20261017-06: _build_runtime_settings handles identical roots and invalid prefixes."""
    vault = tmp_path / "vault"
    vault.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()

    profile = CompanionProfile(
        vault_path=str(vault),
        memory_vault_path=str(vault),
        extra_read_vaults={
            "_memory": str(extra),
            "bad/prefix": str(extra),
            "good_prefix": str(extra),
        },
    )
    resolved_vault, settings = _build_runtime_settings(profile)
    assert resolved_vault == vault.resolve()
    assert settings.memory_root is None
    assert list(settings.extra_read_vaults.keys()) == ["good_prefix"]


def test_companion_api_validates_integer_parameters(tmp_path: Path) -> None:
    """AUD-20261017-06: Non-integer, boolean, or out-of-range parameters return 400."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault)},
        path=profile_path,
        expect_absent=True,
    )
    server, url, token = create_companion_server(
        host="127.0.0.1",
        port=0,
        profile_path=profile_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cases: list[tuple[str, dict[str, Any]]] = [
            ("api/search", {"limit": "not-an-int"}),
            ("api/search", {"limit": True}),
            ("api/search", {"limit": 0}),
            ("api/search", {"limit": 9999}),
            ("api/notes", {"limit": -1}),
            ("api/notes", {"offset": -5}),
            ("api/history", {"limit": "bad"}),
            ("api/compact-memory", {"older_than_days": -1}),
            ("api/compact-memory", {"older_than_days": "nan"}),
        ]
        for endpoint, body in cases:
            r = _http_json(
                f"{url}{endpoint}",
                method="POST",
                token=token,
                body=body,
            )
            assert r.status == 400, f"Expected 400 for {endpoint} {body}, got {r.status}"

        r_null = _http_json(
            f"{url}api/search",
            method="POST",
            token=token,
            body={"limit": None},
        )
        assert r_null.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_companion_history_filters_excluded_and_redacts_secrets(tmp_path: Path) -> None:
    """AUD-20261017-06: History endpoint filters excluded folders and redacts secret paths."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    write_vault_config_cas(
        vault,
        {"excluded_folders": ["Private"], "archive_tags": [], "query_expansion": {}},
    )
    journal = OperationJournal(vault, retention_days=30, history_mode="full")
    journal.append_record(
        OperationRecord(
            operation_id="01J00000000000000000000001",
            timestamp="2026-10-17T10:00:00Z",
            op="create",
            tool="create_note_ai",
            note_id="01J00000000000000000000001",
            rel_path="Private/secret-plan.md",
            before_hash=None,
            after_hash="a" * 64,
            actor="test",
            parameters={},
            history_stored=False,
        )
    )
    secret_token = "ghp_" + "A" * 36
    journal.append_record(
        OperationRecord(
            operation_id="01J00000000000000000000002",
            timestamp="2026-10-17T10:01:00Z",
            op="create",
            tool="create_note_ai",
            note_id="01J00000000000000000000002",
            rel_path=f"Notes/{secret_token}.md",
            before_hash=None,
            after_hash="b" * 64,
            actor="test",
            parameters={},
            history_stored=False,
        )
    )
    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault)},
        path=profile_path,
        expect_absent=True,
    )
    res = asyncio.run(get_companion_history({}, profile_path=profile_path))
    ops = res["operations"]
    assert len(ops) == 1
    assert "Private/secret-plan.md" not in [op["rel_path"] for op in ops]
    assert secret_token not in ops[0]["rel_path"]
    assert "[REDACTED" in ops[0]["rel_path"]


def test_companion_graph_health_and_compaction_redact_secrets(tmp_path: Path) -> None:
    """AUD-20261017-06: Graph health and memory compaction redact secret tokens."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    secret_token = "ghp_" + "B" * 36
    note_dir = vault / "_memory" / "journal"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "2020-01-01.md").write_text(
        f"---\nid: 01J00000000000000000000099\ntitle: Leak {secret_token}\n---\n"
        f"# Leak {secret_token}\n\nBroken link to [[Missing {secret_token}]].\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault)},
        path=profile_path,
        expect_absent=True,
    )
    health_res = asyncio.run(get_companion_graph_health(profile_path=profile_path))
    assert secret_token not in json.dumps(health_res)

    compact_res = asyncio.run(
        run_companion_memory_compaction(
            {"older_than_days": 1, "dry_run": True},
            profile_path=profile_path,
        )
    )
    assert secret_token not in json.dumps(compact_res)


def test_companion_migration_enforces_cas_expected_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-20261017-06: run_companion_migration passes expected_hash to CAS writer."""
    vault = tmp_path / "vault"
    mem = tmp_path / "mem"
    initialize_vault(vault)
    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault)},
        path=profile_path,
        expect_absent=True,
    )

    async def _fake_migrate(*_args: Any, **_kwargs: Any) -> MigrationResult:
        write_companion_profile_cas(
            {"vault_path": str(vault), "ui_language": "en"},
            path=profile_path,
        )
        return MigrationResult(
            vault_path=vault,
            memory_vault_path=mem,
            copied_sidecar_files=1,
            copied_memory_files=0,
            verified_files=1,
            indexed_notes=0,
            indexed_chunks=0,
            source_cleaned=False,
        )

    monkeypatch.setattr("datacron.companion.api.migrate_vault_to_dual", _fake_migrate)
    # The profile changed during the migration: the write stays CAS-protected, so it is
    # merged onto the newer profile instead of overwriting it, and the migration that
    # already happened is still reported (2026-10-18 audit, finding M).
    result = asyncio.run(
        run_companion_migration(
            # No client reconfiguration: a test must never write a real client config.
            {"vault_path": str(vault), "memory_vault_path": str(mem), "client": "none"},
            profile_path=profile_path,
        )
    )
    assert result["migration"]["profile_updated"] is True
    merged = read_companion_profile(profile_path).profile
    assert merged.ui_language == "en"
    assert merged.memory_vault_path == str(mem)


def test_companion_server_hardening_host_csp_and_500_masking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-20261017-07: Empty Host rejected, CSP sent on /, and 500 masks internal exception."""
    profile_path = tmp_path / "companion.json"
    server, url, token = create_companion_server(
        host="127.0.0.1",
        port=0,
        profile_path=profile_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    parsed = urlparse(url)
    assert parsed.hostname is not None
    try:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        conn.putrequest("GET", "/", skip_host=True)
        conn.putheader("Host", "")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 403
        conn.close()

        conn2 = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        conn2.request("GET", "/")
        resp2 = conn2.getresponse()
        assert resp2.status == 200
        csp = resp2.getheader("Content-Security-Policy")
        assert csp is not None
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        conn2.close()

        async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("secret-internal-path-/var/private/db")

        monkeypatch.setitem(
            sys_modules_server()._POST_ROUTES,
            "/api/sync",
            _boom,
        )
        r500 = _http_json(f"{url}api/sync", method="POST", token=token, body={})
        assert r500.status == 500
        # The message comes from the catalog in the profile language (French by default).
        assert r500.data == {"error": t("companion.errors.internal", locale="fr")}
        assert "secret-internal-path" not in json.dumps(r500.data)
    finally:
        server.shutdown()
        server.server_close()


def sys_modules_server() -> Any:
    import datacron.companion.server as srv

    return srv


def test_companion_ui_accessibility_and_keyboard_support() -> None:
    """AUD-20261017-08: Companion UI includes ARIA attributes, keyboard handlers, and runAction."""
    html = render_companion_html("tok-123")
    assert "border: 1px solid transparent;" in html
    assert 'data-i18n-aria="search.query_placeholder"' in html
    assert 'data-i18n-aria="search.folder_placeholder"' in html
    assert 'aria-current="page"' in html
    assert "async function runAction(" in html
    assert 'document.documentElement.lang = currentLang || "en"' in html
    assert 'role="button" tabindex="0" data-jump="vaults"' in html


def test_cli_config_set_partial_merge_and_companion_vault_flag(tmp_path: Path) -> None:
    """AUD-20261017-09: config set merges partial updates and companion accepts --vault."""
    vault = tmp_path / "vault"
    initialize_vault(vault)
    write_vault_config_cas(
        vault,
        {"excluded_folders": ["Archive", "Drafts"], "archive_tags": [], "query_expansion": {}},
    )
    runner = CliRunner()
    r_get = runner.invoke(
        app,
        ["config", "get", "--target", "vault", "--vault", str(vault), "--json"],
    )
    assert r_get.exit_code == 0, r_get.output
    snap = json.loads(r_get.stdout)
    assert snap["values"]["excluded_folders"] == ["Archive", "Drafts"]

    r_set = runner.invoke(
        app,
        [
            "config",
            "set",
            "--target",
            "vault",
            "--vault",
            str(vault),
            "--expected-hash",
            snap["content_hash"],
            "--values-json",
            json.dumps({"query_expansion": {"k8s": ["kubernetes"]}}),
            "--json",
        ],
    )
    assert r_set.exit_code == 0, r_set.output
    updated = json.loads(r_set.stdout)
    assert updated["values"]["query_expansion"]["k8s"] == ["kubernetes"]
    assert updated["values"]["excluded_folders"] == ["Archive", "Drafts"]

    r_comp = runner.invoke(app, ["companion", "--help"], terminal_width=240)
    assert r_comp.exit_code == 0
    assert "--vault" in click.unstyle(r_comp.stdout)


def test_setup_wizard_build_index_honors_archive_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-20261017-10: `run_setup` passes `archive_tags` from VaultConfig to SQLiteFTS5Store."""
    from datacron.indexing.fts5_store import SQLiteFTS5Store as RealStore

    captured_kwargs: list[dict[str, Any]] = []

    def _spy_store(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.append(dict(kwargs))
        return RealStore(*args, **kwargs)

    monkeypatch.setattr("datacron.setup_wizard.SQLiteFTS5Store", _spy_store)

    vault = tmp_path / "vault"
    initialize_vault(vault)
    (vault / "old.md").write_text(
        "---\nid: 01J00000000000000000000055\ntitle: Old Note\ntags:\n  - archived\n---\n"
        "# Old Note\n\nUniqueArchivedKeywordToken in body.\n",
        encoding="utf-8",
    )
    plan = SetupPlan(
        vault_path=vault,
        build_index=True,
        enable_write=False,
        write_paths=[],
        client="none",
        install_scope="user",
    )
    res = asyncio.run(run_setup(plan))
    assert res.indexed_notes == 1
    assert len(captured_kwargs) == 1
    assert "archive_tags" in captured_kwargs[0]
    assert "meta/archive" in captured_kwargs[0]["archive_tags"]

    profile_path = tmp_path / "companion.json"
    write_companion_profile_cas(
        {"vault_path": str(vault)},
        path=profile_path,
        expect_absent=True,
    )
    assert read_companion_profile(profile_path).present is True
    search_res = asyncio.run(
        search_companion(
            {"query": "UniqueArchivedKeywordToken", "mode": "fts"},
            profile_path=profile_path,
        )
    )
    assert len(search_res["results"]) == 1
    sync_res = asyncio.run(run_companion_sync({"rebuild": False}, profile_path=profile_path))
    assert "stats" in sync_res
