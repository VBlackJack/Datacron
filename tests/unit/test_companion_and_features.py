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
"""Unit and contract tests for Graph Health, Memory Compaction, Config CAS, and Companion GUI."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from datacron.bootstrap import initialize_vault
from datacron.cli import app as cli_app
from datacron.companion import (
    DARK_THEME_COLORS,
    TOKEN_HEADER_NAME,
    WCAG_AA_NON_TEXT_RATIO,
    WCAG_AA_TEXT_RATIO,
    create_companion_server,
    verify_theme_wcag_aa,
)
from datacron.companion.api import companion_i18n_bundle
from datacron.companion.ui import render_companion_html
from datacron.config_contract import (
    EXIT_CAS_CONFLICT,
    EXIT_VALIDATION_ERROR,
    HASH_ABSENT,
    ConfigCasConflictError,
    ConfigValidationError,
    read_vault_config_snapshot,
    write_vault_config_cas,
)
from datacron.core.config import Settings
from datacron.graph_health import analyze_vault_graph
from datacron.memory_compaction import compact_vault_memory


def test_companion_theme_wcag_aa_contrast_and_design_tokens() -> None:
    """All Dracula theme pairs satisfy WCAG 2.1 AA and HTML uses CSS custom properties."""
    ratios = verify_theme_wcag_aa()
    assert ratios["text_primary_on_background"] >= WCAG_AA_TEXT_RATIO
    assert ratios["text_secondary_on_background"] >= WCAG_AA_TEXT_RATIO
    assert ratios["text_secondary_on_card"] >= WCAG_AA_TEXT_RATIO
    assert ratios["border_on_background"] >= WCAG_AA_NON_TEXT_RATIO
    assert ratios["focus_on_background"] >= WCAG_AA_NON_TEXT_RATIO

    html = render_companion_html("test-session-token")
    for color_hex in DARK_THEME_COLORS.values():
        assert color_hex in html
    # Strip the :root CSS block and verify no raw hex colors exist in the rest of the document
    without_root = re.sub(r":root\s*\{[^}]*\}", "", html, count=1)
    assert re.findall(r"#[0-9A-Fa-f]{6}\b", without_root) == []


def test_companion_i18n_en_fr_parity() -> None:
    """Every Companion UI translation key is populated in both English and French."""
    en_bundle = companion_i18n_bundle("en")
    fr_bundle = companion_i18n_bundle("fr")
    assert set(en_bundle.keys()) == set(fr_bundle.keys())
    for section, en_keys in en_bundle.items():
        fr_keys = fr_bundle[section]
        assert set(en_keys.keys()) == set(fr_keys.keys())
        for key in en_keys:
            assert en_keys[key].strip() != ""
            assert fr_keys[key].strip() != ""


def test_config_contract_cas_and_cli(tmp_path: Path) -> None:
    """VAULT.yaml Compare-And-Swap contract enforces expected_hash and deterministic exit codes."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)

    initial = read_vault_config_snapshot(vault)
    assert initial.present is False
    assert initial.content_hash == HASH_ABSENT

    written = write_vault_config_cas(
        vault,
        {"excluded_folders": [".git", "_private"], "archive_tags": ["archive/old"]},
        expect_absent=True,
    )
    assert written.present is True
    assert written.content_hash.startswith("sha256:")
    assert ".git" in written.values["excluded_folders"]

    with pytest.raises(ConfigCasConflictError):
        write_vault_config_cas(
            vault,
            {"excluded_folders": [".git"]},
            expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )

    with pytest.raises(ConfigValidationError):
        write_vault_config_cas(
            vault,
            {"unknown_field": 123},
            expected_hash=written.content_hash,
        )

    runner = CliRunner()
    get_res = runner.invoke(cli_app, ["config", "get", str(vault), "--json"])
    assert get_res.exit_code == 0
    get_payload = json.loads(get_res.stdout)
    assert get_payload["content_hash"] == written.content_hash

    conflict_res = runner.invoke(
        cli_app,
        [
            "config",
            "set",
            str(vault),
            "--expected-hash",
            "sha256:deadbeef",
            "--values-json",
            json.dumps({"excluded_folders": ["drafts"]}),
        ],
    )
    assert conflict_res.exit_code == EXIT_CAS_CONFLICT

    invalid_res = runner.invoke(
        cli_app,
        [
            "config",
            "set",
            str(vault),
            "--values-json",
            "not-json",
        ],
    )
    assert invalid_res.exit_code == EXIT_VALIDATION_ERROR

    ok_res = runner.invoke(
        cli_app,
        [
            "config",
            "set",
            str(vault),
            "--expected-hash",
            written.content_hash,
            "--values-json",
            json.dumps({"excluded_folders": [".git", "temp"]}),
            "--json",
        ],
    )
    assert ok_res.exit_code == 0
    ok_payload = json.loads(ok_res.stdout)
    assert "temp" in ok_payload["values"]["excluded_folders"]


