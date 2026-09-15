# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Prepare external review bundles and offline previews without mutating a vault."""

from __future__ import annotations

import asyncio
import difflib
import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ulid import ULID

from datacron.core.config import Settings
from datacron.core.frontmatter import (
    parse,
    parse_preserving_bom_and_body_eols,
    resolve_note_title,
    serialize,
    serialize_preserving_bom,
)
from datacron.core.markdown_headings import markdown_headings
from datacron.core.models import Note
from datacron.core.scope import SingleTenantVaultScope, assert_path_chain_without_links
from datacron.core.vault import H1_PATTERN
from datacron.organization.library import (
    audit_library,
    in_scope,
    links_and_tasks,
    markdown_link,
    navigation,
    read_library,
    vault_archive_tags,
)
from datacron.organization.library_models import (
    AUDIT_NAME,
    CHANGES_NAME,
    LIBRARY_SCHEMA,
    MANIFEST_NAME,
    PAYLOADS_DIRECTORY,
    PREVIEW_DIRECTORY,
    RECIPE_NAME,
    REPORT_NAME,
    SNAPSHOT_NAME,
    SUBJECT_TEMPLATE_NAME,
    EditorialNote,
    EditorialRecipe,
    LibraryAudit,
    LibraryOptions,
    SourceReference,
)
from datacron.organization.library_text import TEXT
from datacron.organization.manifest import (
    OrganizationManifest,
    load_and_validate_organization_bundle,
    normalize_vault_rel_path,
    sha256_bytes,
)


def _note_from_text(path: str, raw: str, vault: Path, captured: datetime) -> Note:
    meta, body = parse(raw)
    return Note(
        id=meta["id"],
        path=vault / path,
        rel_path=path,
        # The same rule as the vault reader: frontmatter title, first H1, then the stem.
        title=resolve_note_title(
            meta, body, Path(path), h1_pattern=H1_PATTERN, empty_h1_falls_back=True
        ),
        frontmatter=meta,
        content=body,
        raw_content=raw,
        created=captured,
        updated=captured,
        content_hash=sha256_bytes(raw.encode("utf-8")),
        tags=meta.get("tags", []),
        aliases=meta.get("aliases", []),
    )


def _generated_page(
    path: str, body: str, original: Note | None, options: LibraryOptions, captured: str
) -> str:
    if original:
        expected = original.frontmatter.get("library_body_sha256")
        actual = sha256_bytes(original.content.encode("utf-8"))
        if original.frontmatter.get("library_generated") is not True or expected != actual:
            raise ValueError(f"Refusing to replace a handwritten or modified page: {path}")
    title = body.splitlines()[0].removeprefix("# ")
    meta = {
        "id": original.id if original else str(ULID()),
        "title": title,
        "tags": options.tags,
        "created": original.frontmatter.get("created", captured) if original else captured,
        "updated": captured,
        "aliases": original.aliases if original else [],
        "library_generated": True,
        "library_body_sha256": sha256_bytes(body.strip().encode("utf-8")),
    }
    return serialize(meta, body)


