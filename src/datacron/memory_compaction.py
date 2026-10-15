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
"""Smart memory compaction and monthly journal digest consolidation engine."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from ulid import ULID

from datacron.core.config import (
    MEMORY_SUBFOLDER_NAME,
    Settings,
    get_settings,
)
from datacron.core.durability import atomic_durable_write
from datacron.core.frontmatter import parse, serialize
from datacron.core.paths import (
    resolve_vault_rel_target,
    sidecar_index_db,
)
from datacron.core.vault import build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile

if TYPE_CHECKING:
    from pathlib import Path

    from datacron.core.models import Note

__all__ = [
    "DEFAULT_COMPACTION_DAYS",
    "CompactionCandidate",
    "MemoryCompactionReport",
    "MonthlyDigestSummary",
    "compact_vault_memory",
]

DEFAULT_COMPACTION_DAYS: Final[int] = 30
_JOURNAL_REL_PREFIX: Final[str] = f"{MEMORY_SUBFOLDER_NAME}/journal/"
_ARCHIVE_REL_DIR: Final[str] = f"{MEMORY_SUBFOLDER_NAME}/archive"
_COMPACTED_TAG: Final[str] = "archive/compacted"
_SUPERSEDED_TAG: Final[str] = "archive/superseded"
_DIGEST_TAGS: Final[tuple[str, ...]] = ("type/archive", "archive/journal-digest")
_DATE_STEM_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<ymd>\d{4}-\d{2}-\d{2})\b")


@dataclass(frozen=True, slots=True)
class CompactionCandidate:
    """One historical journal note eligible for monthly digest consolidation."""

    note_id: str
    rel_path: str
    title: str
    entry_date: str
    month_bucket: str
    digest_rel_path: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class MonthlyDigestSummary:
    """Summary of one monthly journal digest planned or written."""

    month_bucket: str
    digest_rel_path: str
    source_note_count: int
    source_rel_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryCompactionReport:
    """Outcome of a dry-run or applied memory compaction pass."""

    dry_run: bool
    older_than_days: int
    cutoff_date: str
    compacted_count: int
    digests_written: int
    candidate_journals: list[CompactionCandidate] = field(default_factory=list)
    digests_planned: list[MonthlyDigestSummary] = field(default_factory=list)
    superseded_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the compaction report to a JSON-compatible dictionary."""
        return asdict(self)


def _extract_journal_date(note: Note) -> date | None:
    stem = note.rel_path.rsplit("/", 1)[-1].removesuffix(".md")
    match = _DATE_STEM_RE.match(stem)
    if match is not None:
        try:
            return date.fromisoformat(match.group("ymd"))
        except ValueError:
            pass
    return note.updated.astimezone(UTC).date()


def _is_already_archived(note: Note) -> bool:
    folded_tags = {tag.casefold() for tag in note.tags}
    return (
        _COMPACTED_TAG in folded_tags
        or _SUPERSEDED_TAG in folded_tags
        or "compacted_into" in note.frontmatter
    )


def _collect_superseded_paths(notes: list[Note]) -> list[str]:
    notes_by_id = {note.id: note for note in notes}
    superseded_ids: set[str] = set()
    for note in notes:
        raw_sup = note.frontmatter.get("supersedes")
        if isinstance(raw_sup, list):
            for item in raw_sup:
                if isinstance(item, str) and item in notes_by_id:
                    superseded_ids.add(item)
        elif isinstance(raw_sup, str) and raw_sup in notes_by_id:
            superseded_ids.add(raw_sup)
        if note.frontmatter.get("superseded_by"):
            superseded_ids.add(note.id)
    return sorted(notes_by_id[nid].rel_path for nid in superseded_ids if nid in notes_by_id)


def _find_candidates(
    notes: list[Note],
    cutoff: date,
) -> tuple[list[CompactionCandidate], dict[str, list[Note]]]:
    candidates: list[CompactionCandidate] = []
    grouped_notes: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        normalized = note.rel_path.replace("\\", "/")
        if not normalized.casefold().startswith(_JOURNAL_REL_PREFIX.casefold()):
            continue
        if _is_already_archived(note):
            continue
        entry_date = _extract_journal_date(note)
        if entry_date is None or entry_date >= cutoff:
            continue
        month_bucket = entry_date.strftime("%Y-%m")
        digest_rel_path = f"{_ARCHIVE_REL_DIR}/journal-{month_bucket}.md"
        candidates.append(
            CompactionCandidate(
                note_id=note.id,
                rel_path=normalized,
                title=note.title,
                entry_date=entry_date.isoformat(),
                month_bucket=month_bucket,
                digest_rel_path=digest_rel_path,
                byte_size=len(note.content.encode("utf-8")),
            )
        )
        grouped_notes[month_bucket].append(note)
    candidates.sort(key=lambda item: (item.entry_date, item.rel_path))
    return candidates, grouped_notes


