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
"""Metamorphic, adversarial, and global Datacron reliability properties."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from datacron.core.config import Settings
from datacron.core.frontmatter import parse, serialize
from datacron.core.hashing import hash_text
from datacron.core.logger import configure_logging, shutdown_logging
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.mcp.sandbox import wrap_vault_content
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _append_journal_impl,
    _create_note_ai_impl,
    _get_note_impl,
    _patch_note_section_impl,
    _set_frontmatter_impl,
)
from datacron.reliability import compare_with_baseline, scan_vault_read_only

pytestmark = pytest.mark.invariants

_INLINE_TEXT = st.text(
    alphabet=st.characters(
        codec="utf-8",
        blacklist_characters=("\x00", "\r", "\n", "#"),
    ),
    min_size=1,
    max_size=48,
).filter(lambda value: bool(value.strip()))
_SUPPRESS_FIXTURE_CHECK = [HealthCheck.function_scoped_fixture]
_NOTE_ID_A = "01J00000000000000000000021"
_NOTE_ID_B = "01J00000000000000000000022"
_NOTE_ID_C = "01J00000000000000000000023"
_NOTE_ID_D = "01J00000000000000000000024"


def _fresh_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir()
    return vault


def _metadata(note_id: str, title: str) -> dict[str, Any]:
    return {
        "id": note_id,
        "title": title,
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "origin": "human",
        "confidence": "high",
        "last_verified": "2026-01-01",
        "supersedes": [],
        "tags": ["reliability"],
        "custom": {"preserve": True},
    }


def _write_note(
    vault: Path,
    rel_path: str,
    note_id: str,
    title: str,
    body: str,
    *,
    metadata_overrides: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    metadata = _metadata(note_id, title)
    if metadata_overrides:
        metadata.update(metadata_overrides)
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = serialize(metadata, body)
    target.write_bytes(raw.encode("utf-8"))
    return target, raw


async def _open_writable_app(vault: Path) -> tuple[DatacronApp, SQLiteFTS5Store]:
    settings_value = Settings(
        read_paths=[vault],
        write_paths=[vault],
        vault_root=vault,
        max_result_count=100,
        max_result_tokens=100_000,
    )
    store = SQLiteFTS5Store()
    await store.open(vault / ".datacron" / "index" / "datacron.db")
    return build_app(settings=settings_value, vault_root=vault, store=store), store


def _frontmatter_without_updated(raw: str) -> str:
    first_end = raw.find("\n") + 1
    closing = raw.find("\n---\n", first_end)
    assert closing >= 0
    header = raw[first_end:closing]
    return "\n".join(line for line in header.splitlines() if not line.startswith("updated:"))


@settings(max_examples=12, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(
    preamble=_INLINE_TEXT,
    old_target=_INLINE_TEXT,
    replacement=_INLINE_TEXT,
    sibling=_INLINE_TEXT,
)
async def test_prop_02_patch_preserves_rest(
    tmp_path: Path,
    preamble: str,
    old_target: str,
    replacement: str,
    sibling: str,
) -> None:
    """PROP-02: patch changes only its section plus declared `updated` metadata."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/patch-property.md"
    original_body = (
        f"# Patch property\n\n{preamble}\n\n## Target\n\n{old_target}\n\n## Sibling\n\n{sibling}\n"
    )
    target, original_raw = _write_note(
        vault,
        rel_path,
        _NOTE_ID_A,
        "Patch property",
        original_body,
    )
    app, store = await _open_writable_app(vault)
    try:
        result = await _patch_note_section_impl(
            app,
            rel_path=rel_path,
            heading="Target",
            new_content=replacement,
            expected_hash=hash_text(original_raw),
        )
    finally:
        await store.close()

    assert "error" not in result
    final_raw = target.read_text(encoding="utf-8")
    original_metadata, original_body_parsed = parse(original_raw)
    final_metadata, final_body = parse(final_raw)
    original_updated = original_metadata.pop("updated")
    final_updated = final_metadata.pop("updated")

    assert original_metadata == final_metadata
    assert original_updated != final_updated
    assert _frontmatter_without_updated(original_raw) == _frontmatter_without_updated(final_raw)
    assert final_body.split("## Target", 1)[0] == original_body_parsed.split("## Target", 1)[0]
    assert final_body.split("## Sibling", 1)[1] == original_body_parsed.split("## Sibling", 1)[1]
    assert f"## Target\n\n{replacement}" in final_body