def _editorial_changes(
    notes: list[Note], recipe: EditorialRecipe | None, options: LibraryOptions, captured: str
) -> dict[str, str]:
    if recipe is None:
        return {}
    lookup = {n.rel_path: n for n in notes}
    changes: dict[str, str] = {}
    archive_targets: dict[str, list[str]] = {}
    targets = [normalize_vault_rel_path(item.target) for item in recipe.notes]
    if len(set(targets)) != len(targets):
        raise ValueError("Duplicate editorial target")
    for item in recipe.notes:
        if links_and_tasks(item.body)[1]:
            raise ValueError(
                "Editorial notes must link to authoritative actions, not copy open checkboxes"
            )
        if not in_scope(item.target, options.scope) or item.target in lookup:
            raise ValueError(
                f"Editorial target must be new and inside the selected scope: {item.target}"
            )
        sources = {s.path: s.sha256 for s in item.sources}
        if len(sources) != len(item.sources) or not set(item.archive_sources).issubset(sources):
            raise ValueError("Archive sources must be unique exact source references")
        for path, digest in sources.items():
            if path not in lookup or lookup[path].content_hash != digest:
                raise ValueError(f"Editorial source changed or is outside scope: {path}")
            if lookup[path].frontmatter.get("id") != lookup[path].id:
                raise ValueError(f"Adopt the stable source identity before consolidation: {path}")
            # Bind even retained source bytes into the eventual transaction's CAS.
            changes.setdefault(path, lookup[path].raw_content)
        for path in item.archive_sources:
            if links_and_tasks(lookup[path].content)[1]:
                raise ValueError(f"Resolve open checkboxes before archiving: {path}")
            archive_targets.setdefault(path, []).append(item.target)
        meta: dict[str, Any] = {
            "id": str(ULID()),
            "title": item.title,
            "tags": item.tags,
            "created": captured,
            "updated": captured,
            "confidence": "low",
            "library_sources": [s.model_dump() for s in item.sources],
            "library_rationale": item.rationale,
        }
        refs = "\n".join(
            "- " + markdown_link(item.target, s.path, lookup[s.path].title) for s in item.sources
        )
        body = item.body.rstrip() + f"\n\n## {TEXT[options.language]['sources']}\n\n" + refs + "\n"
        changes[item.target] = serialize(meta, body)
    for path, destinations in archive_targets.items():
        meta, body, bom = parse_preserving_bom_and_body_eols(lookup[path].raw_content)
        meta.update(
            archived=True,
            updated=captured,
            library_successors=[
                f"[[{PurePosixPath(p).with_suffix('').as_posix()}]]" for p in destinations
            ],
        )
        changes[path] = serialize_preserving_bom(meta, body, has_bom=bom)
    return changes


def propose_split(source: Note, options: LibraryOptions) -> EditorialRecipe:
    """Draft exact H2 section notes, retaining the original and its incoming anchors."""
    if links_and_tasks(source.content)[1]:
        raise ValueError("Resolve open checkboxes or prepare a sourced editorial recipe manually")
    lines = source.content.splitlines(keepends=True)
    headings = [h for h in markdown_headings(lines) if h.level == 2]
    if len(headings) < 2:
        raise ValueError("Automatic split requires at least two H2 sections")
    entries = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start if index + 1 < len(headings) else len(lines)
        target = PurePosixPath(source.rel_path).with_name(
            f"{PurePosixPath(source.rel_path).stem}-section-{index + 1:03d}.md"
        )
        body = "".join(lines[heading.start : end])
        entries.append(
            EditorialNote(
                target=target.as_posix(),
                title=f"{source.title} / {heading.text}",
                body=f"# {source.title} / {heading.text}\n\n" + body,
                tags=[t for t in source.tags if t not in options.state_tags],
                sources=[SourceReference(path=source.rel_path, sha256=source.content_hash)],
                rationale=(
                    f"Exact section extraction, ordinal {index + 1}; original source retained"
                ),
            )
        )
    return EditorialRecipe(notes=entries)


def _manifest(changes: dict[str, str], notes: list[Note]) -> dict[str, Any]:
    lookup = {n.rel_path: n for n in notes}
    operations: list[dict[str, Any]] = []
    for path, raw in sorted(changes.items()):
        meta, _ = parse(raw)
        operation: dict[str, Any] = {
            "kind": "replace_exact" if path in lookup else "create_exact",
            "target": path,
            "payload_sha256": sha256_bytes(raw.encode("utf-8")),
            "result": {
                "id": meta.get("id", lookup[path].id if path in lookup else ""),
                "aliases": meta.get("aliases", []),
            },
        }
        if path in lookup:
            old = lookup[path]
            operation.update(
                expected_sha256=old.content_hash, expected={"id": old.id, "aliases": old.aliases}
            )
        operations.append(operation)
    document = {"schema": "organization-apply-v1", "operations": operations}
    OrganizationManifest.model_validate_json(json.dumps(document))
    return document


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _copy_attachments(
    vault: Path,
    output: Path,
    options: LibraryOptions,
    audit: LibraryAudit,
    scope: SingleTenantVaultScope,
    total: int,
) -> dict[str, str]:
    attachments: dict[str, str] = {}
    for finding in audit.findings:
        if finding.code != "LOCAL_UNRESOLVED":
            continue
        if finding.detail in attachments:
            continue
        relative = PurePosixPath(finding.detail)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() == ".md":
            continue
        if finding.link_style == "wiki" and not (vault / relative).exists():
            relative = PurePosixPath(finding.path).parent / relative
        attachment_path = vault / relative
        try:
            assert_path_chain_without_links(attachment_path, anchor=vault)
        except FileNotFoundError:
            continue
        # Explicit local references only; never follow links into excluded metadata.
        if any(
            part.casefold() in scope.admission_policy.excluded_folders for part in relative.parts
        ):
            continue
        if (
            relative.name.casefold() in scope.admission_policy.excluded_files
            or not attachment_path.is_file()
        ):
            continue
        if attachment_path.stat().st_size > options.max_attachment_bytes:
            continue
        data = attachment_path.read_bytes()
        if len(data) > options.max_attachment_bytes:
            raise ValueError(f"Attachment grew beyond max_attachment_bytes: {finding.detail}")
        total += len(data)
        if total > options.max_export_bytes:
            raise ValueError("Attachments exceed max_export_bytes")
        if relative.as_posix() not in attachments:
            _write_new(output / PREVIEW_DIRECTORY / relative, data)
            attachments[relative.as_posix()] = sha256_bytes(data)
    return attachments


