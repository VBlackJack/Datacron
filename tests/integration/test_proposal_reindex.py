# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0
"""Proposal confirmation across the actual offline index publication path."""

from datetime import date
from pathlib import Path

import pytest

from datacron.core.config import Settings, VaultConfig
from datacron.core.frontmatter import serialize
from datacron.core.paths import sidecar_index_db
from datacron.indexing import chunker
from datacron.indexing.rebuild import rebuild_index_atomic
from datacron.mcp.server import build_app
from datacron.mcp.tools.advisory import _contradiction_scan_impl


def _legacy_stack(headings: list[tuple[int, str]], level: int, title: str) -> list[tuple[int, str]]:
    """Reproduce the pre-D1 position-based ancestry when seeding the old index."""
    return [*headings[: level - 1], (level, title.strip())]


@pytest.mark.parametrize("changed_chunk_ids", [True, False])
async def test_confirmation_after_offline_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_chunk_ids: bool
) -> None:
    prefix = "## Context\n\nUnrelated introduction.\n\n" if changed_chunk_ids else "# Source\n\n"
    bodies = {
        "target.md": (
            "01HQXR7K9YZ8M2N3PQRSTV4WX5",
            "# Target\n\n## Employer 2026-07-10\n\n"
            "The Windows engineering employer is Magellan for the platform team.\n",
        ),
        "source.md": (
            "01HQXR7K9YZ8M2N3PQRSTV4WX6",
            prefix + "## Employer 2026-07-15\n\n"
            "CORRECTION: The Windows engineering employer is Worldline and replaces "
            "the old Magellan statement for the platform team.\n",
        ),
    }
    for name, (note_id, body) in bodies.items():
        (tmp_path / name).write_text(serialize({"id": note_id}, body), encoding="utf-8")
    before = {name: (tmp_path / name).read_bytes() for name in bodies}
    settings = Settings(
        vault_root=tmp_path,
        read_paths=[tmp_path],
        write_paths=[tmp_path],
        redact_secrets="off",
        repair_min_interval_seconds=0,
    )
    app = build_app(settings=settings, vault_root=tmp_path)
    database = sidecar_index_db(tmp_path)
    await app.store.open(database)
    try:
        with monkeypatch.context() as legacy:
            legacy.setattr(chunker, "_updated_heading_stack", _legacy_stack)
            for note in await app.vault_reader.list_notes():
                await app.store.upsert_note(note, app.chunker.chunk(note))
            scan = await _contradiction_scan_impl(app, today=date(2026, 7, 17))
        token = scan["candidates"][0]["suggested_mutation"]["proposal_token"]
        confirmed = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)
        assert "confirmation" in confirmed
    finally:
        await app.store.close()

    rebuilt = await rebuild_index_atomic(tmp_path, settings, VaultConfig())
    assert rebuilt["reindexed_notes"] == len(bodies)
    await app.store.open(database)
    try:
        result = await _contradiction_scan_impl(app, mode="confirm", proposal_token=token)
        current = await _contradiction_scan_impl(app, today=date(2026, 7, 17))
        new_token = current["candidates"][0]["suggested_mutation"]["proposal_token"]
        if changed_chunk_ids:
            assert new_token != token
            assert result["error"]["code"] == "proposal_token_stale_or_unknown"
            assert "reindex" in result["error"]["message"]
            assert "mode='scan'" in result["error"]["message"]
            assert "confirmation" not in result
        else:
            assert new_token == token
            assert result == confirmed
        refreshed = await _contradiction_scan_impl(app, mode="confirm", proposal_token=new_token)
        assert "confirmation" in refreshed
        assert {name: (tmp_path / name).read_bytes() for name in bodies} == before
        assert not (tmp_path / ".datacron/oplog/operations.jsonl").exists()
    finally:
        await app.store.close()
