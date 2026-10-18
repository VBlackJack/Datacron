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
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.durability import ReadOnlyModeError, ReadOnlyVaultError
from datacron.core.frontmatter import (
    coerce_string_list,
    parse_preserving_bom_and_body_eols,
    serialize,
)
from datacron.core.frontmatter_edit import (
    FrontmatterEditRefusedError,
    serialize_preserving_frontmatter,
)
from datacron.core.operation_log import OperationContext
from datacron.core.paths import (
    effective_memory_root,
    resolve_vault_rel_target,
    sidecar_index_db,
    sidecar_vault_config,
)
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault import build_configured_reader
from datacron.core.vault_writer import FilesystemVaultWriter
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
    "ReadOnlyModeError",
    "ReadOnlyVaultError",
    "SkippedCompactionNote",
    "compact_vault_memory",
]

DEFAULT_COMPACTION_DAYS: Final[int] = 30
_JOURNAL_REL_PREFIX: Final[str] = f"{MEMORY_SUBFOLDER_NAME}/journal/"
_ARCHIVE_REL_DIR: Final[str] = f"{MEMORY_SUBFOLDER_NAME}/archive"
_COMPACTED_TAG: Final[str] = "archive/compacted"
_MEMORY_ARCHIVE_TAG: Final[str] = "memory/archive"
_SUPERSEDED_TAG: Final[str] = "archive/superseded"
# Compacted notes and digests also carry ``archived: true``. The index honours
# that key under any archive tag list, so a vault that declares its own list
# without the compaction tags still demotes them.
_ARCHIVED_KEY: Final[str] = "archived"
_DIGEST_TAGS: Final[tuple[str, ...]] = (
    "type/archive",
    "archive/journal-digest",
    _MEMORY_ARCHIVE_TAG,
)
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
class SkippedCompactionNote:
    """One candidate journal left uncompacted, with the reason it was skipped."""

    rel_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryCompactionReport:
    """Outcome of a dry-run or applied memory compaction pass.

    ``skipped_notes`` lists candidates whose frontmatter cannot be edited without
    rewriting keys compaction does not set. They are left untouched and kept out
    of the digest, so a digest's ``compacted_notes`` names only marked notes.
    """

    dry_run: bool
    older_than_days: int
    cutoff_date: str
    compacted_count: int
    digests_written: int
    candidate_journals: list[CompactionCandidate] = field(default_factory=list)
    digests_planned: list[MonthlyDigestSummary] = field(default_factory=list)
    superseded_notes: list[str] = field(default_factory=list)
    skipped_notes: list[SkippedCompactionNote] = field(default_factory=list)

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


def _verify_memory_paths_without_links(resolved_root: Path, settings: Settings) -> Path:
    memory_base = effective_memory_root(resolved_root, settings=settings) or resolved_root
    memory_dir = memory_base / MEMORY_SUBFOLDER_NAME
    assert_path_chain_without_links(memory_dir, anchor=memory_base, allow_missing=True)
    for subdir_name in ("journal", "archive"):
        subdir = memory_dir / subdir_name
        assert_path_chain_without_links(subdir, anchor=memory_base, allow_missing=True)
        if subdir.exists():
            for entry in subdir.rglob("*"):
                assert_path_chain_without_links(entry, anchor=memory_base, allow_missing=True)
    return memory_dir


def _build_digest_content(
    month_bucket: str,
    bucket_notes: list[Note],
    now_iso: str,
    *,
    existing_meta: dict[str, Any],
    existing_body: str,
) -> tuple[str, dict[str, Any], str]:
    """Return the digest id, its frontmatter and its body; the caller renders them."""
    digest_id = str(existing_meta.get("id") or ULID())
    prev_compacted = coerce_string_list(existing_meta.get("compacted_notes"), split_delimited=False)
    compacted_list: list[str] = list(prev_compacted)
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
        target_no_ext = note.rel_path.removesuffix(".md")
        source_line = f"Source: [[{target_no_ext}|{note.rel_path}]] (id: `{note.id}`)"
        section_text = f"{heading}\n\n{source_line}\n\n{cleaned_body}"
        sections.append(section_text)

    existing_tags = coerce_string_list(existing_meta.get("tags"))
    merged_tags = list(existing_tags)
    for req_tag in _DIGEST_TAGS:
        if not any(t.casefold() == req_tag.casefold() for t in merged_tags):
            merged_tags.append(req_tag)

    metadata: dict[str, Any] = {
        **existing_meta,
        "id": digest_id,
        "title": f"Journal Digest {month_bucket}",
        # An existing value is kept as parsed: turning a YAML date into its
        # string form would rewrite a key compaction has no reason to touch.
        "created": existing_meta.get("created") or now_iso,
        "updated": now_iso,
        "tags": merged_tags,
        "compacted_notes": compacted_list,
        _ARCHIVED_KEY: True,
    }
    body_text = "\n\n".join(sections).strip() + "\n"
    return digest_id, metadata, body_text


