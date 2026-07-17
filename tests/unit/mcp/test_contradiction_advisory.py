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
"""Live, read-only contradiction scan v2 tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools.advisory import _contradiction_scan_impl
from datacron.mcp.tools.search import _search_text_impl
from datacron.mcp.tools.write import _patch_note_section_impl

_TODAY = date(2026, 7, 17)
_OLD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_NEW_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_ONECERT_OLD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX7"
_ONECERT_NEW_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX8"


@pytest.fixture
async def contradiction_app(tmp_path: Path) -> AsyncIterator[tuple[DatacronApp, Path]]:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        repair_min_interval_seconds=0,
        contradiction_max_pairs=64,
        contradiction_max_candidates=10,
        redact_secrets="off",
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    try:
        yield (
            build_app(
                settings=settings,
                vault_root=vault,
                chunker=MarkdownChunker(),
                store=store,
            ),
            vault,
        )
    finally:
        await store.close()


def _write_note(vault: Path, rel_path: str, note_id: str, body: str) -> Path:
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = serialize(
        {
            "id": note_id,
            "title": Path(rel_path).stem,
            "created": "2026-07-01T00:00:00+00:00",
            "updated": "2026-07-17T00:00:00+00:00",
            "origin": "human",
            "confidence": "high",
            "last_verified": "2026-07-17",
            "supersedes": [],
            "tags": ["memory"],
        },
        body,
    )
    target.write_text(raw, encoding="utf-8", newline="")
    return target


def _write_candidate_pair(vault: Path, *, source_content: str) -> tuple[Path, Path]:
    old = _write_note(
        vault,
        "_memory/facts/employer-old.md",
        _OLD_ID,
        (
            "# Employer history\n\n"
            "## Employer 2026-07-10\n\n"
            "The Windows engineering employer is Magellan for the platform team.\n"
        ),
    )
    new = _write_note(
        vault,
        "_memory/facts/employer-current.md",
        _NEW_ID,
        f"# Employer update\n\n## Employer 2026-07-15\n\n{source_content}\n",
    )
    return old, new


def _markdown_snapshot(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*.md"))
    }


def _suggested_token(scan: dict[str, Any]) -> str:
    candidates = cast("list[dict[str, Any]]", scan["candidates"])
    suggested = cast("dict[str, Any]", candidates[0]["suggested_mutation"])
    return cast("str", suggested["proposal_token"])


async def test_live_scan_is_deterministic_bounded_and_read_only(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    before = _markdown_snapshot(vault)

    first = await _contradiction_scan_impl(app, today=_TODAY)
    second = await _contradiction_scan_impl(app, today=_TODAY)

    assert first == second
    assert first["schema_version"] == 2
    assert first["mode"] == "scan"
    assert first["candidate_count"] == 1
    assert first["examined_pairs"] <= app.settings.contradiction_max_pairs
    assert first["limits"] == {"max_pairs": 64, "max_candidates": 10}
    candidate = first["candidates"][0]
    assert candidate["class"] == "CONTRADICTION"
    assert candidate["addressable"] is True
    assert candidate["target"]["note_id"] == _OLD_ID
    assert candidate["source"]["note_id"] == _NEW_ID
    assert candidate["target"]["header_path"] == "Employer history / Employer 2026-07-10"
    assert candidate["target"]["line_start"] <= candidate["target"]["line_end"]
    suggested = candidate["suggested_mutation"]
    assert suggested["tool"] == "patch_note_section"
    assert suggested["block"].startswith("> CORRECTION 2026-07-17 :")
    assert _markdown_snapshot(vault) == before
    assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()


@pytest.mark.parametrize(
    ("source_content", "expected_class"),
    [
        (
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team.",
            "CONTRADICTION",
        ),
        (
            "MISE A JOUR: The Windows engineering employer remains Magellan; this "
            "clarifies the platform team reporting line.",
            "RAFFINEMENT",
        ),
        (
            "Question ouverte: is the Windows engineering employer Magellan or "
            "Worldline for the platform team?",
            "QUESTION_OUVERTE",
        ),
    ],
)
async def test_each_class_gets_its_only_allowed_section_mutation(
    contradiction_app: tuple[DatacronApp, Path],
    source_content: str,
    expected_class: str,
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(vault, source_content=source_content)

    scan = await _contradiction_scan_impl(app, today=_TODAY)

    candidate = scan["candidates"][0]
    assert candidate["class"] == expected_class
    assert candidate["suggested_mutation"]["classification"] == expected_class
    assert candidate["suggested_mutation"]["scope"] == "section"
    assert candidate["suggested_mutation"]["tool"] == "patch_note_section"


async def test_previously_corrected_reference_cases_are_suppressed(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_note(
        vault,
        "_memory/facts/org-cpdc-windows.md",
        _OLD_ID,
        (
            "# CPDC Windows\n\n## Entite / conventions 2026-07-10\n\n"
            "Julien works for Worldline in the SYS Windows platform team.\n\n"
            "> CORRECTION 2026-07-15 : The employer is now Magellan after the entity "
            "split; platform names remain Worldline. Voir _memory/preferences/julien.md.\n"
        ),
    )
    _write_note(
        vault,
        "_memory/preferences/julien.md",
        _NEW_ID,
        (
            "# Julien\n\n## Employer 2026-07-15\n\n"
            "Julien works for Magellan after the entity split while Windows platform "
            "names remain Worldline.\n"
        ),
    )
    _write_note(
        vault,
        "_memory/projects/onecert-windows-direction-juillet-2026.md",
        _ONECERT_OLD_ID,
        (
            "# OneCert direction\n\n"
            "## Decision produit 2026-07-08\n\n"
            "Hide Settings and Assignment by default and add Server Deck observability.\n\n"
            "> MISE A JOUR 2026-07-13 : The meeting confirmed two-level tabs, "
            "admin_mode and plugins as consumers. Voir "
            "_memory/facts/onecert-windows-reunion-2026-07-13.md.\n\n"
            "## Arbitrage Server Deck vs WAC 2026-07-08\n\n"
            "Keep Server Deck read-first for certificate fleet observability; whether "
            "OneCert or Charon owns the dashboard remains open.\n\n"
            "> MISE A JOUR 2026-07-13 : Prefer the OneCert view for managed "
            "certificates and Charon for discovery; Eric's answer remains open. Voir "
            "_memory/facts/onecert-windows-reunion-2026-07-13.md.\n"
        ),
    )
    _write_note(
        vault,
        "_memory/facts/onecert-windows-reunion-2026-07-13.md",
        _ONECERT_NEW_ID,
        (
            "# OneCert meeting\n\n"
            "## Product decisions 2026-07-13\n\n"
            "OneCert uses two-level tabs, admin_mode and plugins as consumers.\n\n"
            "## Dashboard direction 2026-07-13\n\n"
            "The OneCert view handles managed certificates while Charon discovers "
            "unmanaged certificates; Eric's dashboard answer remains open.\n"
        ),
    )

    scan = await _contradiction_scan_impl(app, today=_TODAY)

    assert scan["candidate_count"] == 0


async def test_duplicate_same_level_heading_is_not_addressable(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_note(
        vault,
        "_memory/facts/duplicate.md",
        _OLD_ID,
        (
            "# Employer history\n\n## Employer 2026-07-10\n\n"
            "The Windows engineering employer is Magellan for the platform team.\n\n"
            "## Employer 2026-07-10\n\n"
            "The Windows engineering employer is Magellan for the platform team.\n"
        ),
    )
    _write_note(
        vault,
        "_memory/facts/current.md",
        _NEW_ID,
        (
            "# Employer update\n\n## Employer 2026-07-15\n\n"
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "Magellan for the platform team.\n"
        ),
    )

    scan = await _contradiction_scan_impl(app, today=_TODAY)

    assert scan["candidate_count"] == 1
    candidate = scan["candidates"][0]
    assert candidate["addressable"] is False
    assert candidate["suggested_mutation"] is None
    assert candidate["alternative_mutations"] == []
    assert "ambiguous" in candidate["manual_action"]


async def test_confirmation_is_idempotent_and_write_cas_is_single_effect(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    scan = await _contradiction_scan_impl(app, today=_TODAY)
    token = _suggested_token(scan)

    first = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)
    second = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)

    assert first == second
    call = first["confirmation"]["write_call"]
    assert call["tool"] == "patch_note_section"
    arguments = cast("dict[str, Any]", call["arguments"])
    written = await _patch_note_section_impl(app, **arguments)
    replayed_write = await _patch_note_section_impl(app, **arguments)
    stale_confirmation = await _contradiction_scan_impl(
        app,
        mode="confirm",
        proposal_token=token,
    )

    assert written["indexed"] is True
    assert replayed_write["error"]["type"] == "WriteConflictError"
    assert stale_confirmation["error"]["type"] == "ValueError"
    retrieval = await _search_text_impl(app, query="worldline", limit=10)
    assert any(
        result["note_rel_path"] == "_memory/facts/employer-old.md"
        for result in retrieval["results"]
    )
    oplog = (vault / ".datacron" / "oplog" / "operations.jsonl").read_text(encoding="ascii")
    assert '"tool":"patch_note_section"' in oplog
    assert (vault / ".datacron" / "history").is_dir()


async def test_target_change_before_confirmation_refuses_without_write(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    target, _source = _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    scan = await _contradiction_scan_impl(app, today=_TODAY)
    token = _suggested_token(scan)
    target.write_text(
        target.read_text(encoding="utf-8").replace("Magellan", "Contoso"),
        encoding="utf-8",
        newline="",
    )

    result = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)

    assert result["error"]["type"] == "ValueError"
    assert "stale" in result["error"]["message"]
    assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()
    assert not (vault / ".datacron" / "history").exists()


class _FakeContext:
    def __init__(self, action: str, *, form_capability: bool = True) -> None:
        form = object() if form_capability else None
        self.request_context = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities=SimpleNamespace(
                        elicitation=SimpleNamespace(form=form),
                    )
                )
            )
        )
        self.action = action
        self.calls = 0

    async def elicit(self, *, message: str, schema: type[Any]) -> Any:
        del message, schema
        self.calls += 1
        if self.action == "accept":
            data = SimpleNamespace(classification="CONTRADICTION", scope="section")
        else:
            data = None
        return SimpleNamespace(action=self.action, data=data)


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_decline_or_cancel_leaves_no_state_and_scan_replays(
    contradiction_app: tuple[DatacronApp, Path],
    action: str,
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    ctx = _FakeContext(action)
    before = _markdown_snapshot(vault)

    elicited = await _contradiction_scan_impl(
        app,
        ctx=cast("Any", ctx),
        today=_TODAY,
    )
    replay = await _contradiction_scan_impl(app, today=_TODAY)

    assert ctx.calls == 1
    assert elicited["elicitation_action"] == action
    assert elicited["candidates"] == replay["candidates"]
    assert _markdown_snapshot(vault) == before
    assert not (vault / ".datacron" / "oplog" / "operations.jsonl").exists()


async def test_elicitation_accept_and_fallback_confirmation_match(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    baseline = await _contradiction_scan_impl(app, today=_TODAY)
    token = _suggested_token(baseline)
    fallback = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)
    ctx = _FakeContext("accept")

    elicited = await _contradiction_scan_impl(
        app,
        ctx=cast("Any", ctx),
        today=_TODAY,
    )

    assert ctx.calls == 1
    assert elicited == fallback


async def test_client_without_form_capability_is_not_elicited(
    contradiction_app: tuple[DatacronApp, Path],
) -> None:
    app, vault = contradiction_app
    _write_candidate_pair(
        vault,
        source_content=(
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team."
        ),
    )
    ctx = _FakeContext("accept", form_capability=False)

    result = await _contradiction_scan_impl(
        app,
        ctx=cast("Any", ctx),
        today=_TODAY,
    )

    assert result["mode"] == "scan"
    assert ctx.calls == 0
