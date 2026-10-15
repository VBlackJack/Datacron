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
"""Knowledge-graph topology and wikilink integrity diagnostics across vaults."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final

from datacron.core.config import Settings, get_settings
from datacron.core.models import ChunkType
from datacron.core.paths import note_source_vault
from datacron.core.vault import build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.wikilinks import (
    OpenFence,
    extract_wikilink_targets,
    open_fence_after,
)

if TYPE_CHECKING:
    from pathlib import Path

    from datacron.core.models import Note
    from datacron.core.protocols import VaultReader

__all__ = [
    "DEFAULT_HUB_LIMIT",
    "BrokenWikilink",
    "CrossVaultLink",
    "GraphHealthReport",
    "HubNote",
    "OrphanNote",
    "analyze_graph_health",
    "analyze_vault_graph",
]

DEFAULT_HUB_LIMIT: Final[int] = 10
_JOURNAL_PREFIX: Final[str] = "_memory/journal/"
_ARCHIVE_PREFIX: Final[str] = "_memory/archive/"
_INIT_REL_PATH: Final[str] = "_memory/INIT.md"


@dataclass(frozen=True, slots=True)
class BrokenWikilink:
    """An outgoing wikilink whose target does not resolve to any live note."""

    source_note_id: str
    source_rel_path: str
    source_vault: str
    target_alias: str
    section_title: str | None = None


@dataclass(frozen=True, slots=True)
class OrphanNote:
    """A non-journal note with zero incoming and zero outgoing wikilinks."""

    note_id: str
    rel_path: str
    title: str
    source_vault: str


@dataclass(frozen=True, slots=True)
class HubNote:
    """A high-centrality note ranked by incoming and outgoing wikilink degree."""

    note_id: str
    rel_path: str
    title: str
    source_vault: str
    incoming_count: int
    outgoing_count: int


@dataclass(frozen=True, slots=True)
class CrossVaultLink:
    """A wikilink connecting notes across distinct logical vault roots."""

    source_rel_path: str
    source_vault: str
    target_rel_path: str
    target_vault: str
    target_alias: str


@dataclass(frozen=True, slots=True)
class GraphHealthReport:
    """Complete link-integrity and topology snapshot for a Datacron vault."""

    total_notes: int
    total_links: int
    resolved_links: int
    broken_link_count: int
    orphan_note_count: int
    cross_vault_link_count: int
    broken_links: list[BrokenWikilink] = field(default_factory=list)
    orphan_notes: list[OrphanNote] = field(default_factory=list)
    hub_notes: list[HubNote] = field(default_factory=list)
    cross_vault_links: list[CrossVaultLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph health report to a JSON-compatible dictionary."""
        return asdict(self)


