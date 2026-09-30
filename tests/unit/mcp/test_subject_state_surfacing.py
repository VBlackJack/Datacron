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
"""A subject's state note is surfaced where sessions read and write the subject."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN, Settings
from datacron.core.memory_protocol import CONTRACT_HASH, SESSION_MIN_TOKENS
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import _create_note_ai_impl
from datacron.mcp.tools.session import rendered_size, session_context
from datacron.mcp.tools.subject_state import FOLD_GUIDANCE

_STATE_NOTE = "_memory/subjects/heimdall/heimdall.md"
_SUBJECT_FOLDER = "_memory/subjects/heimdall"
_VERIFIED = "2026-09-01"
_POLICY = {
    "scope": "_memory",
    "rules": [
        {"tag": "project/heimdall", "folder": _SUBJECT_FOLDER},
        {"tag": "memory/fact", "folder": "_memory/facts"},
        {"tag": "memory/project", "folder": "_memory/projects"},
    ],
    "tags": {
        "placement_namespace": "memory",
        "subject_namespace": "project",
        "subjects": [{"tag": "project/heimdall", "aliases": ["heimdall.next"]}],
        "allowed_namespaces": ["kind"],
    },
}


async def _open_app(vault: Path, organization: dict[str, Any] | None) -> AsyncIterator[DatacronApp]:
    if organization is not None:
        config_path = vault / ".datacron" / "VAULT.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {}
        if config_path.exists():
            document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        document["organization"] = organization
        config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    settings = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        max_result_count=20,
        max_result_tokens=8000,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    try:
        yield build_app(settings=settings, vault_root=vault, chunker=MarkdownChunker(), store=store)
    finally:
        await store.close()


@pytest.fixture
async def app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    async for opened in _open_app(tmp_vault, _POLICY):
        yield opened


@pytest.fixture
async def plain_app(tmp_vault: Path) -> AsyncIterator[DatacronApp]:
    async for opened in _open_app(tmp_vault, None):
        yield opened


async def _create(
    app: DatacronApp,
    rel_path: str,
    tags: list[str],
    body: str = "# Note\n",
    last_verified: str | None = None,
) -> dict[str, Any]:
    return await _create_note_ai_impl(
        app,
        rel_path=rel_path,
        title="Heimdall state" if rel_path == _STATE_NOTE else "Note",
        body=body,
        origin="ai",
        confidence="high",
        tags=tags,
        last_verified=last_verified,
    )


async def _create_state_note(app: DatacronApp) -> None:
    result = await _create(
        app,
        _STATE_NOTE,
        ["memory/project", "project/heimdall", "kind/development"],
        body="# Heimdall\n\n## Etat date\n\nCurrent.\n",
        last_verified=_VERIFIED,
    )
    assert "error" not in result
    assert "organization" not in result or "next_step" not in result["organization"]


async def test_a_fact_filed_away_from_its_subject_names_the_folder_and_the_state_note(
    app: DatacronApp,
) -> None:
    await _create_state_note(app)

    result = await _create(
        app, "_memory/facts/2026-09-25-rust.md", ["memory/fact", "project/heimdall"]
    )

    assert result["indexed"] is True
    guidance = result["organization"]
    assert guidance["expected_folder"] == _SUBJECT_FOLDER
    assert guidance["links_state_note"] is False
    assert guidance["next_step"] == FOLD_GUIDANCE
    subject = guidance["subject"]
    assert subject["subject_tag"] == "project/heimdall"
    assert subject["state_notes"] == [
        {"rel_path": _STATE_NOTE, "last_verified": _VERIFIED, "newer_notes": 1}
    ]
    assert subject["state_notes_omitted"] == 0


async def test_a_linked_fact_in_its_folder_carries_no_folder_advice(app: DatacronApp) -> None:
    await _create_state_note(app)

    result = await _create(
        app,
        f"{_SUBJECT_FOLDER}/2026-09-26-milestone.md",
        ["memory/fact", "project/heimdall"],
        body="# Note\n\n## Rattachements\n\n- [[Heimdall state]]\n",
    )

    guidance = result["organization"]
    assert "expected_folder" not in guidance
    assert guidance["links_state_note"] is True


async def test_a_vault_without_organization_rules_adds_no_guidance(plain_app: DatacronApp) -> None:
    result = await _create(plain_app, "_memory/facts/2026-09-25-x.md", ["memory/fact"])

    assert result["indexed"] is True
    assert "organization" not in result


async def test_a_guidance_failure_never_fails_the_committed_write(
    app: DatacronApp, monkeypatch: pytest.MonkeyPatch, tmp_vault: Path
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("guidance exploded")

    monkeypatch.setattr("datacron.mcp.tools.write.placement_guidance", broken)

    result = await _create(
        app, "_memory/facts/2026-09-25-y.md", ["memory/fact", "project/heimdall"]
    )

    assert "error" not in result
    assert result["indexed"] is True
    assert "organization" not in result
    assert (tmp_vault / "_memory" / "facts" / "2026-09-25-y.md").is_file()


@pytest.mark.parametrize("subject", ["heimdall", "project/heimdall", "Heimdall.Next"])
async def test_session_context_loads_the_state_note_first(app: DatacronApp, subject: str) -> None:
    await _create_state_note(app)
    await _create(app, "_memory/facts/2026-09-25-rust.md", ["memory/fact", "project/heimdall"])

    result = await session_context(app, subject=subject)

    assert result["sources"][0]["rel_path"] == _STATE_NOTE
    assert result["subject_state"]["subject_tag"] == "project/heimdall"
    assert result["subject_state"]["subject_notes"] == 2
    assert result["subject_state"]["state_notes"][0]["newer_notes"] == 1


async def test_the_state_note_survives_a_domain_filter_it_does_not_match(app: DatacronApp) -> None:
    await _create_state_note(app)

    result = await session_context(app, subject="heimdall", domain="meeting")

    assert result["sources"][0]["rel_path"] == _STATE_NOTE


async def test_a_note_outside_the_organization_scope_gets_no_folder_advice(
    app: DatacronApp,
) -> None:
    await _create_state_note(app)

    result = await _create(app, "notes/2026-09-25-outside.md", ["memory/fact", "project/heimdall"])

    assert "expected_folder" not in result["organization"]


async def test_a_backslash_path_in_the_right_folder_is_not_misplaced(app: DatacronApp) -> None:
    await _create_state_note(app)

    result = await _create(
        app,
        "_memory\\subjects\\heimdall\\2026-09-26-backslash.md",
        ["memory/fact", "project/heimdall"],
    )

    assert "error" not in result
    assert "expected_folder" not in result["organization"]


async def test_subject_state_never_turns_a_fitting_budget_into_a_refusal(
    app: DatacronApp,
) -> None:
    await _create_state_note(app)
    baseline = await session_context(app, known_contract_hash=CONTRACT_HASH)
    kernel_tokens = -(-rendered_size({**baseline, "sources": []}) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)

    result = await session_context(
        app,
        subject="heimdall",
        known_contract_hash=CONTRACT_HASH,
        max_tokens=max(kernel_tokens + 8, SESSION_MIN_TOKENS),
    )

    assert "error" not in result
    # The full listing cannot fit in eight spare tokens: the field was trimmed or dropped.
    assert "subject_state" not in result or result["subject_state"]["state_notes_omitted"] == 1


async def test_a_subject_state_failure_falls_back_to_the_plain_search(
    app: DatacronApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_state_note(app)

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr("datacron.mcp.tools.session.load_subject_notes", broken)

    result = await session_context(app, subject="heimdall")

    assert "error" not in result
    assert "subject_state" not in result


async def test_an_unregistered_subject_keeps_the_plain_search(app: DatacronApp) -> None:
    await _create_state_note(app)

    result = await session_context(app, subject="release checklist")

    assert "subject_state" not in result
