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
"""Read-only global reliability scans and baseline comparison.

The scanner deliberately does not instantiate ``FilesystemVaultReader``:
notes without frontmatter IDs would otherwise be assigned a persisted sidecar
ID. It does reuse the reader's :class:`NoteAdmissionPolicy`, so that both agree
on what belongs to the vault without the scan acquiring the reader's write side
effects. Markdown bytes, JSON sidecars, and SQLite are only read; SQLite
databases are opened with ``mode=ro``.

Invariants are reported for admitted notes only. A defect on a note that
``excluded_folders`` removes is outside the regular reader, index, and invariant
counters, so reporting it would pin health to ``degraded`` outside that serving
scope. Exclusion is not a write ACL: an independently write-authorized mutator
can still reach the path. The byte checksum is the deliberate exception: it
stays exhaustive, because narrowing it would silently change what an earlier
trusted value means.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from datacron.core.config import VaultConfig, load_vault_config
from datacron.core.frontmatter import (
    FrontmatterError,
    build_tiered_alias_index,
    coerce_string_list,
    parse,
    resolve_note_title,
)
from datacron.core.hashing import hash_text
from datacron.core.logger import get_logger
from datacron.core.models import Note
from datacron.core.paths import read_ulid_mappings, sidecar_vault_config
from datacron.core.vault import SKIPPED_FOLDERS, NoteAdmissionPolicy
from datacron.indexing.chunker import MarkdownChunker

__all__ = [
    "WIKILINK_EXISTING_UNDER_OTHER_TITLE_OR_ALIAS",
    "WIKILINK_NONEXISTENT",
    "BaselineComparison",
    "ReliabilityScan",
    "ReliabilityViolation",
    "compare_with_baseline",
    "scan_vault_read_only",
]

WIKILINK_NONEXISTENT: Final[str] = "nonexistent"
"""Target resolves to no note at all: an intent link toward a note yet to be written."""

WIKILINK_EXISTING_UNDER_OTHER_TITLE_OR_ALIAS: Final[str] = "existing_under_other_title_or_alias"
"""Target matches an existing note under another title or alias: a genuine misdirection."""

_H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?m)^#\s+(.+?)\s*$")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".datacron", ".git", ".hg", ".svn", "__pycache__", "node_modules"}
)
_SCANNER_NOTE_ID: Final[str] = "01J00000000000000000000000"
_SCANNER_TIMESTAMP: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def _logger() -> logging.Logger:
    return get_logger(__name__)


@dataclass(frozen=True)
class ReliabilityNote:
    """Minimal note representation needed by global invariant scans."""

    rel_path: str
    metadata: dict[str, Any]
    body: str
    title: str
    aliases: tuple[str, ...]
    mixed_eol: bool
    content_hash: str

    @property
    def frontmatter_id(self) -> str | None:
        value = self.metadata.get("id")
        return value if isinstance(value, str) and value else None

    @property
    def supersedes(self) -> tuple[str, ...]:
        return tuple(coerce_string_list(self.metadata.get("supersedes")))


@dataclass(frozen=True)
class ReliabilityViolation:
    """One stable, baseline-addressable invariant violation."""

    kind: str
    key: str
    rel_path: str
    target: str | None = None
    classification: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "key": self.key,
            "fingerprint": self.fingerprint,
            "rel_path": self.rel_path,
        }
        if self.target is not None:
            payload["target"] = self.target
        if self.classification is not None:
            payload["classification"] = self.classification
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class ReliabilityScan:
    """Complete result of one read-only vault scan."""

    notes_count: int
    id_violations: tuple[ReliabilityViolation, ...]
    broken_wikilinks: tuple[ReliabilityViolation, ...]
    supersedes_cycles: tuple[ReliabilityViolation, ...]
    mixed_eol_notes: tuple[str, ...]
    content_hashes: tuple[tuple[str, str], ...]
    parse_errors: tuple[str, ...]

    @property
    def violations(self) -> tuple[ReliabilityViolation, ...]:
        return (*self.id_violations, *self.broken_wikilinks, *self.supersedes_cycles)

    @property
    def misdirected_wikilinks(self) -> tuple[ReliabilityViolation, ...]:
        """Broken wikilinks whose target exists under another title or alias.

        A link classified ``nonexistent`` is excluded on purpose: vaults may use an
        unresolved link to mark a note that still has to be written, so it carries an
        editorial backlog rather than a defect. A link that misses an existing note is
        always a mistake, which is what operational health has to keep reporting.
        """
        return tuple(
            violation
            for violation in self.broken_wikilinks
            if violation.classification != WIKILINK_NONEXISTENT
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": {
                "notes": self.notes_count,
                "id_coherence_violations": len(self.id_violations),
                "broken_wikilinks": len(self.broken_wikilinks),
                "supersedes_cycles": len(self.supersedes_cycles),
                "mixed_eol_notes": len(self.mixed_eol_notes),
                "frontmatter_parse_errors": len(self.parse_errors),
            },
            "id_coherence": [item.to_dict() for item in self.id_violations],
            "broken_wikilinks": [item.to_dict() for item in self.broken_wikilinks],
            "supersedes_cycles": [item.to_dict() for item in self.supersedes_cycles],
            "mixed_eol_notes": list(self.mixed_eol_notes),
            "frontmatter_parse_errors": list(self.parse_errors),
        }


@dataclass(frozen=True)
class BaselineComparison:
    """New and retired violations relative to an accepted baseline."""

    new_violations: tuple[ReliabilityViolation, ...]
    accepted_violations: tuple[ReliabilityViolation, ...]
    stale_fingerprints: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.new_violations


def scan_vault_read_only(vault_root: Path) -> ReliabilityScan:
    """Scan all Markdown notes without mutating the vault or its sidecars."""
    root = vault_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"vault root does not exist: {root}")

    every_note, every_parse_error = _load_notes(root)
    policy = _admission_policy(root)
    notes = [note for note in every_note if _is_admitted(note.rel_path, policy)]
    parse_errors = [
        entry for entry in every_parse_error if _is_admitted(entry.split(":", 1)[0], policy)
    ]
    sidecar_ids = _load_sidecar_ids(root)
    sqlite_ids = _load_sqlite_ids(root)
    canonical_ids = {
        note.rel_path: sqlite_ids.get(note.rel_path)
        or sidecar_ids.get(note.rel_path)
        or note.frontmatter_id
        for note in notes
    }
    id_violations = _scan_id_coherence(notes, sidecar_ids, sqlite_ids)
    broken_wikilinks = _scan_broken_wikilinks(notes)
    supersedes_cycles = _scan_supersedes_cycles(notes, canonical_ids)
    result = ReliabilityScan(
        notes_count=len(notes),
        id_violations=tuple(id_violations),
        broken_wikilinks=tuple(broken_wikilinks),
        supersedes_cycles=tuple(supersedes_cycles),
        mixed_eol_notes=tuple(note.rel_path for note in notes if note.mixed_eol),
        content_hashes=tuple((note.rel_path, note.content_hash) for note in every_note),
        parse_errors=tuple(parse_errors),
    )
    _logger().info(
        "reliability scan complete "
        "(notes=%d excluded=%d ids=%d links=%d mixed_eol=%d cycles=%d parse_errors=%d)",
        result.notes_count,
        len(every_note) - len(notes),
        len(result.id_violations),
        len(result.broken_wikilinks),
        len(result.mixed_eol_notes),
        len(result.supersedes_cycles),
        len(result.parse_errors),
    )
    return result


def compare_with_baseline(
    scan: ReliabilityScan,
    accepted_fingerprints: Mapping[str, Sequence[str]],
) -> BaselineComparison:
    """Fail only violations whose stable fingerprint is outside the allowlist."""
    allowed = {
        fingerprint
        for fingerprints in accepted_fingerprints.values()
        for fingerprint in fingerprints
    }
    current = {violation.fingerprint: violation for violation in scan.violations}
    new = tuple(current[key] for key in sorted(current.keys() - allowed))
    accepted = tuple(current[key] for key in sorted(current.keys() & allowed))
    stale = tuple(sorted(allowed - current.keys()))
    return BaselineComparison(
        new_violations=new,
        accepted_violations=accepted,
        stale_fingerprints=stale,
    )


def _admission_policy(root: Path) -> NoteAdmissionPolicy:
    """Return the same admission policy the vault reader builds for ``root``.

    The ``or VaultConfig()`` fallback is the whole point and not a formality: with no
    ``.datacron/VAULT.yaml`` the reader still applies the default exclusions, so a
    scan that fell back to "exclude nothing" would disagree with the reader on
    exactly the vaults that never configured anything.
    """
    config = load_vault_config(sidecar_vault_config(root)) or VaultConfig()
    return NoteAdmissionPolicy(
        excluded_folders=SKIPPED_FOLDERS | frozenset(config.excluded_folders),
        excluded_files=frozenset(config.excluded_files),
    )


def _is_admitted(rel_path: str, policy: NoteAdmissionPolicy) -> bool:
    """Report whether the served vault would hand this path to a reader."""
    parts = rel_path.split("/")
    if any(
        part.casefold() in policy.excluded_folders or part.startswith(".") for part in parts[:-1]
    ):
        return False
    return parts[-1].casefold() not in policy.excluded_files


def _load_notes(root: Path) -> tuple[list[ReliabilityNote], list[str]]:
    notes: list[ReliabilityNote] = []
    parse_errors: list[str] = []
    for path in _iter_markdown(root):
        rel_path = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            parse_errors.append(f"{rel_path}: {type(exc).__name__}")
            continue
        try:
            metadata, body = parse(text)
        except FrontmatterError as exc:
            parse_errors.append(f"{rel_path}: {type(exc).__name__}")
            metadata, body = {}, text
        notes.append(
            ReliabilityNote(
                rel_path=rel_path,
                metadata=metadata,
                body=body,
                title=resolve_note_title(metadata, body, path, h1_pattern=_H1_PATTERN),
                aliases=tuple(coerce_string_list(metadata.get("aliases"))),
                mixed_eol=_has_mixed_eol(raw),
                content_hash=hashlib.sha256(raw).hexdigest(),
            )
        )
    return notes, parse_errors


def _has_mixed_eol(raw: bytes) -> bool:
    has_crlf = b"\r\n" in raw
    without_crlf = raw.replace(b"\r\n", b"")
    has_lf = b"\n" in without_crlf
    has_cr = b"\r" in without_crlf
    return sum((has_crlf, has_lf, has_cr)) > 1


def _iter_markdown(root: Path) -> list[Path]:
    paths: list[Path] = []
    for current_raw, dirnames, filenames in os.walk(root):
        current = Path(current_raw)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
        ]
        paths.extend(
            current / filename for filename in sorted(filenames) if filename.lower().endswith(".md")
        )
    return paths


def _load_sidecar_ids(root: Path) -> dict[str, str]:
    merged: dict[str, str] = {}
    for filename in ("ulids.json.migrated", "ulids.json"):
        path = root / ".datacron" / filename
        if not path.is_file():
            continue
        try:
            payload = read_ulid_mappings(
                path,
                require_string_pairs=True,
                invalid_object_is_empty=True,
            )
        except (OSError, json.JSONDecodeError) as exc:
            _logger().warning("cannot read ID sidecar %s: %s", path, exc)
            continue
        merged.update(payload)
    return merged


def _load_sqlite_ids(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for db_path in sorted((root / ".datacron").glob("**/*.db")):
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            _logger().warning("cannot open index %s read-only: %s", db_path, exc)
            continue
        try:
            connection.execute("PRAGMA query_only = ON;")
            queries = (
                ("ulid_paths", "SELECT rel_path, note_id FROM ulid_paths"),
                ("notes", "SELECT rel_path, note_id FROM notes"),
            )
            for table, query in queries:
                if not _table_exists(connection, table):
                    continue
                rows = connection.execute(query).fetchall()
                result.update({str(rel_path): str(note_id) for rel_path, note_id in rows})
        except sqlite3.Error as exc:
            _logger().warning("cannot query index %s: %s", db_path, exc)
        finally:
            connection.close()
    return result


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _scan_id_coherence(
    notes: Sequence[ReliabilityNote],
    sidecar_ids: Mapping[str, str],
    sqlite_ids: Mapping[str, str],
) -> list[ReliabilityViolation]:
    sources_by_path: dict[str, dict[str, str]] = {}
    paths_by_id: dict[str, set[str]] = defaultdict(set)
    for note in notes:
        sources = {
            key: value
            for key, value in {
                "frontmatter": note.frontmatter_id,
                "sidecar": sidecar_ids.get(note.rel_path),
                "sqlite": sqlite_ids.get(note.rel_path),
            }.items()
            if value is not None
        }
        sources_by_path[note.rel_path] = sources
        for note_id in set(sources.values()):
            paths_by_id[note_id].add(note.rel_path)

    duplicate_paths = {path for paths in paths_by_id.values() if len(paths) > 1 for path in paths}
    violations: list[ReliabilityViolation] = []
    for note in notes:
        sources = sources_by_path[note.rel_path]
        mismatch = len(set(sources.values())) > 1
        if not mismatch and note.rel_path not in duplicate_paths:
            continue
        details = tuple(sorted(sources.items()))
        violations.append(
            ReliabilityViolation(
                kind="id_coherence",
                key=f"id_coherence:{note.rel_path}",
                rel_path=note.rel_path,
                classification="mismatch" if mismatch else "duplicate",
                details=details,
            )
        )
    return violations


def _scan_broken_wikilinks(notes: Sequence[ReliabilityNote]) -> list[ReliabilityViolation]:
    aliases = _build_alias_index(notes)
    aggressive_aliases = _build_aggressive_alias_index(notes)
    chunker = MarkdownChunker()
    violations: list[ReliabilityViolation] = []
    occurrences: dict[tuple[str, str], int] = defaultdict(int)
    for note in notes:
        indexed_note = Note(
            id=_SCANNER_NOTE_ID,
            path=Path(note.rel_path),
            rel_path=note.rel_path,
            title=note.title,
            frontmatter=note.metadata,
            content=note.body,
            raw_content=note.body,
            created=_SCANNER_TIMESTAMP,
            updated=_SCANNER_TIMESTAMP,
            content_hash=hash_text(note.body),
            tags=[],
            aliases=list(note.aliases),
        )
        for chunk in chunker.chunk(indexed_note):
            for target in chunk.wikilinks_out:
                _append_broken_wikilink(
                    violations,
                    occurrences,
                    aliases,
                    aggressive_aliases,
                    note.rel_path,
                    target,
                )
    return violations


def _append_broken_wikilink(
    violations: list[ReliabilityViolation],
    occurrences: dict[tuple[str, str], int],
    aliases: Mapping[str, str | None],
    aggressive_aliases: Mapping[str, set[str]],
    rel_path: str,
    target: str,
) -> None:
    normalized = _normalize_alias(target)
    if normalized in aliases and aliases[normalized] is not None:
        return
    occurrence_key = (rel_path, target)
    occurrences[occurrence_key] += 1
    ordinal = occurrences[occurrence_key]
    aggressive_matches = aggressive_aliases.get(_aggressive_alias(target), set())
    classification = (
        WIKILINK_EXISTING_UNDER_OTHER_TITLE_OR_ALIAS if aggressive_matches else WIKILINK_NONEXISTENT
    )
    details = (
        (("candidate_paths", ";".join(sorted(aggressive_matches))),) if aggressive_matches else ()
    )
    violations.append(
        ReliabilityViolation(
            kind="broken_wikilink",
            key=f"broken_wikilink:{rel_path}:{target}:occurrence-{ordinal}",
            rel_path=rel_path,
            target=target,
            classification=classification,
            details=details,
        )
    )


def _build_alias_index(notes: Sequence[ReliabilityNote]) -> dict[str, str | None]:
    return build_tiered_alias_index(
        notes,
        identity=lambda note: note.rel_path,
        title=lambda note: (note.title,),
        stem=lambda note: (Path(note.rel_path).stem,),
        aliases=lambda note: note.aliases,
        normalize=_normalize_alias,
    )


def _build_aggressive_alias_index(
    notes: Sequence[ReliabilityNote],
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for note in notes:
        for alias in (note.title, Path(note.rel_path).stem, *note.aliases):
            key = _aggressive_alias(alias)
            if key:
                index[key].add(note.rel_path)
    return index


def _scan_supersedes_cycles(
    notes: Sequence[ReliabilityNote],
    canonical_ids: Mapping[str, str | None],
) -> list[ReliabilityViolation]:
    graph: dict[str, list[str]] = defaultdict(list)
    path_by_id: dict[str, str] = {}
    frontmatter_to_canonical: dict[str, str] = {}
    for note in notes:
        source = canonical_ids.get(note.rel_path) or note.frontmatter_id or note.rel_path
        path_by_id[source] = note.rel_path
        if note.frontmatter_id:
            frontmatter_to_canonical[note.frontmatter_id] = source
        graph[source].extend(note.supersedes)

    normalized_graph = {
        source: [frontmatter_to_canonical.get(target, target) for target in targets]
        for source, targets in graph.items()
    }
    violations: list[ReliabilityViolation] = []
    for cycle in _find_cycles(normalized_graph):
        canonical_cycle = _canonical_cycle(cycle)
        source = canonical_cycle[0]
        violations.append(
            ReliabilityViolation(
                kind="supersedes_cycle",
                key=f"supersedes_cycle:{'->'.join(canonical_cycle)}",
                rel_path=path_by_id.get(source, source),
                target=" -> ".join(canonical_cycle),
                classification="cycle",
            )
        )
    return violations


def _find_cycles(graph: Mapping[str, Sequence[str]]) -> list[tuple[str, ...]]:
    cycles: set[tuple[str, ...]] = set()
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            start = stack.index(node)
            cycles.add(_canonical_cycle((*stack[start:], node)))
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for target in graph.get(node, ()):
            visit(target)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)
    return sorted(cycles)


def _canonical_cycle(cycle: Iterable[str]) -> tuple[str, ...]:
    nodes = list(cycle)
    if len(nodes) > 1 and nodes[0] == nodes[-1]:
        nodes.pop()
    if not nodes:
        return ()
    rotations = [tuple(nodes[index:] + nodes[:index]) for index in range(len(nodes))]
    selected = min(rotations)
    return (*selected, selected[0])


def _normalize_alias(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip().lower()


def _aggressive_alias(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in decomposed if char.isalnum())
