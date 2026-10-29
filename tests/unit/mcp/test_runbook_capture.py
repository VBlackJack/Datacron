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
"""runbook_capture files a fact in its runbook, inside its own write boundary only."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from datacron.core.config import Settings
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app, create_server
from datacron.mcp.tools import _append_journal_impl
from datacron.mcp.tools.runbook import _runbook_capture_impl

_DAY = "2026-10-20"
_RUNBOOK = "CPDC/wsus-runbook"
_TROUBLESHOOTING = f"{_RUNBOOK}/50-troubleshooting.md"
_VARIABLES = f"{_RUNBOOK}/90-variables.md"
_ARCHITECTURE = f"{_RUNBOOK}/00-architecture.md"
_TROUBLESHOOTING_TEXT = (
    "# Troubleshooting\n\nState: written.\n\n## Symptom: A client stops updating\n\n"
    "**Cause.** Proxy bypass list.\n\n**Fix.** Add the name.\n\nmeasured 2026-09-01\n"
)
_VARIABLES_TEXT = (
    "# Variables\n\n## Servers\n\n| Placeholder | Meaning | On Mets |\n|---|---|---|\n"
    "| `<UPSTREAM>` | Upstream WSUS server | `tpcpdc03d` |\n"
)
_ARCHITECTURE_TEXT = (
    "---\ntitle: Architecture\nupdated: 2026-09-01T00:00:00+00:00\n---\n# Architecture\n"
)
_RUNBOOKS: dict[str, Any] = {
    "roots": [{"folder": "CPDC", "template": "infra"}],
    "templates": {
        "infra": {
            "language": "en",
            "readme": "README",
            "observation_label": "On Mets",
            "roles": {
                "architecture": {"stem": "00-architecture", "match": r"^\d{2}-architecture$"},
                "troubleshooting": {
                    "stem": "50-troubleshooting",
                    "match": r"^\d{2}-troubleshooting$",
                    "title": "Troubleshooting",
                },
                "escalation": {
                    "stem": "60-escalation",
                    "match": r"^\d{2}-.*escalation$",
                    "title": "Escalation",
                },
                "variables": {"stem": "90-variables", "match": r"^\d{2}-variables$"},
            },
            "labels": {
                "symptom": "Symptom",
                "cause": "Cause",
                "fix": "Fix",
                "not_captured": "Not captured",
                "update": "Update",
                "placeholder": "Placeholder",
                "meaning": "Meaning",
                "value": "On Mets",
            },
            "routing": {
                "error_fix": "troubleshooting",
                "server": "variables",
                "contact": "escalation",
                "fact": "architecture",
            },
        }
    },
}
_ORGANIZATION: dict[str, Any] = {
    "scope": "_memory",
    "rules": [{"tag": "type/fact", "folder": "_memory/facts"}],
    "tags": {
        "placement_namespace": "type",
        "subject_namespace": "project",
        "subjects": ["project/wsus", "project/ad"],
    },
}
_TRACE = re.compile(r"(?i)\b(?:origin|confidence|ai|assistant|claude|session)\b")


def _write(vault: Path, rel_path: str, text: str) -> None:
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


async def _open(
    vault: Path, *, runbook_write: bool = True, **overrides: Any
) -> AsyncIterator[DatacronApp]:
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if config_path.exists()
        else {}
    )
    document.update(runbooks=_RUNBOOKS, organization=_ORGANIZATION)
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    _write(vault, _TROUBLESHOOTING, _TROUBLESHOOTING_TEXT)
    _write(vault, _VARIABLES, _VARIABLES_TEXT)
    _write(vault, _ARCHITECTURE, _ARCHITECTURE_TEXT)
    (vault / _RUNBOOK / "confluence-source").mkdir()
    settings = Settings(
        read_paths=[vault],
        write_paths=[vault / "_memory"],
        runbook_write_paths=[vault / "CPDC"] if runbook_write else [],
        vault_root=vault,
        **overrides,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(settings=settings, vault_root=vault, chunker=MarkdownChunker(), store=store)
    finally:
        await store.close()


@pytest.fixture
async def app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    async for opened in _open(tmp_vault):
        yield opened


async def _capture(app: DatacronApp, kind: str, evidence: str = "measured", **kwargs: Any) -> Any:
    subject = kwargs.pop("subject", "wsus")
    return await _runbook_capture_impl(
        app,
        subject=subject,
        kind=kind,
        evidence=evidence,
        source=kwargs.pop("source", "incident 2026-10-20"),
        fields=kwargs.pop("fields"),
        observed_on=kwargs.pop("observed_on", _DAY),
        **kwargs,
    )


def _read(app: DatacronApp, rel_path: str) -> str:
    return (app.vault_root / rel_path).read_bytes().decode("utf-8")


# --- registration -------------------------------------------------------------


async def test_the_tool_is_offered_only_when_runbooks_and_their_write_paths_exist(
    tmp_vault: Path, tmp_path: Path
) -> None:
    async for configured in _open(tmp_vault):
        assert "runbook_capture" in {
            tool.name for tool in await create_server(configured).list_tools()
        }
    bare = tmp_path / "bare"
    bare.mkdir()
    plain = build_app(settings=Settings(read_paths=[bare], vault_root=bare), vault_root=bare)
    assert "runbook_capture" not in {tool.name for tool in await create_server(plain).list_tools()}


@pytest.mark.parametrize(
    ("runbook_write", "overrides"),
    [(False, {}), (True, {"read_only": True}), (True, {"tool_profile": "essential"})],
)
async def test_the_tool_is_absent_without_paths_on_a_read_only_server_and_in_essential(
    tmp_vault: Path, runbook_write: bool, overrides: dict[str, Any]
) -> None:
    async for opened in _open(tmp_vault, runbook_write=runbook_write, **overrides):
        names = {tool.name for tool in await create_server(opened).list_tools()}
        assert "runbook_capture" not in names


# --- writes --------------------------------------------------------------------


async def test_a_new_symptom_is_appended_without_adding_frontmatter(app: DatacronApp) -> None:
    receipt = await _capture(
        app,
        "error_fix",
        fields={
            "symptom": "Sync fails with 0x80244022",
            "cause": "IIS pool stopped",
            "fix": "Start the pool",
        },
    )

    assert receipt["action"] == "appended", receipt
    assert receipt["written"] is True
    assert receipt["indexed"] is True
    assert receipt["operation_id"]
    text = _read(app, _TROUBLESHOOTING)
    assert text.startswith("# Troubleshooting\n")
    assert text.endswith(
        "## Symptom: Sync fails with 0x80244022\n\n**Cause.** IIS pool stopped\n\n"
        "**Fix.** Start the pool\n\nmeasured 2026-10-20\n"
    )
    assert not _TRACE.search(text)


async def test_the_same_fact_on_another_day_is_skipped_and_nothing_is_written(
    app: DatacronApp,
) -> None:
    before = _read(app, _TROUBLESHOOTING)

    receipt = await _capture(
        app,
        "error_fix",
        "said",
        fields={
            "symptom": "A client stops updating",
            "cause": "Proxy bypass list.",
            "fix": "Add the name.",
        },
    )

    assert (receipt["action"], receipt["written"]) == ("duplicate_skipped", False)
    assert _read(app, _TROUBLESHOOTING) == before


async def test_a_known_symptom_with_a_new_fix_is_updated_not_duplicated(app: DatacronApp) -> None:
    receipt = await _capture(
        app,
        "error_fix",
        fields={"symptom": "a client stops updating", "fix": "Also clear the proxy cache"},
    )

    text = _read(app, _TROUBLESHOOTING)
    assert receipt["action"] == "updated", receipt
    assert text.count("## Symptom:") == 1
    assert "**Fix.** Add the name." in text
    assert (
        "**Update, 2026-10-20.** **Cause.** Not captured **Fix.** Also clear the proxy cache "
        "(measured 2026-10-20)" in text
    )


async def test_a_variable_row_is_replaced_in_place(app: DatacronApp) -> None:
    receipt = await _capture(
        app,
        "server",
        fields={
            "placeholder": "<UPSTREAM>",
            "meaning": "Upstream WSUS server",
            "value": "tpcpdc04d",
            "heading": "Servers",
        },
    )

    text = _read(app, _VARIABLES)
    assert receipt["action"] == "updated", receipt
    assert "| `<UPSTREAM>` | Upstream WSUS server | `tpcpdc04d` (measured 2026-10-20) |\n" in text
    assert "tpcpdc03d" not in text


async def test_a_new_file_is_created_with_its_title_and_no_frontmatter(app: DatacronApp) -> None:
    receipt = await _capture(
        app,
        "contact",
        "said",
        fields={
            "name": "Network team",
            "role": "firewall",
            "channel": "ticket queue",
            "heading": "Contacts",
        },
    )

    assert receipt["action"] == "created", receipt
    assert receipt["rel_path"] == f"{_RUNBOOK}/60-escalation.md"
    assert _read(app, receipt["rel_path"]) == (
        "# Escalation\n\n## Contacts\n\n"
        "- **Network team**, firewall: ticket queue. said 2026-10-20\n"
    )


async def test_a_registered_subject_without_runbook_starts_one(app: DatacronApp) -> None:
    receipt = await _capture(
        app,
        "error_fix",
        "unverified",
        subject="project/ad",
        fields={"symptom": "Replication stalls", "fix": "Check the site link"},
    )

    assert receipt["rel_path"] == "CPDC/ad-runbook/50-troubleshooting.md", receipt
    assert receipt["runbook_created"] is True
    assert _read(app, receipt["rel_path"]).startswith(
        "# Troubleshooting\n\n## Symptom: Replication stalls\n"
    )


async def test_a_role_without_title_cannot_create_its_file(app: DatacronApp) -> None:
    (app.vault_root / _VARIABLES).unlink()

    receipt = await _capture(
        app,
        "server",
        fields={"placeholder": "<X>", "meaning": "m", "value": "v", "heading": "Servers"},
    )

    assert "declares no title" in receipt["error"]["message"]
    assert not (app.vault_root / _VARIABLES).exists()


async def test_a_measured_capture_refreshes_last_verified_of_a_file_that_has_frontmatter(
    app: DatacronApp,
) -> None:
    receipt = await _capture(app, "fact", fields={"heading": "Forest", "fact": "Schema version 88"})

    text = _read(app, _ARCHITECTURE)
    assert receipt["action"] == "appended", receipt
    assert "last_verified: '2026-10-20'" in text or "last_verified: 2026-10-20" in text
    assert "origin" not in text
    assert "confidence" not in text
    assert "> **On Mets, 2026-10-20.** Schema version 88 (measured 2026-10-20)" in text


async def test_an_unverified_capture_leaves_last_verified_alone(app: DatacronApp) -> None:
    await _capture(app, "fact", "unverified", fields={"heading": "Forest", "fact": "Two sites"})

    assert "last_verified" not in _read(app, _ARCHITECTURE)


async def test_dry_run_returns_the_plan_and_writes_nothing(app: DatacronApp) -> None:
    before = _read(app, _TROUBLESHOOTING)

    plan = await _capture(app, "error_fix", fields={"symptom": "New one", "fix": "x"}, dry_run=True)

    assert plan["dry_run"] is True
    assert (plan["action"], plan["written"], plan["role"]) == ("appended", False, "troubleshooting")
    assert _read(app, _TROUBLESHOOTING) == before


async def test_a_secret_is_refused_and_the_file_is_untouched(app: DatacronApp) -> None:
    before = _read(app, _TROUBLESHOOTING)

    receipt = await _capture(
        app, "error_fix", fields={"symptom": "Login fails", "fix": "Use password=Hunter2Hunter2"}
    )

    assert "looks like a secret" in receipt["error"]["message"]
    assert "Hunter2" not in str(receipt)
    assert _read(app, _TROUBLESHOOTING) == before


async def test_a_retry_with_the_same_request_id_replays_the_receipt(app: DatacronApp) -> None:
    fields = {"symptom": "Disk full", "fix": "Run the cleanup"}
    first = await _capture(app, "error_fix", fields=fields, request_id="rb-1")
    second = await _capture(app, "error_fix", fields=fields, request_id="rb-1")

    assert second["replayed"] is True
    assert second["operation_id"] == first["operation_id"]
    assert _read(app, _TROUBLESHOOTING).count("## Symptom: Disk full") == 1


# --- boundaries -----------------------------------------------------------------


async def test_the_generic_writers_still_cannot_write_a_runbook(app: DatacronApp) -> None:
    refused = await _append_journal_impl(
        app, rel_path=_TROUBLESHOOTING, heading="Notes", entry="- x"
    )

    assert "error" in refused
    assert "Notes" not in _read(app, _TROUBLESHOOTING)


async def test_a_runbook_outside_the_runbook_write_paths_is_refused(
    tmp_vault: Path,
) -> None:
    async for opened in _open(tmp_vault):
        object.__setattr__(
            opened,
            "settings",
            opened.settings.model_copy(update={"runbook_write_paths": [tmp_vault / "Other"]}),
        )
        receipt = await _capture(opened, "error_fix", fields={"symptom": "s", "fix": "f"})
        assert "outside DATACRON_RUNBOOK_WRITE_PATHS" in receipt["error"]["message"]
        assert str(tmp_vault) not in receipt["error"]["message"]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"subject": "wsu"}, "names no runbook"),
        ({"target_role": "build"}, "not declared"),
        ({"observed_on": "2999-01-01"}, "must not be in the future"),
        ({"source": "  "}, "source must not be empty"),
    ],
)
async def test_refusals_say_what_to_send_instead(
    app: DatacronApp, kwargs: dict[str, Any], fragment: str
) -> None:
    receipt = await _capture(app, "error_fix", fields={"symptom": "s", "fix": "f"}, **kwargs)

    assert fragment in receipt["error"]["message"], receipt
