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
import sqlite3
import sys
from pathlib import Path

import pytest

from datacron.core.config import Settings, VaultConfig
from datacron.core.durability import DurabilityStatus
from datacron.core.frontmatter import serialize
from datacron.core.paths import sidecar_index_db
from datacron.indexing import rebuild as rebuild_module
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.rebuild import (
    REBUILD_FAULT_POINTS,
    IndexRebuildError,
    rebuild_index_atomic,
)
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


def test_publication_failure_names_the_cause_and_the_remedy(tmp_path: Path) -> None:
    """A held index must not surface as a bare WinError 5 from `os.replace`.

    The operator has no way to guess from `PermissionError: [WinError 5]` that the
    MCP server is what holds the file. Reading the source was the only route.
    """
    db_path = tmp_path / "datacron.db"
    db_path.write_bytes(b"live index")
    temp_path = tmp_path / "datacron.db.rebuild"
    temp_path.write_bytes(b"rebuilt index")

    def _denied(_source: object, _destination: object) -> None:
        raise PermissionError(13, "Access is denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rebuild_module.os, "replace", _denied)
        with pytest.raises(IndexRebuildError) as caught:
            rebuild_module._publish_index(temp_path, db_path)

    message = str(caught.value)
    assert "held open by another process" in message
    assert "datacron mcp serve" in message
    assert "Stop every MCP client and server" in message
    assert "the existing index is untouched" in message
    assert db_path.read_bytes() == b"live index"


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace open files")
async def test_rebuild_refuses_up_front_when_the_index_is_held_open(tmp_path: Path) -> None:
    """The condition is knowable before the rebuild, not after minutes of work.

    BL-0103 measured the old behaviour: 2290 notes indexed, then a raw traceback at
    the swap. The whole rebuild was thrown away for a condition one handle can test.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_bytes(
        _serialized(_NOTE_A, "Held", "# Held\n\nBody.\n").encode("utf-8")
    )
    await rebuild_index_atomic(vault, _settings(vault), VaultConfig())
    db_path = sidecar_index_db(vault)
    before = db_path.read_bytes()

    holder = sqlite3.connect(db_path)
    try:
        with pytest.raises(IndexRebuildError) as caught:
            await rebuild_index_atomic(vault, _settings(vault), VaultConfig())
    finally:
        holder.close()

    assert "held open by another process" in str(caught.value)
    assert db_path.read_bytes() == before
    assert not list(db_path.parent.glob("*.rebuild*"))