def test_graph_health_broken_links_orphans_hubs_and_cross_vault(tmp_path: Path) -> None:
    """Graph health analyzer detects broken links, orphans, hubs, and cross-vault links."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    (docs_vault / "Architecture").mkdir(parents=True)
    (docs_vault / "Architecture" / "core.md").write_text(
        "---\nid: 01J00000000000000000000101\ntitle: Core Architecture\n---\n"
        "# Core Architecture\n\nCentral hub note.\n",
        encoding="utf-8",
    )
    (docs_vault / "Architecture" / "api.md").write_text(
        "---\nid: 01J00000000000000000000102\ntitle: API Layer\n---\n"
        "# API Layer\n\nDepends on [[Core Architecture]] and [[Missing Spec]].\n",
        encoding="utf-8",
    )
    (docs_vault / "Architecture" / "orphan.md").write_text(
        "---\nid: 01J00000000000000000000103\ntitle: Isolated Note\n---\n"
        "# Isolated Note\n\nNo incoming or outgoing links.\n",
        encoding="utf-8",
    )
    initialize_vault(memory_vault)
    (memory_vault / "_memory" / "decisions").mkdir(parents=True, exist_ok=True)
    (memory_vault / "_memory" / "decisions" / "adr1.md").write_text(
        "---\nid: 01J00000000000000000000104\ntitle: ADR 1\n---\n"
        "# ADR 1\n\nReferences [[Core Architecture]] from memory vault.\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory"],
    )
    report = asyncio.run(analyze_vault_graph(docs_vault, settings=settings))
    assert report.broken_link_count == 1
    assert report.broken_links[0].target_alias == "Missing Spec"
    assert report.broken_links[0].source_rel_path == "Architecture/api.md"

    orphan_paths = [o.rel_path for o in report.orphan_notes]
    assert "Architecture/orphan.md" in orphan_paths

    assert report.cross_vault_link_count == 1
    assert report.cross_vault_links[0].source_vault == "memory"
    assert report.cross_vault_links[0].target_vault == "docs"
    assert report.cross_vault_links[0].target_rel_path == "Architecture/core.md"

    assert report.hub_notes[0].rel_path == "Architecture/core.md"
    assert report.hub_notes[0].incoming_count == 2

    runner = CliRunner()
    cli_res = runner.invoke(
        cli_app,
        [
            "graph-health",
            str(docs_vault),
            "--memory-vault",
            str(memory_vault),
            "--json",
        ],
    )
    assert cli_res.exit_code == 0
    cli_payload = json.loads(cli_res.stdout)
    assert cli_payload["broken_link_count"] == 1
    assert cli_payload["cross_vault_link_count"] == 1


def test_memory_compaction_dry_run_and_apply_monthly_digest(tmp_path: Path) -> None:
    """Memory compaction groups older journals into monthly digests and tags sources."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    docs_vault.mkdir(parents=True)
    initialize_vault(memory_vault)
    journal_dir = memory_vault / "_memory" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)

    (journal_dir / "2026-01-10.md").write_text(
        "---\nid: 01J00000000000000000000201\ntitle: 2026-01-10\n---\n"
        "# 2026-01-10\n\nFirst January entry.\n",
        encoding="utf-8",
    )
    (journal_dir / "2026-01-22.md").write_text(
        "---\nid: 01J00000000000000000000202\ntitle: 2026-01-22\n---\n"
        "# 2026-01-22\n\nSecond January entry.\n",
        encoding="utf-8",
    )
    (journal_dir / "2026-03-10.md").write_text(
        "---\nid: 01J00000000000000000000203\ntitle: 2026-03-10\n---\n"
        "# 2026-03-10\n\nRecent March entry kept uncompacted.\n",
        encoding="utf-8",
    )

    settings = Settings(
        vault_root=docs_vault,
        memory_root=memory_vault,
        read_paths=[docs_vault, memory_vault],
        write_paths=[memory_vault / "_memory"],
    )
    ref_date = date(2026, 3, 15)

    preview = asyncio.run(
        compact_vault_memory(
            docs_vault,
            settings=settings,
            older_than_days=30,
            dry_run=True,
            reference_date=ref_date,
        )
    )
    assert preview.dry_run is True
    assert len(preview.candidate_journals) == 2
    assert len(preview.digests_planned) == 1
    assert preview.digests_planned[0].digest_rel_path == "_memory/archive/journal-2026-01.md"
    assert not (memory_vault / "_memory" / "archive" / "journal-2026-01.md").exists()

    applied = asyncio.run(
        compact_vault_memory(
            docs_vault,
            settings=settings,
            older_than_days=30,
            dry_run=False,
            reference_date=ref_date,
        )
    )
    assert applied.dry_run is False
    assert applied.compacted_count == 2
    assert applied.digests_written == 1

    digest_file = memory_vault / "_memory" / "archive" / "journal-2026-01.md"
    assert digest_file.is_file()
    digest_text = digest_file.read_text(encoding="utf-8")
    assert "First January entry." in digest_text
    assert "Second January entry." in digest_text
    assert "01J00000000000000000000201" in digest_text

    src_text = (journal_dir / "2026-01-10.md").read_text(encoding="utf-8")
    assert "archive/compacted" in src_text
    assert "_memory/archive/journal-2026-01.md" in src_text

    # Running compaction again skips already-compacted notes
    second_pass = asyncio.run(
        compact_vault_memory(
            docs_vault,
            settings=settings,
            older_than_days=30,
            dry_run=True,
            reference_date=ref_date,
        )
    )
    assert second_pass.candidate_journals == []


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    if token is not None:
        headers[TOKEN_HEADER_NAME] = token
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_companion_http_server_endpoints_and_csrf_protection(tmp_path: Path) -> None:
    """Companion HTTP server enforces session token and serves all 7 tab workflows."""
    docs_vault = tmp_path / "docs-vault"
    memory_vault = tmp_path / "memory-vault"
    profile_file = tmp_path / "companion.json"
    docs_vault.mkdir(parents=True)
    (docs_vault / "guide.md").write_text(
        "---\nid: 01J00000000000000000000301\ntitle: Guide\n---\n"
        "# Guide\n\nCompanion search target.\n",
        encoding="utf-8",
    )

    server, base_url, token = create_companion_server(
        port=0,
        profile_path=profile_file,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(base_url, timeout=10) as html_resp:  # noqa: S310
            assert html_resp.status == int(HTTPStatus.OK)
            html_body = html_resp.read().decode("utf-8")
            assert token in html_body

        # Missing token is rejected with 403 Forbidden
        status_no_tok, _ = _http_json(f"{base_url}api/state", token=None)
        assert status_no_tok == int(HTTPStatus.FORBIDDEN)

        # Initial state load
        status_init, state_init = _http_json(f"{base_url}api/state", token=token)
        assert status_init == int(HTTPStatus.OK)
        assert state_init["profile_snapshot"]["content_hash"] == HASH_ABSENT

        # Save Dual-Vault topology
        status_top, state_after_top = _http_json(
            f"{base_url}api/topology",
            method="POST",
            token=token,
            body={
                "expected_hash": HASH_ABSENT,
                "profile": {
                    "vault_path": str(docs_vault),
                    "memory_vault_path": str(memory_vault),
                    "extra_read_vaults": {},
                    "ui_language": "fr",
                    "cli_timeout_seconds": 30,
                    "enable_write": True,
                    "durability": "best-effort",
                },
            },
        )
        assert status_top == int(HTTPStatus.OK)
        assert state_after_top["readiness"]["vault_mode"] == "dual"

        # Synchronize index
        status_sync, sync_data = _http_json(
            f"{base_url}api/sync",
            method="POST",
            token=token,
            body={"rebuild": False},
        )
        assert status_sync == int(HTTPStatus.OK)
        assert sync_data["stats"]["reindexed_notes"] >= 1

        # Search via Companion API
        status_search, search_data = _http_json(
            f"{base_url}api/search",
            method="POST",
            token=token,
            body={"query": "Companion", "folder": "", "mode": "fts"},
        )
        assert status_search == int(HTTPStatus.OK)
        assert len(search_data["results"]) == 1
        assert search_data["results"][0]["source_vault"] == "docs"

        # Graph health via Companion API
        status_graph, graph_data = _http_json(
            f"{base_url}api/graph-health",
            method="POST",
            token=token,
            body={},
        )
        assert status_graph == int(HTTPStatus.OK)
        assert graph_data["total_notes"] >= 1

        # Save VAULT.yaml with CAS conflict test
        status_conflict, _ = _http_json(
            f"{base_url}api/vault-config",
            method="POST",
            token=token,
            body={
                "expected_hash": "sha256:bad",
                "values": {"excluded_folders": [".git"], "archive_tags": [], "query_expansion": {}},
            },
        )
        assert status_conflict == int(HTTPStatus.CONFLICT)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
