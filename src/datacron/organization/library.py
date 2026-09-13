# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Read-only inventory, Markdown navigation and conservative readability checks."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from mistletoe import block_token

from datacron.core.frontmatter import coerce_string_list, parse
from datacron.core.markdown_headings import markdown_headings, token_text
from datacron.core.models import Note
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault import build_configured_reader
from datacron.organization.library_models import Finding, LibraryAudit, LibraryOptions
from datacron.organization.library_text import TEXT
from datacron.organization.manifest import normalize_vault_rel_path

_WIKI = re.compile(r"\[\[([^\[\]]+)\]\]")
_TASK = re.compile(r"^\[ \]\s+(.+)", re.DOTALL)


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
    """Extract parsed links and checkboxes outside fenced, indented and inline code."""
    links: list[tuple[str, bool]] = []
    tasks: list[str] = []
    for token in _walk(block_token.Document(body)):
        name = type(token).__name__
        if name in {"Link", "Image"}:
            links.append((str(token.src if name == "Image" else token.target), False))
        elif name == "RawText":
            links.extend(
                (match.group(1).split("|", 1)[0], True) for match in _WIKI.finditer(token.content)
            )
        elif name == "ListItem":
            children = getattr(token, "children", None) or []
            if children:
                match = _TASK.match(token_text(children[0]))
                if match:
                    tasks.append(match.group(1).strip())
    return links, tasks