def _strip_top_heading(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        return "\n".join(lines[1:]).strip()
    return body.strip()


def _write_monthly_digest(
    vault_root: Path,
    settings: Settings,
    month_bucket: str,
    bucket_notes: list[Note],
    now_iso: str,
) -> str:
    digest_rel_path = f"{_ARCHIVE_REL_DIR}/journal-{month_bucket}.md"
    target_path = resolve_vault_rel_target(vault_root, digest_rel_path, settings=settings)
    existing_meta: dict[str, Any] = {}
    existing_body = ""
    if target_path.is_file():
        raw_existing = target_path.read_text(encoding="utf-8")
        existing_meta, existing_body = parse(raw_existing)

    digest_id = str(existing_meta.get("id") or ULID())
    prev_compacted = existing_meta.get("compacted_notes")
    compacted_list: list[str] = (
        [str(x) for x in prev_compacted if isinstance(x, str)]
        if isinstance(prev_compacted, list)
        else []
    )
    sections: list[str] = []
    if existing_body.strip():
        sections.append(existing_body.strip())
    else:
        sections.append(f"# Journal Digest {month_bucket}")

    sorted_notes = sorted(
        bucket_notes,
        key=lambda n: (_extract_journal_date(n) or date.min, n.rel_path),
    )
    for note in sorted_notes:
        if note.rel_path not in compacted_list:
            compacted_list.append(note.rel_path)
        entry_date_obj = _extract_journal_date(note)
        entry_date_str = entry_date_obj.isoformat() if entry_date_obj else note.title
        heading = f"## {entry_date_str} ({note.id})"
        if heading in existing_body:
            continue
        cleaned_body = _strip_top_heading(note.content)
        section_text = f"{heading}\n\nSource: [[{note.id}|{note.rel_path}]]\n\n{cleaned_body}"
        sections.append(section_text)

    metadata: dict[str, Any] = {
        **existing_meta,
        "id": digest_id,
        "title": f"Journal Digest {month_bucket}",
        "created": str(existing_meta.get("created") or now_iso),
        "updated": now_iso,
        "tags": list(_DIGEST_TAGS),
        "compacted_notes": compacted_list,
    }
    rendered = serialize(metadata, "\n\n".join(sections).strip() + "\n")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_durable_write(target_path, rendered.encode("utf-8"))
    return digest_rel_path


def _mark_source_note_compacted(
    vault_root: Path,
    settings: Settings,
    note: Note,
    digest_rel_path: str,
    now_iso: str,
) -> None:
    target_path = resolve_vault_rel_target(vault_root, note.rel_path, settings=settings)
    if not target_path.is_file():
        return
    raw_text = target_path.read_text(encoding="utf-8")
    meta, body = parse(raw_text)
    raw_tags = meta.get("tags")
    tags: list[str] = (
        [str(t) for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
    )
    if not any(t.casefold() == _COMPACTED_TAG for t in tags):
        tags.append(_COMPACTED_TAG)
    meta["id"] = meta.get("id") or note.id
    meta["tags"] = tags
    meta["compacted_into"] = digest_rel_path
    meta["updated"] = now_iso
    rendered = serialize(meta, body)
    atomic_durable_write(target_path, rendered.encode("utf-8"))


async def _reconcile_if_indexed(
    vault_root: Path,
    settings: Settings,
    reader: Any,
) -> None:
    db_path = sidecar_index_db(vault_root, settings=settings)
    if not db_path.is_file():
        return
    store = SQLiteFTS5Store()
    await store.open(db_path)
    try:
        chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
        await reconcile(store, reader, chunker, mtime_gate=False)
    finally:
        await store.close()


async def compact_vault_memory(
    vault_root: Path,
    *,
    settings: Settings | None = None,
    older_than_days: int = DEFAULT_COMPACTION_DAYS,
    dry_run: bool = True,
    reference_date: date | None = None,
) -> MemoryCompactionReport:
    """Consolidate historical `_memory/journal/` notes into monthly archive digests."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    resolved_root = vault_root.expanduser().resolve()
    active_settings = settings or get_settings()
    reader = build_configured_reader(
        resolved_root,
        read_only=dry_run,
        settings=active_settings,
    )
    notes = await reader.list_notes(folder=MEMORY_SUBFOLDER_NAME)
    today = reference_date or datetime.now(UTC).date()
    cutoff = today - timedelta(days=older_than_days)
    candidates, grouped_notes = _find_candidates(notes, cutoff)
    superseded_paths = _collect_superseded_paths(notes)

    digests_planned = [
        MonthlyDigestSummary(
            month_bucket=bucket,
            digest_rel_path=f"{_ARCHIVE_REL_DIR}/journal-{bucket}.md",
            source_note_count=len(bucket_notes),
            source_rel_paths=sorted(n.rel_path for n in bucket_notes),
        )
        for bucket, bucket_notes in sorted(grouped_notes.items())
    ]

    if dry_run or not candidates:
        return MemoryCompactionReport(
            dry_run=dry_run,
            older_than_days=older_than_days,
            cutoff_date=cutoff.isoformat(),
            compacted_count=0,
            digests_written=0,
            candidate_journals=candidates,
            digests_planned=digests_planned,
            superseded_notes=superseded_paths,
        )

    now_iso = datetime.now(UTC).isoformat()
    digests_written = 0
    compacted_count = 0
    for bucket, bucket_notes in sorted(grouped_notes.items()):
        digest_rel = _write_monthly_digest(
            resolved_root, active_settings, bucket, bucket_notes, now_iso
        )
        digests_written += 1
        for note in bucket_notes:
            _mark_source_note_compacted(resolved_root, active_settings, note, digest_rel, now_iso)
            compacted_count += 1

    await _reconcile_if_indexed(resolved_root, active_settings, reader)
    return MemoryCompactionReport(
        dry_run=False,
        older_than_days=older_than_days,
        cutoff_date=cutoff.isoformat(),
        compacted_count=compacted_count,
        digests_written=digests_written,
        candidate_journals=candidates,
        digests_planned=digests_planned,
        superseded_notes=superseded_paths,
    )
