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
"""Invariant properties for byte-exact, fence-aware, atomic full reindex."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from datacron.core.config import Settings, VaultConfig
from datacron.core.durability import DurabilityStatus
from datacron.core.frontmatter import serialize
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.rebuild import REBUILD_FAULT_POINTS, rebuild_index_atomic
from datacron.mcp.health import build_health
from datacron.mcp.server import build_app

pytestmark = pytest.mark.invariants

_NOTE_A = "01J00000000000000000000071"
_NOTE_B = "01J00000000000000000000072"
_SUPPORTED = DurabilityStatus(backend="property-supported", directory_flush_supported=True)


def _settings(vault: Path, *, read_only: bool = False) -> Settings:
    return Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        read_only=read_only,
        max_result_count=100,
        max_result_tokens=100_000,
    )


def _serialized(note_id: str, title: str, body: str) -> str:
    return serialize(
        {
            "id": note_id,
            "title": title,
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "tags": ["lot7"],
        },
        body,
    )


async def _health(vault: Path) -> tuple[dict[str, object], SQLiteFTS5Store]:
    db_path = vault / ".datacron" / "index" / "datacron.db"
    store = SQLiteFTS5Store()
    await store.open(db_path, read_only=True)
    app = build_app(
        settings=_settings(vault, read_only=True),
        vault_root=vault,
        store=store,
        durability_status=_SUPPORTED,
    )
    return await build_health(app), store


async def test_prop_fresh_index_zero_hash_divergence(tmp_path: Path) -> None:
    """Fresh full rebuild stores exact BOM/EOL hashes and only real wikilinks."""
    vault = tmp_path / "vault"
    vault.mkdir()
    lf_path = vault / "lf.md"
    crlf_bom_path = vault / "crlf-bom.md"
    sidecar = vault / ".datacron"
    sidecar.mkdir()
    migrated_ids = sidecar / "ulids.json.migrated"
    migrated_raw = json.dumps({"retired.md": _NOTE_A}).encode("ascii")
    migrated_ids.write_bytes(migrated_raw)
    lf_raw = _serialized(
        _NOTE_A,
        "Fence aware",
        (
            "# Fence aware\n\n"
            "```text\n[[fenced-false-positive]]\n```\n\n"
            "if [[ value != other ]]; then\n  echo safe\nfi\n\n"
            "[[real-missing-target]]\n"
        ),
    ).encode("utf-8")
    crlf_bom_raw = b"\xef\xbb\xbf" + _serialized(
        _NOTE_B,
        "Byte exact",
        "# Byte exact\n\nCRLF and BOM remain significant.\n",
    ).replace("\n", "\r\n").encode("utf-8")
    lf_path.write_bytes(lf_raw)
    crlf_bom_path.write_bytes(crlf_bom_raw)
    before = {path.name: path.read_bytes() for path in (lf_path, crlf_bom_path)}

    rebuilt = await rebuild_index_atomic(vault, _settings(vault), VaultConfig())
    health, store = await _health(vault)
    try:
        indexed = await store.list_indexed_notes()
        wikilink_chunks = await store.list_chunks_with_wikilinks()
    finally:
        await store.close()

    assert rebuilt["generation"] == 1
    assert health["index"]["hash_divergences"] == 0  # type: ignore[index]
    assert health["index"]["consistent_with_vault"] is True  # type: ignore[index]
    assert indexed["lf.md"][1] == hashlib.sha256(lf_raw).hexdigest()
    assert indexed["crlf-bom.md"][1] == hashlib.sha256(crlf_bom_raw).hexdigest()
    assert (
        indexed["crlf-bom.md"][1]
        != hashlib.sha256(
            crlf_bom_raw.decode("utf-8").lstrip("\ufeff").replace("\r\n", "\n").encode("utf-8")
        ).hexdigest()
    )
    indexed_targets = {target for chunk in wikilink_chunks for target in chunk.wikilinks_out}
    assert indexed_targets == {"real-missing-target"}
    assert health["integrity"]["broken_wikilinks"] == 1  # type: ignore[index]
    assert {path.name: path.read_bytes() for path in (lf_path, crlf_bom_path)} == before
    assert migrated_ids.read_bytes() == migrated_raw
    assert not (sidecar / "ulids.json").exists()


@pytest.mark.parametrize("fault_point", REBUILD_FAULT_POINTS)
async def test_atomic_reindex_crash_boundaries(tmp_path: Path, fault_point: str) -> None:
    """Every injected publication crash leaves the complete old or new generation."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note_path = vault / "note.md"
    old_raw = _serialized(_NOTE_A, "Atomic", "# Atomic\n\nold-generation\n").encode()
    new_raw = _serialized(_NOTE_A, "Atomic", "# Atomic\n\nnew-generation\n").encode()
    note_path.write_bytes(old_raw)
    initial = await rebuild_index_atomic(vault, _settings(vault), VaultConfig())
    assert initial["generation"] == 1
    note_path.write_bytes(new_raw)

    def crash_at(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"simulated crash at {point}")

    with pytest.raises(RuntimeError, match=fault_point):
        await rebuild_index_atomic(
            vault,
            _settings(vault),
            VaultConfig(),
            fault_injector=crash_at,
        )

    health, store = await _health(vault)
    try:
        indexed = await store.list_indexed_notes()
        stats = await store.stats()
    finally:
        await store.close()

    published_new = fault_point == "after_swap"
    expected_hash = hashlib.sha256(new_raw if published_new else old_raw).hexdigest()
    assert indexed["note.md"][1] == expected_hash
    assert stats.generation == (2 if published_new else 1)
    assert health["index"]["hash_divergences"] == (0 if published_new else 1)  # type: ignore[index]
    assert note_path.read_bytes() == new_raw
    assert not list((vault / ".datacron" / "index").glob("*.rebuild*"))