def _with_trailing_newline(rendered: str, raw_text: str) -> str:
    if rendered.endswith(("\n", "\r\n")):
        return rendered
    return rendered + ("\r\n" if "\r\n" in raw_text else "\n")


async def _write_monthly_digest(
    vault_root: Path,
    settings: Settings,
    writer: FilesystemVaultWriter,
    month_bucket: str,
    bucket_notes: list[Note],
    now_iso: str,
) -> str:
    digest_rel_path = f"{_ARCHIVE_REL_DIR}/journal-{month_bucket}.md"
    target_path = resolve_vault_rel_target(vault_root, digest_rel_path, settings=settings)
    op_ctx = OperationContext(
        op="compact_memory_digest",
        tool="compact_vault_memory",
        actor="datacron:memory_compaction",
        parameters={"month_bucket": month_bucket},
    )
    if target_path.is_file():

        def _mutate_digest(raw_existing: str) -> str:
            existing_meta, existing_body, has_bom = parse_preserving_bom_and_body_eols(raw_existing)
            _, metadata, body_text = _build_digest_content(
                month_bucket,
                bucket_notes,
                now_iso,
                existing_meta=existing_meta,
                existing_body=existing_body,
            )
            # Only the keys compaction sets are rendered again; the rest of the
            # digest's frontmatter keeps its bytes, comments and key order.
            rendered = serialize_preserving_frontmatter(
                raw_existing, metadata, body_text, has_bom=has_bom
            )
            return _with_trailing_newline(rendered, raw_existing)

        await writer.mutate_note_atomic(
            digest_rel_path,
            _mutate_digest,
            operation=op_ctx,
        )
    else:
        digest_id, metadata, body_text = _build_digest_content(
            month_bucket,
            bucket_notes,
            now_iso,
            existing_meta={},
            existing_body="",
        )
        rendered = _with_trailing_newline(serialize(metadata, body_text), "")
        await writer.write_note_atomic(
            digest_rel_path,
            rendered,
            overwrite=False,
            note_id=digest_id,
            operation=op_ctx,
        )
    return digest_rel_path


def _render_compacted_source(
    raw_text: str,
    note_id: str,
    digest_rel_path: str,
    now_iso: str,
) -> str:
    """Return ``raw_text`` marked as compacted into ``digest_rel_path``.

    Only the keys compaction sets are rendered again. A PyYAML round trip of the
    whole block turned 14:30 into 870, 1.10 into 1.1 and NO into false, and
    dropped every comment, in notes compaction only meant to tag. A block that
    cannot be edited that way raises :class:`FrontmatterEditRefusedError`.
    """
    meta, body, has_bom = parse_preserving_bom_and_body_eols(raw_text)
    tags = coerce_string_list(meta.get("tags"))
    for required_tag in (_COMPACTED_TAG, _MEMORY_ARCHIVE_TAG):
        if not any(t.casefold() == required_tag.casefold() for t in tags):
            tags.append(required_tag)
    meta["id"] = meta.get("id") or note_id
    meta["tags"] = tags
    meta["compacted_into"] = digest_rel_path
    meta["updated"] = now_iso
    meta[_ARCHIVED_KEY] = True
    rendered = serialize_preserving_frontmatter(raw_text, meta, body, has_bom=has_bom)
    return _with_trailing_newline(rendered, raw_text)


def _partition_markable(
    bucket_notes: list[Note],
    digest_rel_path: str,
    now_iso: str,
) -> tuple[list[Note], list[SkippedCompactionNote]]:
    """Split notes into those compaction can mark and those it must leave alone.

    The check runs before the digest is written, so a note that cannot be marked
    never gets a section or a ``compacted_notes`` entry it does not carry itself.
    """
    markable: list[Note] = []
    skipped: list[SkippedCompactionNote] = []
    for note in bucket_notes:
        try:
            _render_compacted_source(note.raw_content, note.id, digest_rel_path, now_iso)
        except FrontmatterEditRefusedError as exc:
            skipped.append(SkippedCompactionNote(rel_path=note.rel_path, reason=str(exc)))
            continue
        markable.append(note)
    return markable, skipped