@settings(max_examples=8, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(
    update=st.sampled_from(
        (
            ("confidence", "low"),
            ("origin", "ai"),
            ("last_verified", "2026-02-03"),
            ("supersedes", [_NOTE_ID_B]),
        )
    )
)
async def test_prop_03_set_frontmatter_isolation(
    tmp_path: Path,
    update: tuple[str, Any],
) -> None:
    """PROP-03: only the requested key and declared `updated` field may change."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/frontmatter-property.md"
    body = "# Frontmatter property\n\nBody bytes stay exactly stable.\n"
    target, original_raw = _write_note(
        vault,
        rel_path,
        _NOTE_ID_A,
        "Frontmatter property",
        body,
    )
    field, value = update
    app, store = await _open_writable_app(vault)
    try:
        if field == "confidence":
            result = await _set_frontmatter_impl(app, rel_path=rel_path, confidence=value)
        elif field == "origin":
            result = await _set_frontmatter_impl(app, rel_path=rel_path, origin=value)
        elif field == "last_verified":
            result = await _set_frontmatter_impl(app, rel_path=rel_path, last_verified=value)
        else:
            result = await _set_frontmatter_impl(app, rel_path=rel_path, supersedes=value)
    finally:
        await store.close()

    assert "error" not in result
    original_metadata, original_body = parse(original_raw)
    final_metadata, final_body = parse(target.read_text(encoding="utf-8"))
    assert final_body == original_body
    assert final_metadata[field] == value
    for key, original_value in original_metadata.items():
        if key not in {field, "updated"}:
            assert final_metadata[key] == original_value
    assert final_metadata["updated"] != original_metadata["updated"]


@settings(max_examples=8, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(prefix=_INLINE_TEXT)
async def test_prop_08_chunk_resolves_or_fails(tmp_path: Path, prefix: str) -> None:
    """PROP-08: chunks return indexed text exactly or report staleness."""
    vault = _fresh_vault(tmp_path)
    rel_path = "facts/chunk-property.md"
    target, _raw = _write_note(
        vault,
        rel_path,
        _NOTE_ID_A,
        "Chunk property",
        "# Chunk property\n\n## Current\n\nexact-current-section-token\n",
    )
    app, store = await _open_writable_app(vault)
    try:
        await reconcile(app.store, app.vault_reader, app.chunker, mtime_gate=True)
        chunks = await store.list_chunks_for_note(_NOTE_ID_A)
        chunk = next(item for item in chunks if "exact-current-section-token" in item.content)
        current = await _get_note_impl(app, id_or_path=chunk.chunk_id, fmt="full")
        assert current["content"] == wrap_vault_content(rel_path, chunk.content)

        metadata, body = parse(target.read_text(encoding="utf-8"))
        target.write_text(serialize(metadata, f"{prefix}\n{body}"), encoding="utf-8", newline="")
        stale = await _get_note_impl(app, id_or_path=chunk.chunk_id, fmt="full")
    finally:
        await store.close()

    assert stale["error"]["type"] == "StaleChunkError"
    assert "reindex and retry" in stale["error"]["message"]


async def test_prop_10_supersedes_acyclic_triggers_on_set_frontmatter_cycle(
    tmp_path: Path,
) -> None:
    """PROP-10: the global DAG detector triggers on a cycle made by the tool."""
    vault = _fresh_vault(tmp_path)
    rel_a = "_memory/facts/cycle-a.md"
    rel_b = "_memory/facts/cycle-b.md"
    _write_note(vault, rel_a, _NOTE_ID_A, "Cycle A", "# Cycle A\n")
    _write_note(vault, rel_b, _NOTE_ID_B, "Cycle B", "# Cycle B\n")
    assert not scan_vault_read_only(vault).supersedes_cycles

    app, store = await _open_writable_app(vault)
    try:
        first = await _set_frontmatter_impl(app, rel_path=rel_a, supersedes=[_NOTE_ID_B])
        second = await _set_frontmatter_impl(app, rel_path=rel_b, supersedes=[_NOTE_ID_A])
    finally:
        await store.close()

    assert "error" not in first
    assert "error" not in second
    cycles = scan_vault_read_only(vault).supersedes_cycles
    assert len(cycles) == 1
    assert cycles[0].classification == "cycle"


async def _call_all_path_tools(app: DatacronApp, rel_path: str) -> dict[str, dict[str, Any]]:
    return {
        "get_note": await _get_note_impl(app, id_or_path=rel_path, fmt="full"),
        "create_note_ai": await _create_note_ai_impl(
            app,
            rel_path=rel_path,
            title="Must stay confined",
            body="# Must stay confined\n",
            origin="ai",
            confidence="high",
            tags=["containment"],
        ),
        "patch_note_section": await _patch_note_section_impl(
            app,
            rel_path=rel_path,
            heading="Journal",
            new_content="Must not escape.",
        ),
        "set_frontmatter": await _set_frontmatter_impl(
            app,
            rel_path=rel_path,
            confidence="low",
        ),
        "append_journal": await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading="Journal",
            entry="- must not escape",
        ),
    }


@pytest.mark.parametrize("path_kind", ["traversal", "absolute"])
async def test_prop_11_path_containment_read_and_write(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """PROP-11: traversal and absolute paths are rejected for reads and writes."""
    vault = _fresh_vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside_raw = serialize(_metadata(_NOTE_ID_A, "Outside"), "# Outside\n\n## Journal\n")
    outside.write_text(outside_raw, encoding="utf-8", newline="")
    candidate = "_memory/../../outside.md" if path_kind == "traversal" else str(outside)
    app, store = await _open_writable_app(vault)
    try:
        results = await _call_all_path_tools(app, candidate)
    finally:
        await store.close()

    assert outside.read_text(encoding="utf-8") == outside_raw
    assert all(result["error"]["type"] == "PathConfinementError" for result in results.values())


async def test_prop_11_path_containment_rejects_outside_symlink(tmp_path: Path) -> None:
    """PROP-11: a vault-local reparse link cannot redirect tools outside."""
    vault = _fresh_vault(tmp_path)
    outside_dir = tmp_path / "outside-link-target"
    outside_dir.mkdir()
    outside = outside_dir / "note.md"
    outside_raw = serialize(_metadata(_NOTE_ID_A, "Outside"), "# Outside\n\n## Journal\n")
    outside.write_text(outside_raw, encoding="utf-8", newline="")
    link = vault / "_memory" / "outside-link"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"real symlink creation is unavailable on this runner: {exc}")
        command_shell = os.environ.get("COMSPEC")
        if not command_shell:
            pytest.fail("COMSPEC is required to create a temporary NTFS junction")
        process = await asyncio.create_subprocess_exec(
            command_shell,
            "/c",
            "mklink",
            "/J",
            str(link),
            str(outside_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode(errors="replace")

    app, store = await _open_writable_app(vault)
    try:
        results = await _call_all_path_tools(app, "_memory/outside-link/note.md")
    finally:
        await store.close()

    assert outside.read_text(encoding="utf-8") == outside_raw
    assert all(result["error"]["type"] == "PathConfinementError" for result in results.values())


def test_prop_07_id_uniqueness_baseline_blocks_only_new_debt(tmp_path: Path) -> None:
    """PROP-07: one allowlisted ID mismatch passes and a new mismatch fails."""
    vault = _fresh_vault(tmp_path)
    _write_note(vault, "known.md", _NOTE_ID_A, "Known", "# Known\n")
    sidecar = vault / ".datacron" / "ulids.json"
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps({"known.md": _NOTE_ID_B}), encoding="ascii")
    known_scan = scan_vault_read_only(vault)
    assert len(known_scan.id_violations) == 1
    policy = {"id_coherence": [known_scan.id_violations[0].fingerprint]}
    assert compare_with_baseline(known_scan, policy).passed

    _write_note(vault, "new.md", _NOTE_ID_C, "New", "# New\n")
    sidecar.write_text(
        json.dumps({"known.md": _NOTE_ID_B, "new.md": _NOTE_ID_D}),
        encoding="ascii",
    )
    changed_scan = scan_vault_read_only(vault)
    comparison = compare_with_baseline(changed_scan, policy)
    assert not comparison.passed
    assert [item.rel_path for item in comparison.new_violations] == ["new.md"]


def test_prop_09_link_integrity_baseline_blocks_only_new_debt(tmp_path: Path) -> None:
    """PROP-09: parser-aware known broken links pass; a new one fails."""
    vault = _fresh_vault(tmp_path)
    _write_note(vault, "target.md", _NOTE_ID_A, "Cafe Runbook", "# Cafe Runbook\n")
    source, _raw = _write_note(
        vault,
        "source.md",
        _NOTE_ID_B,
        "Source",
        (
            "# Source\n\n[[Missing target]]\n\n[[Cafe-Runbook]]\n\n"
            "```text\n[[Excluded code target]]\n```\n"
        ),
    )
    known_scan = scan_vault_read_only(vault)
    assert len(known_scan.broken_wikilinks) == 2
    assert {item.classification for item in known_scan.broken_wikilinks} == {
        "nonexistent",
        "existing_under_other_title_or_alias",
    }
    policy = {"broken_wikilink": [item.fingerprint for item in known_scan.broken_wikilinks]}
    assert compare_with_baseline(known_scan, policy).passed

    source.write_text(
        source.read_text(encoding="utf-8") + "\n[[Brand new broken target]]\n",
        encoding="utf-8",
        newline="",
    )
    changed_scan = scan_vault_read_only(vault)
    comparison = compare_with_baseline(changed_scan, policy)
    assert not comparison.passed
    assert len(comparison.new_violations) == 1
    assert comparison.new_violations[0].target == "Brand new broken target"


def test_checked_in_reliability_policy_tracks_expected_legacy_counts() -> None:
    """The publishable policy tracks exactly the declared 4 ID and 35 link debts."""
    policy_path = Path(__file__).parents[1] / "fixtures" / "reliability_baseline.json"
    policy = json.loads(policy_path.read_text(encoding="ascii"))
    assert policy["accepted_counts"] == {
        "broken_wikilink": 35,
        "id_coherence": 4,
    }
    assert all(
        len(fingerprint) == 64
        for values in policy["accepted_fingerprints"].values()
        for fingerprint in values
    )


def test_mounted_vault_has_no_new_global_invariant_violations() -> None:
    """Enforce the private-vault policy when CI or pre-push mounts that vault."""
    configured = os.environ.get("DATACRON_RELIABILITY_VAULT")
    if not configured:
        pytest.skip("DATACRON_RELIABILITY_VAULT is not mounted")
    policy_path = Path(__file__).parents[1] / "fixtures" / "reliability_baseline.json"
    policy_payload = json.loads(policy_path.read_text(encoding="ascii"))
    scan = scan_vault_read_only(Path(configured))
    comparison = compare_with_baseline(scan, policy_payload["accepted_fingerprints"])
    assert not scan.parse_errors
    assert comparison.passed, [item.to_dict() for item in comparison.new_violations]


async def test_invariant_06_mutation_is_audited_and_reversible(tmp_path: Path) -> None:
    """I6: a mutation emits an audit record and snapshots the exact prior bytes."""
    vault = _fresh_vault(tmp_path)
    rel_path = "_memory/facts/audit-property.md"
    _target, original_raw = _write_note(
        vault,
        rel_path,
        _NOTE_ID_A,
        "Audit property",
        "# Audit property\n\n## Journal\n\nBefore.\n",
    )
    shutdown_logging()
    log_dir = tmp_path / "logs"
    configure_logging(Settings(log_dir=log_dir))
    app, store = await _open_writable_app(vault)
    try:
        result = await _append_journal_impl(
            app,
            rel_path=rel_path,
            heading="Journal",
            entry="- after",
        )
    finally:
        await store.close()
        shutdown_logging()

    assert "error" not in result
    history = vault / ".datacron" / "history" / hash_text(original_raw)
    assert history.read_text(encoding="utf-8") == original_raw
    records = await app.vault_writer.list_operations()
    assert len(records) == 1
    assert records[0].before_hash == hash_text(original_raw)
    assert records[0].after_hash == result["content_hash"]
    log_text = "\n".join(path.read_text(encoding="utf-8") for path in log_dir.glob("*.log"))
    assert "AUDIT tool=append_journal" in log_text


def test_invariant_manifest_collects_prop_01_through_prop_15() -> None:
    """The marker suite contains an explicit test for every PROP-01..PROP-15."""
    property_dir = Path(__file__).parent
    discovered: set[int] = set()
    pattern = re.compile(r"test_prop_(\d{2})")
    for path in property_dir.glob("test_*.py"):
        discovered.update(int(match) for match in pattern.findall(path.read_text(encoding="utf-8")))
    assert discovered == set(range(1, 16))