def _write_workbench(
    vault: Path,
    output: Path,
    options: LibraryOptions,
    notes: list[Note],
    changes: dict[str, str],
    recipe: EditorialRecipe | None,
    settings: Settings,
) -> dict[str, object]:
    lexical = output.expanduser().absolute()
    assert_path_chain_without_links(lexical, allow_missing=True)
    output = lexical.resolve()
    vault = vault.resolve()
    if output.is_relative_to(vault) or vault.is_relative_to(output):
        raise ValueError("Review output and vault must not overlap")
    if output.exists():
        raise FileExistsError(f"Review output must be a new directory: {output}")
    manifest = _manifest(changes, notes)
    # Compute everything before creating the output directory where practical.
    audit = audit_library(notes, options)
    before = {n.rel_path: n.raw_content for n in notes}
    projected = before | changes
    total = sum(len(raw.encode("utf-8")) for raw in projected.values())
    if total > options.max_export_bytes:
        raise ValueError("Preview exceeds max_export_bytes")
    output.mkdir(parents=True, exist_ok=False)
    for raw in set(changes.values()):
        data = raw.encode("utf-8")
        _write_new(output / PAYLOADS_DIRECTORY / (sha256_bytes(data) + ".md"), data)
    _write_new(output / MANIFEST_NAME, _json_bytes(manifest))
    scope = SingleTenantVaultScope(vault, settings)
    load_and_validate_organization_bundle(output / MANIFEST_NAME, vault_root=vault, scope=scope)
    # Copy source bytes, not live rereads, so the preview is the exact reviewed revision.
    for path, raw in projected.items():
        normalize_vault_rel_path(path)
        _write_new(output / PREVIEW_DIRECTORY / path, raw.encode("utf-8"))
    attachments = _copy_attachments(vault, output, options, audit, scope, total)
    differences = "".join(
        line
        for path, raw in sorted(changes.items())
        for line in difflib.unified_diff(
            before.get(path, "").splitlines(keepends=True),
            raw.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )
    _write_new(output / CHANGES_NAME, differences.encode("utf-8"))
    _write_new(output / AUDIT_NAME, audit.model_dump_json(indent=2).encode("utf-8"))
    text = TEXT[options.language]
    report = [
        f"# {text['report']}",
        text["evidence"],
        text["next"],
        markdown_link(REPORT_NAME, f"{PREVIEW_DIRECTORY}/{options.home}", text["home"]),
    ]
    report.extend(f"- {f.code}: {f.path}: {f.detail}" for f in audit.findings)
    if recipe:
        _write_new(output / RECIPE_NAME, recipe.model_dump_json(indent=2).encode("utf-8"))
        report.extend(f"- {item.target}: {item.rationale}" for item in recipe.notes)
    _write_new(output / REPORT_NAME, ("\n\n".join(report) + "\n").encode("utf-8"))
    _write_new(output / SUBJECT_TEMPLATE_NAME, text["template_body"].encode("utf-8"))
    snapshot = {
        "schema": LIBRARY_SCHEMA,
        "options": options.model_dump(),
        "sources": audit.source_hashes,
        "attachments": attachments,
        "manifest_sha256": sha256_bytes((output / MANIFEST_NAME).read_bytes()),
        "preview": {p: sha256_bytes(raw.encode("utf-8")) for p, raw in projected.items()},
        "editorial_review": "required" if recipe else "not_applicable",
    }
    _write_new(output / SNAPSHOT_NAME, _json_bytes(snapshot))
    return {
        "output": str(output),
        "notes": len(notes),
        "operations": len(changes),
        "manifest_sha256": snapshot["manifest_sha256"],
        "vault_changed": False,
        "editorial_review": snapshot["editorial_review"],
    }


async def prepare_library(
    vault: Path,
    output: Path,
    options: LibraryOptions,
    settings: Settings,
    recipe: EditorialRecipe | None = None,
) -> dict[str, object]:
    """Build an exact, externally reviewable navigation/consolidation bundle."""
    vault = vault.expanduser().absolute()
    output = output.expanduser().absolute()
    notes = await read_library(vault, options)
    captured = datetime.now(tz=UTC)
    changes = _editorial_changes(notes, recipe, options, captured.isoformat())
    lookup = {n.rel_path: n for n in notes}
    projected = lookup | {p: _note_from_text(p, raw, vault, captured) for p, raw in changes.items()}
    pages = navigation(
        list(projected.values()),
        options,
        captured.isoformat(),
        archive_tags=vault_archive_tags(vault),
    )
    for path, body in pages.items():
        if path in changes:
            raise ValueError(f"Navigation collides with an editorial target: {path}")
        changes[path] = _generated_page(path, body, lookup.get(path), options, captured.isoformat())
    result = await asyncio.to_thread(
        _write_workbench, vault, output, options, notes, changes, recipe, settings
    )
    await check_library(vault, output, settings)
    return result


async def check_library(vault: Path, output: Path, settings: Settings) -> dict[str, object]:
    """Reject stale source sets, changed preview bytes or invalid manifest payloads."""
    vault = vault.expanduser().absolute()
    output = output.expanduser().absolute()
    assert_path_chain_without_links(output / SNAPSHOT_NAME)
    snapshot = json.loads(
        await asyncio.to_thread((output / SNAPSHOT_NAME).read_text, encoding="utf-8")
    )
    if snapshot["schema"] != LIBRARY_SCHEMA:
        raise ValueError("Unsupported library snapshot")
    options = LibraryOptions.model_validate(snapshot["options"])
    notes = await read_library(vault, options)
    current = {n.rel_path: n.content_hash for n in notes}
    if current != snapshot["sources"]:
        raise ValueError("Source set or bytes changed; prepare a fresh review bundle")
    manifest = output / MANIFEST_NAME
    if sha256_bytes(await asyncio.to_thread(manifest.read_bytes)) != snapshot["manifest_sha256"]:
        raise ValueError("Manifest changed after preparation")
    await asyncio.to_thread(
        load_and_validate_organization_bundle,
        manifest,
        vault_root=vault,
        scope=SingleTenantVaultScope(vault, settings),
    )
    for relative, digest in (snapshot["preview"] | snapshot["attachments"]).items():
        normalize_vault_rel_path(relative if relative.endswith(".md") else relative + ".md")
        path = output / PREVIEW_DIRECTORY / relative
        assert_path_chain_without_links(path, anchor=(output / PREVIEW_DIRECTORY).absolute())
        if sha256_bytes(await asyncio.to_thread(path.read_bytes)) != digest:
            raise ValueError(f"Preview changed: {relative}")
    preview_root = output / PREVIEW_DIRECTORY
    actual_notes = {p.relative_to(preview_root).as_posix() for p in preview_root.rglob("*.md")}
    if actual_notes != set(snapshot["preview"]):
        raise ValueError("Preview note set changed")
    for relative, digest in snapshot["attachments"].items():
        path = vault / relative
        assert_path_chain_without_links(path, anchor=vault.absolute())
        if sha256_bytes(await asyncio.to_thread(path.read_bytes)) != digest:
            raise ValueError(f"Attachment changed: {relative}")
    return {
        "valid": True,
        "notes": len(notes),
        "semantic_truth": "not_verified",
        "vault_changed": False,
    }
