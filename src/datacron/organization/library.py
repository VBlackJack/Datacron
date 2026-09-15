# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Read-only inventory, Markdown navigation and conservative readability checks."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from mistletoe import block_token

from datacron.core.config import DEFAULT_ARCHIVE_TAGS, load_vault_config
from datacron.core.frontmatter import coerce_string_list, parse
from datacron.core.markdown_headings import markdown_headings, token_text
from datacron.core.models import ChunkType, Note
from datacron.core.paths import sidecar_vault_config
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault import build_configured_reader
from datacron.indexing.wikilinks import extract_wikilink_anchors
from datacron.organization.library_models import Finding, LibraryAudit, LibraryOptions
from datacron.organization.library_text import TEXT
from datacron.organization.manifest import normalize_vault_rel_path

_TASK = re.compile(r"^\[ \]\s+(.+)", re.DOTALL)
_ANCHOR_SEPARATOR = "#"
_NOTE_SUFFIX = ".md"

# One parsed (links, tasks) pair per note, keyed by content hash so an audit and the
# navigation of the same note set parse each body once.
ParsedLinks = dict[str, tuple[list[tuple[str, bool]], list[str]]]


def in_scope(path: str, scope: str) -> bool:
    """Match a complete folder boundary, never a textual prefix."""
    return path == scope or path.startswith(scope.rstrip("/") + "/")


async def read_library(vault: Path, options: LibraryOptions) -> list[Note]:
    """Read admitted notes strictly without writing IDs, an index or a cache."""
    normalize_vault_rel_path(options.scope + "/scope.md")
    normalize_vault_rel_path(options.home)
    assert_path_chain_without_links(vault.expanduser().absolute())
    vault = vault.resolve(strict=True)
    reader = build_configured_reader(vault, read_only=True)
    paths = await reader.stat_notes()
    selected = sorted(path for path in paths if in_scope(path, options.scope))
    if len(selected) > options.max_notes:
        raise ValueError("Library scope exceeds max_notes; select a smaller scope")
    notes = []
    identities: dict[str, str] = {}
    for rel_path in selected:
        path = paths[rel_path][0]
        assert_path_chain_without_links(path, anchor=vault)
        if path.stat().st_size > options.max_note_bytes:
            raise ValueError(f"Note exceeds max_note_bytes: {rel_path}")
        note = await reader.read_note(path)
        if len(note.raw_content.encode("utf-8")) > options.max_note_bytes:
            raise ValueError(f"Note grew beyond max_note_bytes: {rel_path}")
        parse(note.raw_content)  # Reject malformed frontmatter instead of indexing a fallback.
        previous = identities.setdefault(note.id, rel_path)
        if previous != rel_path:
            raise ValueError(f"Duplicate note identity: {previous}, {rel_path}")
        notes.append(note)
    return notes


def _walk(token: Any) -> list[Any]:
    if type(token).__name__ in {"CodeFence", "BlockCode", "InlineCode"}:
        return []
    return [
        token,
        *(child for item in getattr(token, "children", None) or [] for child in _walk(item)),
    ]


def links_and_tasks(body: str) -> tuple[list[tuple[str, bool]], list[str]]:
    """Extract parsed links and checkboxes outside fenced, indented and inline code.

    Wikilinks come from the one canonical parser in ``indexing.wikilinks``, applied
    to each raw text run in document order; a header anchor stays attached to its
    target so link resolution can verify it.
    """
    links: list[tuple[str, bool]] = []
    tasks: list[str] = []
    for token in _walk(block_token.Document(body)):
        name = type(token).__name__
        if name in {"Link", "Image"}:
            links.append((str(token.src if name == "Image" else token.target), False))
        elif name == "RawText":
            links.extend(
                (f"{target}{_ANCHOR_SEPARATOR}{header}" if header else target, True)
                for target, header in extract_wikilink_anchors(token.content, ChunkType.NARRATIVE)
            )
        elif name == "ListItem":
            children = getattr(token, "children", None) or []
            if children:
                match = _TASK.match(token_text(children[0]))
                if match:
                    tasks.append(match.group(1).strip())
    return links, tasks


def parse_links_and_tasks(notes: Iterable[Note], cache: ParsedLinks | None = None) -> ParsedLinks:
    """Parse every note body once; a caller's cache is reused for bodies it already holds."""
    parsed: ParsedLinks = dict(cache or {})
    for note in notes:
        if note.content_hash not in parsed:
            parsed[note.content_hash] = links_and_tasks(note.content)
    return parsed