def resolve_link(source: str, target: str, wiki: bool, notes: list[Note]) -> tuple[str, str]:
    """Resolve scoped note links; return external, missing or ambiguous explicitly."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return "external", target
    path = unquote(parsed.path)
    if not path:
        candidates = [source]
    elif wiki:
        key = path.casefold().removesuffix(".md")
        # Prefer explicit paths, then title, stem and aliases in separate tiers.
        tiers = [
            [n.rel_path for n in notes if n.rel_path.casefold().removesuffix(".md") == key],
            [n.rel_path for n in notes if n.title.casefold() == key],
            [n.rel_path for n in notes if PurePosixPath(n.rel_path).stem.casefold() == key],
            [n.rel_path for n in notes if key in {a.casefold() for a in n.aliases}],
        ]
        candidates = next((tier for tier in tiers if tier), [])
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        candidates = [n.rel_path for n in notes if n.rel_path.casefold() == resolved.casefold()]
        if not candidates:
            return "local_unresolved", resolved
    if len(candidates) != 1:
        return ("ambiguous" if candidates else "local_unresolved"), path
    selected = candidates[0]
    if parsed.fragment:
        note = next(n for n in notes if n.rel_path == selected)
        headings = markdown_headings(note.content.splitlines(keepends=True))
        fragment = unquote(parsed.fragment).casefold()
        anchors = {h.text.casefold() for h in headings}
        anchors.update(
            re.sub(r"[^\w\- ]", "", h.text.casefold()).replace(" ", "-") for h in headings
        )
        if fragment not in anchors:
            return "anchor_unverified", selected + "#" + unquote(parsed.fragment)
    return "note", selected


def lifecycle(note: Note, notes: list[Note], superseded: set[str] | None = None) -> str:
    """Use explicit lifecycle evidence only; missing evidence never proves validity."""
    if superseded is None:
        superseded = {i for n in notes for i in coerce_string_list(n.frontmatter.get("supersedes"))}
    meta = note.frontmatter
    if note.id in superseded or meta.get("invalid_at") or meta.get("archived") is True:
        return "historical"
    if str(meta.get("status", "")).casefold() in {"archived", "historical", "superseded"}:
        return "historical"
    if {"meta/archive", "memory/archive"}.intersection(note.tags):
        return "historical"
    if str(meta.get("confidence", "")).casefold() == "low" or not meta.get("last_verified"):
        return "review"
    return "active"


def audit_library(notes: list[Note], options: LibraryOptions) -> LibraryAudit:
    """Measure size, navigation, duplicate candidates and unresolved references."""
    findings: list[Finding] = []
    titles: dict[str, list[str]] = defaultdict(list)
    bodies: dict[str, list[str]] = defaultdict(list)
    folders: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        titles[note.title.casefold()].append(note.rel_path)
        bodies[hashlib.sha256(note.content.encode("utf-8")).hexdigest()].append(note.rel_path)
        folders[str(PurePosixPath(note.rel_path).parent)].append(note)
        if len(note.content) > options.max_note_chars:
            findings.append(
                Finding(code="LONG_NOTE", path=note.rel_path, detail=str(len(note.content)))
            )
        headings = markdown_headings(note.content.splitlines(keepends=True))
        lines = note.content.splitlines(keepends=True)
        for i, heading in enumerate(headings):
            end = next((h.start for h in headings[i + 1 :] if h.level <= heading.level), len(lines))
            size = len("".join(lines[heading.end : end]))
            if heading.level > 1 and size > options.max_section_chars:
                findings.append(
                    Finding(code="LONG_SECTION", path=note.rel_path, detail=heading.text)
                )
        for target, wiki in links_and_tasks(note.content)[0]:
            status, resolved = resolve_link(note.rel_path, target, wiki, notes)
            if status != "note":
                findings.append(
                    Finding(
                        code=status.upper(),
                        path=note.rel_path,
                        detail=resolved,
                        link_style="wiki" if wiki else "markdown",
                    )
                )
    for code, groups in (
        ("DUPLICATE_TITLE_CANDIDATE", titles),
        ("IDENTICAL_BODY_CANDIDATE", bodies),
    ):
        for paths in groups.values():
            if len(paths) > 1:
                findings.append(Finding(code=code, path=paths[0], detail="; ".join(paths[1:])))
    for folder, members in folders.items():
        if not any(set(options.state_tags).intersection(n.tags) for n in members):
            findings.append(
                Finding(code="NO_SUBJECT_STATE_CANDIDATE", path=folder, detail=str(len(members)))
            )
    return LibraryAudit(
        scope=options.scope,
        notes=len(notes),
        source_hashes={n.rel_path: n.content_hash for n in notes},
        findings=findings,
    )


def markdown_link(source: str, target: str, label: str) -> str:
    """Build a portable relative link with escaped display text and encoded path."""
    escaped = re.sub(r"([\\`*{}\[\]<>!|])", r"\\\1", " ".join(label.splitlines()))
    relative = posixpath.relpath(target, posixpath.dirname(source) or ".")
    return f"[{escaped}]({quote(relative, safe='/')})"


def navigation(notes: list[Note], options: LibraryOptions, captured: str) -> dict[str, str]:
    """Render a home and folder maps as ordinary Markdown, with sourced task links."""
    text = TEXT[options.language]
    parent = PurePosixPath(options.home).parent
    folders: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        if not note.frontmatter.get("library_generated"):
            folders[str(PurePosixPath(note.rel_path).parent)].append(note)
    pages: dict[str, str] = {}
    superseded = {i for n in notes for i in coerce_string_list(n.frontmatter.get("supersedes"))}
    states = {n.rel_path: lifecycle(n, notes, superseded) for n in notes}
    area_links: dict[str, list[str]] = defaultdict(list)
    home = [
        f"# {text['home']}",
        text["snapshot"].format(date=captured, scope=options.scope),
        text["notice"],
        text["offline"],
        f"## {text['subjects']}",
    ]
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
        body = [
            f"# {text['folder_title'].format(subject=label)}",
            markdown_link(target, options.home, text["home"]),
            text["notice"],
        ]
        for state in ("active", "review", "historical"):
            body.append(f"## {text[state]}")
            items = [n for n in members if states[n.rel_path] == state]
            body.extend(
                ["- " + markdown_link(target, n.rel_path, n.title) for n in items]
                or [text["empty"]]
            )
        pages[target] = "\n\n".join(body) + "\n"
    for area, entries in area_links.items():
        if area:
            home.append(f"### {area}")
        home.extend(entries)
    for category in ("people", "procedures", "tasks", "historical"):
        home.append(f"## {text[category]}")
        category_items: list[str] = []
        for note in notes:
            if note.frontmatter.get("library_generated"):
                continue
            tasks = links_and_tasks(note.content)[1]
            matched = (
                (category == "people" and bool(set(options.people_tags).intersection(note.tags)))
                or (
                    category == "procedures"
                    and bool(set(options.procedure_tags).intersection(note.tags))
                )
                or (category == "historical" and states[note.rel_path] == "historical")
                or (category == "tasks" and bool(tasks))
            )
            if matched:
                label = note.title + (f" ({len(tasks)})" if category == "tasks" else "")
                category_items.append("- " + markdown_link(options.home, note.rel_path, label))
        home.extend(category_items or [text["empty"]])
    pages[options.home] = "\n\n".join(home) + "\n"
    return pages
