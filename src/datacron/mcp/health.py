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
"""Truthful, read-only operational health assembly for the MCP surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING, Any, Final

from datacron import __version__
from datacron.core.hashing import index_generation_hash
from datacron.reliability import scan_vault_read_only
from datacron.scrubber import read_scrubber_health

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

__all__ = ["build_health", "index_generation", "vault_checksum"]

_EVIDENCE_RESOURCE: Final[str] = "reliability_evidence.json"
_VALID_INVARIANT_STATUSES: Final[frozenset[str]] = frozenset(
    {"PROVEN", "BASELINE-TRACKED", "DEFERRED"}
)


async def build_health(app: DatacronApp) -> dict[str, Any]:
    """Return live operational truth without modifying notes or sidecars."""
    root = app.scope.authorize_path(app.vault_root, "read")
    scan_task = asyncio.to_thread(scan_vault_read_only, root)
    notes_task = app.vault_reader.list_notes()
    stat_task = app.vault_reader.stat_notes()
    stats_task = app.store.stats()
    indexed_task = app.store.list_indexed_notes()
    scan, notes, stat_rows, stats, indexed = await asyncio.gather(
        scan_task,
        notes_task,
        stat_task,
        stats_task,
        indexed_task,
    )

    live_rows = {note.rel_path: note.content_hash for note in notes}
    indexed_rows = {rel_path: row[1] for rel_path, row in indexed.items()}
    stale_paths = sorted(
        rel_path
        for rel_path in live_rows.keys() | indexed_rows.keys()
        if live_rows.get(rel_path) != indexed_rows.get(rel_path)
    )
    hash_divergences = sorted(
        rel_path
        for rel_path in live_rows.keys() & indexed_rows.keys()
        if live_rows[rel_path] != indexed_rows[rel_path]
    )
    staleness_seconds = _staleness_seconds(
        exact=not stale_paths,
        last_reindex=stats.last_indexed_at,
        mtimes_ns=[mtime_ns for _path, mtime_ns in stat_rows.values()],
    )
    evidence = _load_reliability_evidence()
    statuses = evidence["invariants"]
    proven = sum(status == "PROVEN" for status in statuses.values())
    baseline_tracked = sum(status == "BASELINE-TRACKED" for status in statuses.values())
    deferred = sum(status == "DEFERRED" for status in statuses.values())

    durability = app.durability_status.to_dict(mode=app.settings.durability)
    durability["writes_allowed"] = app.write_policy.writes_allowed
    durability["write_paths_configured"] = app.write_policy.write_paths_configured
    durability["effective_writes_enabled"] = app.write_policy.effective_writes_enabled
    durability["read_only"] = app.settings.read_only
    scrubber = read_scrubber_health(
        root,
        app.settings,
        app.scope,
        current_generation=stats.generation,
        current_total_notes=stats.note_count,
    )
    scrubber_anomaly_count = scrubber["anomalies_count"]
    scrubber_critical = isinstance(scrubber_anomaly_count, int) and scrubber_anomaly_count > 0
    healthy = (
        not stale_paths
        and not scan.parse_errors
        and not scan.id_violations
        and not scan.broken_wikilinks
        and not scan.mixed_eol_notes
        and not scan.supersedes_cycles
        and (app.settings.durability != "strict" or app.durability_status.directory_flush_supported)
    )
    return {
        "status": "critical" if scrubber_critical else ("healthy" if healthy else "degraded"),
        "server_version": __version__,
        "read_only": app.settings.read_only,
        "index": {
            "generation": stats.generation,
            "generation_hash": index_generation(indexed),
            "last_reindex": (
                stats.last_indexed_at.isoformat() if stats.last_indexed_at is not None else None
            ),
            "notes_count": stats.note_count,
            "vault_notes_count": len(notes),
            "chunks_count": stats.chunk_count,
            "consistent_with_vault": not stale_paths,
            "stale_entries": len(stale_paths),
            "hash_divergences": len(hash_divergences),
            "staleness_seconds": staleness_seconds,
        },
        "integrity": {
            "notes_count": scan.notes_count,
            "id_mismatches": len(scan.id_violations),
            "broken_wikilinks": len(scan.broken_wikilinks),
            "mixed_eol_notes": len(scan.mixed_eol_notes),
            "supersedes_cycles": len(scan.supersedes_cycles),
            "frontmatter_parse_errors": len(scan.parse_errors),
        },
        "vault_checksum": {
            "algorithm": "sha256-path-content-hash-rollup-v1",
            "value": vault_checksum(dict(scan.content_hashes)),
            "notes_count": scan.notes_count,
            "scope": "all non-hidden Markdown notes in the reliability scan",
            "claim": "point-in-time Markdown byte integrity; not future durability",
        },
        "durability": durability,
        "scrubber": scrubber,
        "invariants": {
            "summary": {
                "proven": proven,
                "baseline_tracked": baseline_tracked,
                "deferred": deferred,
            },
            "statuses": statuses,
            "scope_notes": evidence.get("scope_notes", {}),
        },
    }


def vault_checksum(content_hashes: dict[str, str]) -> str:
    """Hash sorted relative paths and byte-exact note content hashes."""
    digest = hashlib.sha256()
    for rel_path, content_hash in sorted(content_hashes.items()):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def index_generation(indexed: dict[str, tuple[str, str]]) -> str:
    """Hash the exact indexed path, identity, and content-hash rows."""
    return index_generation_hash(indexed)


def _staleness_seconds(
    *,
    exact: bool,
    last_reindex: datetime | None,
    mtimes_ns: list[int],
) -> float | None:
    if exact:
        return 0.0
    if last_reindex is None or not mtimes_ns:
        return None
    normalized = (
        last_reindex.replace(tzinfo=UTC)
        if last_reindex.tzinfo is None
        else last_reindex.astimezone(UTC)
    )
    newest_live = datetime.fromtimestamp(max(mtimes_ns) / 1_000_000_000, tz=UTC)
    return round(max(0.0, (newest_live - normalized).total_seconds()), 6)


def _load_reliability_evidence() -> dict[str, dict[str, str]]:
    raw = resources.files("datacron").joinpath(_EVIDENCE_RESOURCE).read_text(encoding="ascii")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("reliability evidence must be a JSON object")
    invariants = payload.get("invariants")
    expected = {f"I{index}" for index in range(1, 16)}
    if not isinstance(invariants, dict) or set(invariants) != expected:
        raise ValueError("reliability evidence must define exactly I1 through I15")
    if any(status not in _VALID_INVARIANT_STATUSES for status in invariants.values()):
        raise ValueError("reliability evidence contains an invalid invariant status")
    scope_notes = payload.get("scope_notes", {})
    if not isinstance(scope_notes, dict):
        raise ValueError("reliability evidence scope_notes must be an object")
    return {
        "invariants": {str(key): str(value) for key, value in invariants.items()},
        "scope_notes": {str(key): str(value) for key, value in scope_notes.items()},
    }