@dataclass(frozen=True)
class LinkIndex:
    """Casefolded lookup tables over one note set, built once and shared by every link.

    Each table maps a casefolded key to the matching note paths in note order, so a
    lookup keeps the same candidates, in the same order, as a scan of the notes.
    """

    paths: dict[str, list[str]]
    titles: dict[str, list[str]]
    stems: dict[str, list[str]]
    aliases: dict[str, list[str]]
    rel_paths: dict[str, list[str]]
    notes: dict[str, Note]
    _anchors: dict[str, set[str]] = field(default_factory=dict)

    def anchors(self, rel_path: str) -> set[str]:
        """Return the casefolded heading anchors of a note, computed on first use."""
        cached = self._anchors.get(rel_path)
        if cached is None:
            cached = _heading_anchors(self.notes[rel_path])
            self._anchors[rel_path] = cached
        return cached


def build_link_index(notes: list[Note]) -> LinkIndex:
    """Index explicit paths, titles, stems and aliases of ``notes`` for link resolution."""
    paths: dict[str, list[str]] = defaultdict(list)
    titles: dict[str, list[str]] = defaultdict(list)
    stems: dict[str, list[str]] = defaultdict(list)
    aliases: dict[str, list[str]] = defaultdict(list)
    rel_paths: dict[str, list[str]] = defaultdict(list)
    for note in notes:
        paths[note.rel_path.casefold().removesuffix(_NOTE_SUFFIX)].append(note.rel_path)
        titles[note.title.casefold()].append(note.rel_path)
        stems[PurePosixPath(note.rel_path).stem.casefold()].append(note.rel_path)
        for alias in {alias.casefold() for alias in note.aliases}:
            aliases[alias].append(note.rel_path)
        rel_paths[note.rel_path.casefold()].append(note.rel_path)
    return LinkIndex(
        paths=dict(paths),
        titles=dict(titles),
        stems=dict(stems),
        aliases=dict(aliases),
        rel_paths=dict(rel_paths),
        notes={note.rel_path: note for note in notes},
    )


def _heading_anchors(note: Note) -> set[str]:
    headings = markdown_headings(note.content.splitlines(keepends=True))
    anchors = {heading.text.casefold() for heading in headings}
    anchors.update(
        re.sub(r"[^\w\- ]", "", heading.text.casefold()).replace(" ", "-") for heading in headings
    )
    return anchors


def _wiki_candidates(index: LinkIndex, key: str) -> list[str]:
    """Prefer explicit paths, then title, stem and aliases, in separate tiers."""
    for table in (index.paths, index.titles, index.stems, index.aliases):
        candidates = table.get(key)
        if candidates:
            return candidates
    return []


def resolve_link(source: str, target: str, wiki: bool, index: LinkIndex) -> tuple[str, str]:
    """Resolve scoped note links; return external, missing or ambiguous explicitly."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return "external", target
    path = unquote(parsed.path)
    if not path:
        candidates = [source]
    elif wiki:
        candidates = _wiki_candidates(index, path.casefold().removesuffix(_NOTE_SUFFIX))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        candidates = index.rel_paths.get(resolved.casefold(), [])
        if not candidates:
            return "local_unresolved", resolved
    if len(candidates) != 1:
        return ("ambiguous" if candidates else "local_unresolved"), path
    selected = candidates[0]
    fragment = unquote(parsed.fragment)
    if fragment and fragment.casefold() not in index.anchors(selected):
        return "anchor_unverified", selected + _ANCHOR_SEPARATOR + fragment
    return "note", selected


def vault_archive_tags(vault: Path) -> frozenset[str]:
    """Return the archive tags the vault's tag policy declares, else the shared defaults."""
    config = load_vault_config(sidecar_vault_config(vault))
    if config is None:
        return frozenset(tag.casefold() for tag in DEFAULT_ARCHIVE_TAGS)
    return config.archive_tags


def lifecycle(
    note: Note,
    notes: list[Note],
    superseded: set[str] | None = None,
    *,
    archive_tags: Iterable[str] = DEFAULT_ARCHIVE_TAGS,
) -> str:
    """Use explicit lifecycle evidence only; missing evidence never proves validity."""
    if superseded is None:
        superseded = {i for n in notes for i in coerce_string_list(n.frontmatter.get("supersedes"))}
    meta = note.frontmatter
    if note.id in superseded or meta.get("invalid_at") or meta.get("archived") is True:
        return "historical"
    if str(meta.get("status", "")).casefold() in {"archived", "historical", "superseded"}:
        return "historical"
    archived_tags = {tag.casefold() for tag in archive_tags}
    if archived_tags.intersection(tag.casefold() for tag in note.tags):
        return "historical"
    if str(meta.get("confidence", "")).casefold() == "low" or not meta.get("last_verified"):
        return "review"
    return "active"