def _is_exempt_from_orphan_check(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    if normalized.casefold() == _INIT_REL_PATH.casefold():
        return True
    lower = normalized.casefold()
    return lower.startswith((_JOURNAL_PREFIX, _ARCHIVE_PREFIX))


def _extract_note_links(
    note: Note,
    chunker: MarkdownChunker,
) -> list[tuple[str, str | None]]:
    chunks = chunker.chunk(note)
    pairs: list[tuple[str, str | None]] = []
    open_fence: OpenFence | None = None
    for chunk in chunks:
        if chunk.chunk_type is ChunkType.CODE:
            continue
        targets = extract_wikilink_targets(
            chunk.content,
            chunk.chunk_type,
            open_fence=open_fence,
        )
        open_fence = open_fence_after(
            chunk.content,
            open_fence=open_fence,
        )
        for target in targets:
            cleaned = target.strip()
            if cleaned:
                pairs.append((cleaned, chunk.section_title))
    return pairs


async def _resolve_target(
    reader: VaultReader,
    notes_by_id: dict[str, Note],
    notes_by_folded_id: dict[str, str],
    alias_cache: dict[str, str | None],
    target: str,
) -> str | None:
    key = target.casefold()
    if key in alias_cache:
        return alias_cache[key]
    canonical_id = notes_by_folded_id.get(key)
    if canonical_id is not None:
        alias_cache[key] = canonical_id
        return canonical_id
    resolved = await reader.resolve_alias(target)
    if resolved is not None and resolved not in notes_by_id:
        resolved = None
    alias_cache[key] = resolved
    return resolved


def _rank_hubs(
    notes_by_id: dict[str, Note],
    incoming_counts: Counter[str],
    outgoing_counts: Counter[str],
    hub_limit: int,
) -> list[HubNote]:
    scored = [
        HubNote(
            note_id=note_id,
            rel_path=note.rel_path,
            title=note.title,
            source_vault=note_source_vault(note.rel_path),
            incoming_count=incoming_counts.get(note_id, 0),
            outgoing_count=outgoing_counts.get(note_id, 0),
        )
        for note_id, note in notes_by_id.items()
        if incoming_counts.get(note_id, 0) > 0 or outgoing_counts.get(note_id, 0) > 0
    ]
    scored.sort(key=lambda item: (-item.incoming_count, -item.outgoing_count, item.rel_path))
    return scored[: max(0, hub_limit)]


async def analyze_graph_health(
    reader: VaultReader,
    *,
    chunker: MarkdownChunker | None = None,
    hub_limit: int = DEFAULT_HUB_LIMIT,
) -> GraphHealthReport:
    """Compute broken links, orphan notes, hub notes, and cross-vault links."""
    active_chunker = chunker or MarkdownChunker()
    notes = await reader.list_notes()
    notes_by_id = {note.id: note for note in notes}
    notes_by_folded_id = {note.id.casefold(): note.id for note in notes}
    alias_cache: dict[str, str | None] = {}

    incoming_counts: Counter[str] = Counter()
    outgoing_counts: Counter[str] = Counter()
    broken_links: list[BrokenWikilink] = []
    cross_vault_links: list[CrossVaultLink] = []
    total_links = 0
    resolved_links = 0

    for note in notes:
        src_vault = note_source_vault(note.rel_path)
        link_pairs = _extract_note_links(note, active_chunker)
        for target_alias, section_title in link_pairs:
            total_links += 1
            target_id = await _resolve_target(
                reader, notes_by_id, notes_by_folded_id, alias_cache, target_alias
            )
            if target_id is None:
                broken_links.append(
                    BrokenWikilink(
                        source_note_id=note.id,
                        source_rel_path=note.rel_path,
                        source_vault=src_vault,
                        target_alias=target_alias,
                        section_title=section_title,
                    )
                )
                continue
            resolved_links += 1
            if target_id != note.id:
                outgoing_counts[note.id] += 1
                incoming_counts[target_id] += 1
            target_note = notes_by_id[target_id]
            dst_vault = note_source_vault(target_note.rel_path)
            if src_vault != dst_vault:
                cross_vault_links.append(
                    CrossVaultLink(
                        source_rel_path=note.rel_path,
                        source_vault=src_vault,
                        target_rel_path=target_note.rel_path,
                        target_vault=dst_vault,
                        target_alias=target_alias,
                    )
                )

    orphan_notes = [
        OrphanNote(
            note_id=note.id,
            rel_path=note.rel_path,
            title=note.title,
            source_vault=note_source_vault(note.rel_path),
        )
        for note in notes
        if incoming_counts.get(note.id, 0) == 0
        and outgoing_counts.get(note.id, 0) == 0
        and not _is_exempt_from_orphan_check(note.rel_path)
    ]
    hub_notes = _rank_hubs(notes_by_id, incoming_counts, outgoing_counts, hub_limit)

    return GraphHealthReport(
        total_notes=len(notes),
        total_links=total_links,
        resolved_links=resolved_links,
        broken_link_count=len(broken_links),
        orphan_note_count=len(orphan_notes),
        cross_vault_link_count=len(cross_vault_links),
        broken_links=broken_links,
        orphan_notes=orphan_notes,
        hub_notes=hub_notes,
        cross_vault_links=cross_vault_links,
    )


async def analyze_vault_graph(
    vault_root: Path,
    *,
    settings: Settings | None = None,
    hub_limit: int = DEFAULT_HUB_LIMIT,
) -> GraphHealthReport:
    """High-level helper that builds a configured reader and analyzes its graph."""
    resolved_root = vault_root.expanduser().resolve()
    active_settings = settings or get_settings()
    reader = build_configured_reader(
        resolved_root,
        read_only=True,
        settings=active_settings,
    )
    chunker = MarkdownChunker(max_tokens=active_settings.chunk_max_tokens)
    return await analyze_graph_health(reader, chunker=chunker, hub_limit=hub_limit)