async def _mark_source_note_compacted(
    vault_root: Path,
    settings: Settings,
    writer: FilesystemVaultWriter,
    note: Note,
    digest_rel_path: str,
    now_iso: str,
) -> None:
    target_path = resolve_vault_rel_target(vault_root, note.rel_path, settings=settings)
    if not target_path.is_file():
        return

    def _mutate_source(raw_text: str) -> str:
        return _render_compacted_source(raw_text, note.id, digest_rel_path, now_iso)

    await writer.mutate_note_atomic(
        note.rel_path,
        _mutate_source,
        operation=OperationContext(
            op="compact_memory_source",
            tool="compact_vault_memory",
            actor="datacron:memory_compaction",
            parameters={"digest_rel_path": digest_rel_path},
        ),
    )


async def _reconcile_if_indexed(
    vault_root: Path,
    settings: Settings,
    reader: Any,
    vault_cfg: VaultConfig,
) -> None:
    db_path = sidecar_index_db(vault_root, settings=settings)
    if not db_path.is_file():
        return
    archive_tags = (
        *vault_cfg.archive_tags,
        _COMPACTED_TAG,
        "archive/journal-digest",
        _MEMORY_ARCHIVE_TAG,
    )
    store = SQLiteFTS5Store(
        term_map=vault_cfg.query_expansion,
        archive_tags=archive_tags,
    )
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
    if not dry_run and active_settings.read_only:
        raise ReadOnlyVaultError("Memory compaction apply is not permitted in read-only mode.")

    memory_dir = _verify_memory_paths_without_links(resolved_root, active_settings)
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

    now_iso = datetime.now(UTC).isoformat()
    if dry_run or not candidates:
        # A preview names the notes an apply would skip, from the same check.
        preview_skipped = [
            skipped
            for bucket, bucket_notes in sorted(grouped_notes.items())
            for skipped in _partition_markable(
                bucket_notes, f"{_ARCHIVE_REL_DIR}/journal-{bucket}.md", now_iso
            )[1]
        ]
        return MemoryCompactionReport(
            dry_run=dry_run,
            older_than_days=older_than_days,
            cutoff_date=cutoff.isoformat(),
            compacted_count=0,
            digests_written=0,
            candidate_journals=candidates,
            digests_planned=digests_planned,
            superseded_notes=superseded_paths,
            skipped_notes=preview_skipped,
        )

    writer_settings = (
        active_settings
        if active_settings.write_paths
        else active_settings.model_copy(update={"write_paths": [memory_dir]})
    )
    vault_cfg = (
        load_vault_config(sidecar_vault_config(resolved_root, settings=active_settings))
        or VaultConfig()
    )
    writer = FilesystemVaultWriter(
        resolved_root,
        writer_settings,
        vault_config=vault_cfg,
    )

    digests_written = 0
    compacted_count = 0
    skipped_notes: list[SkippedCompactionNote] = []
    for bucket, all_bucket_notes in sorted(grouped_notes.items()):
        digest_rel_path = f"{_ARCHIVE_REL_DIR}/journal-{bucket}.md"
        bucket_notes, bucket_skipped = _partition_markable(
            all_bucket_notes, digest_rel_path, now_iso
        )
        skipped_notes.extend(bucket_skipped)
        if not bucket_notes:
            continue
        try:
            digest_rel = await _write_monthly_digest(
                resolved_root,
                active_settings,
                writer,
                bucket,
                bucket_notes,
                now_iso,
            )
        except FrontmatterEditRefusedError as exc:
            # An existing digest whose frontmatter cannot be edited in place is
            # left as it is, and so is every note of its month: marking them
            # would point them at a digest that does not hold their sections.
            skipped_notes.append(SkippedCompactionNote(rel_path=digest_rel_path, reason=str(exc)))
            skipped_notes.extend(
                SkippedCompactionNote(rel_path=note.rel_path, reason=str(exc))
                for note in bucket_notes
            )
            continue
        digests_written += 1
        for note in bucket_notes:
            try:
                await _mark_source_note_compacted(
                    resolved_root,
                    active_settings,
                    writer,
                    note,
                    digest_rel,
                    now_iso,
                )
            except FrontmatterEditRefusedError as exc:
                # The note changed on disk after the check above. The digest
                # already holds its section; the report names it so the two
                # can be told apart, and a rerun after a fix marks it.
                skipped_notes.append(SkippedCompactionNote(rel_path=note.rel_path, reason=str(exc)))
                continue
            compacted_count += 1

    await _reconcile_if_indexed(resolved_root, active_settings, reader, vault_cfg)
    return MemoryCompactionReport(
        dry_run=False,
        older_than_days=older_than_days,
        cutoff_date=cutoff.isoformat(),
        compacted_count=compacted_count,
        digests_written=digests_written,
        candidate_journals=candidates,
        digests_planned=digests_planned,
        superseded_notes=superseded_paths,
        skipped_notes=skipped_notes,
    )