def audit_library(
    notes: list[Note],
    options: LibraryOptions,
    *,
    parsed_links: ParsedLinks | None = None,
) -> LibraryAudit:
    """Measure size, navigation, duplicate candidates and unresolved references."""
    parsed = parse_links_and_tasks(notes, parsed_links)
    index = build_link_index(notes)
    findings: list[Finding] = []
    titles: dict[str, list[str]] = defaultdict(list)
    bodies: dict[str, list[str]] = defaultdict(list)
    folders: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        titles[note.title.casefold()].append(note.rel_path)
        bodies[hashlib.sha256(note.content.encode("utf-8")).hexdigest()].append(note.rel_path)
        folders[str(PurePosixPath(note.rel_path).parent)].append(note)
        findings.extend(_size_findings(note, options))
        findings.extend(_link_findings(note, parsed[note.content_hash][0], index))
    findings.extend(_duplicate_findings(titles, bodies))
    findings.extend(_folder_findings(folders, options))
    return LibraryAudit(
        scope=options.scope,
        notes=len(notes),
        source_hashes={n.rel_path: n.content_hash for n in notes},
        findings=findings,
    )


def _size_findings(note: Note, options: LibraryOptions) -> list[Finding]:
    findings: list[Finding] = []
    if len(note.content) > options.max_note_chars:
        findings.append(
            Finding(code="LONG_NOTE", path=note.rel_path, detail=str(len(note.content)))
        )
    lines = note.content.splitlines(keepends=True)
    headings = markdown_headings(lines)
    for i, heading in enumerate(headings):
        end = next((h.start for h in headings[i + 1 :] if h.level <= heading.level), len(lines))
        size = len("".join(lines[heading.end : end]))
        if heading.level > 1 and size > options.max_section_chars:
            findings.append(Finding(code="LONG_SECTION", path=note.rel_path, detail=heading.text))
    return findings


def _link_findings(note: Note, links: list[tuple[str, bool]], index: LinkIndex) -> list[Finding]:
    findings: list[Finding] = []
    for target, wiki in links:
        status, resolved = resolve_link(note.rel_path, target, wiki, index)
        if status != "note":
            findings.append(
                Finding(
                    code=status.upper(),
                    path=note.rel_path,
                    detail=resolved,
                    link_style="wiki" if wiki else "markdown",
                )
            )
    return findings


def _duplicate_findings(
    titles: dict[str, list[str]], bodies: dict[str, list[str]]
) -> list[Finding]:
    findings: list[Finding] = []
    for code, groups in (
        ("DUPLICATE_TITLE_CANDIDATE", titles),
        ("IDENTICAL_BODY_CANDIDATE", bodies),
    ):
        for paths in groups.values():
            if len(paths) > 1:
                findings.append(Finding(code=code, path=paths[0], detail="; ".join(paths[1:])))
    return findings


def _folder_findings(folders: dict[str, list[Note]], options: LibraryOptions) -> list[Finding]:
    return [
        Finding(code="NO_SUBJECT_STATE_CANDIDATE", path=folder, detail=str(len(members)))
        for folder, members in folders.items()
        if not any(set(options.state_tags).intersection(n.tags) for n in members)
    ]


def markdown_link(source: str, target: str, label: str) -> str:
    """Build a portable relative link with escaped display text and encoded path."""
    escaped = re.sub(r"([\\`*{}\[\]<>!|])", r"\\\1", " ".join(label.splitlines()))
    relative = posixpath.relpath(target, posixpath.dirname(source) or ".")
    return f"[{escaped}]({quote(relative, safe='/')})"


def navigation(
    notes: list[Note],
    options: LibraryOptions,
    captured: str,
    *,
    archive_tags: Iterable[str] = DEFAULT_ARCHIVE_TAGS,
    parsed_links: ParsedLinks | None = None,
) -> dict[str, str]:
    """Render a home and folder maps as ordinary Markdown, with sourced task links."""
    text = TEXT[options.language]
    parsed = parse_links_and_tasks(notes, parsed_links)
    states = _lifecycle_of(notes, archive_tags)
    folders: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        if not note.frontmatter.get("library_generated"):
            folders[str(PurePosixPath(note.rel_path).parent)].append(note)
    pages, area_links = _folder_pages(folders, options, states, text)
    home = [
        f"# {text['home']}",
        text["snapshot"].format(date=captured, scope=options.scope),
        text["notice"],
        text["offline"],
        f"## {text['subjects']}",
    ]
    for area, entries in area_links.items():
        if area:
            home.append(f"### {area}")
        home.extend(entries)
    home.extend(_home_categories(notes, options, states, parsed, text))
    pages[options.home] = "\n\n".join(home) + "\n"
    return pages


def _lifecycle_of(notes: list[Note], archive_tags: Iterable[str]) -> dict[str, str]:
    """Return the lifecycle state of every note, keyed by path."""
    superseded = {i for n in notes for i in coerce_string_list(n.frontmatter.get("supersedes"))}
    archived = frozenset(tag.casefold() for tag in archive_tags)
    return {n.rel_path: lifecycle(n, notes, superseded, archive_tags=archived) for n in notes}


def _folder_pages(
    folders: dict[str, list[Note]],
    options: LibraryOptions,
    states: dict[str, str],
    text: dict[str, str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Render one navigation page per folder and the home links that point at them."""
    parent = PurePosixPath(options.home).parent
    pages: dict[str, str] = {}
    area_links: dict[str, list[str]] = defaultdict(list)
    for folder, members in sorted(folders.items()):
        relative = posixpath.relpath(folder, options.scope)
        readable = PurePosixPath(folder).name if relative == "." else relative
        key = re.sub(r"[^\w-]+", "-", readable.casefold()).strip("-_")
        if not key:
            raise ValueError(f"Folder has no readable navigation filename: {folder}")
        target = (parent / f"navigation-{key}.md").as_posix()
        if target.casefold() in {p.casefold() for p in pages}:
            raise ValueError(f"Navigation filenames collide; narrow the scope: {folder}")
        label = next(
            (n.title for n in members if set(options.state_tags).intersection(n.tags)),
            PurePosixPath(folder).name.replace("-", " ").capitalize(),
        )
        label = options.folder_labels.get(folder, label)
        area = next(
            (name for name, prefix in options.areas.items() if in_scope(folder, prefix)), ""
        )
        area_links[area].append("- " + markdown_link(options.home, target, label))
        pages[target] = _folder_page(target, label, members, options, states, text)
    return pages, area_links


def _folder_page(
    target: str,
    label: str,
    members: list[Note],
    options: LibraryOptions,
    states: dict[str, str],
    text: dict[str, str],
) -> str:
    body = [
        f"# {text['folder_title'].format(subject=label)}",
        markdown_link(target, options.home, text["home"]),
        text["notice"],
    ]
    for state in ("active", "review", "historical"):
        body.append(f"## {text[state]}")
        items = [n for n in members if states[n.rel_path] == state]
        body.extend(
            ["- " + markdown_link(target, n.rel_path, n.title) for n in items] or [text["empty"]]
        )
    return "\n\n".join(body) + "\n"


def _home_categories(
    notes: list[Note],
    options: LibraryOptions,
    states: dict[str, str],
    parsed: ParsedLinks,
    text: dict[str, str],
) -> list[str]:
    """Render the people, procedures, tasks and historical sections of the home page."""
    lines: list[str] = []
    for category in ("people", "procedures", "tasks", "historical"):
        lines.append(f"## {text[category]}")
        category_items: list[str] = []
        for note in notes:
            if note.frontmatter.get("library_generated"):
                continue
            tasks = parsed[note.content_hash][1]
            if _in_category(category, note, options, states, tasks):
                label = note.title + (f" ({len(tasks)})" if category == "tasks" else "")
                category_items.append("- " + markdown_link(options.home, note.rel_path, label))
        lines.extend(category_items or [text["empty"]])
    return lines


def _in_category(
    category: str,
    note: Note,
    options: LibraryOptions,
    states: dict[str, str],
    tasks: list[str],
) -> bool:
    if category == "people":
        return bool(set(options.people_tags).intersection(note.tags))
    if category == "procedures":
        return bool(set(options.procedure_tags).intersection(note.tags))
    if category == "historical":
        return states[note.rel_path] == "historical"
    return bool(tasks)
