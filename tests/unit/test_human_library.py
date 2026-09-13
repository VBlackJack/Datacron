# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Offline navigation and editorial workbench safety contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from ulid import ULID

from datacron.cli import app
from datacron.core.config import Settings
from datacron.core.frontmatter import parse, serialize
from datacron.organization.library import (
    audit_library,
    lifecycle,
    links_and_tasks,
    markdown_link,
    read_library,
    resolve_link,
)
from datacron.organization.library_models import (
    EditorialNote,
    EditorialRecipe,
    LibraryOptions,
    SourceReference,
)
from datacron.organization.library_workbench import check_library, prepare_library


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, LibraryOptions, Settings]:
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / ".datacron").mkdir()
    (vault / ".datacron/VAULT.yaml").write_text(
        "organization:\n  scope: notes\n  rules:\n"
        "    - tag: memory/fact\n      folder: notes\n      naming: '{slug}'\n",
        encoding="utf-8",
    )
    options = LibraryOptions(
        scope="notes", home="notes/accueil.md", tags=["memory/fact"], language="fr"
    )
    return (
        vault,
        options,
        Settings(vault_root=vault, read_paths=[vault], write_paths=[vault / "notes"]),
    )


def _note(vault: Path, name: str, body: str, **metadata: object) -> Path:
    path = vault / "notes" / name
    meta = {"id": str(ULID()), "title": path.stem, "tags": ["memory/fact"]} | metadata
    path.write_text(serialize(meta, body), encoding="utf-8", newline="")
    return path


def _bytes(vault: Path) -> dict[str, bytes]:
    return {
        p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*") if p.is_file()
    }


async def test_prepare_is_read_only_and_links_open_offline(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, settings = library
    _note(
        vault,
        "guide.md",
        "# Guide\n\n## Restore\n\n- [ ] Restore backup\n\n![Diagram](diagram.png)\n",
    )
    (vault / "notes/diagram.png").write_bytes(b"image-fixture")
    before = _bytes(vault)
    output = tmp_path / "review"
    result = await prepare_library(vault, output, options, settings)
    assert result["vault_changed"] is False
    assert before == _bytes(vault)
    assert (output / "preview/notes/diagram.png").read_bytes() == b"image-fixture"
    assert (await check_library(vault, output, settings))["valid"] is True
    preview_notes = await read_library(output / "preview", options)
    generated = [n for n in preview_notes if n.frontmatter.get("library_generated")]
    assert generated
    for note in generated:
        for target, wiki in links_and_tasks(note.content)[0]:
            assert resolve_link(note.rel_path, target, wiki, preview_notes)[0] == "note"
    assert "Cases ouvertes" in (output / "preview/notes/accueil.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutation", ["change", "add", "delete", "preview", "manifest", "attachment"]
)
async def test_check_refuses_stale_or_tampered_inputs(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path, mutation: str
) -> None:
    vault, options, settings = library
    source = _note(vault, "source.md", "# Source\n\n![Image](image.png)\n")
    (vault / "notes/image.png").write_bytes(b"old")
    output = tmp_path / "review"
    await prepare_library(vault, output, options, settings)
    if mutation == "change":
        source.write_text(source.read_text(encoding="utf-8") + "Changed", encoding="utf-8")
    elif mutation == "add":
        _note(vault, "new.md", "New")
    elif mutation == "delete":
        source.unlink()
    elif mutation == "preview":
        (output / "preview/notes/source.md").write_text("Tampered", encoding="utf-8")
    elif mutation == "manifest":
        (output / "manifest.json").write_text("{}", encoding="utf-8")
    else:
        (vault / "notes/image.png").write_bytes(b"new")
    with pytest.raises(ValueError, match=r"changed|Changed"):
        await check_library(vault, output, settings)


@pytest.mark.parametrize("target", ["existing", "inside", "parent"])
async def test_prepare_refuses_overlapping_or_existing_output(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path, target: str
) -> None:
    vault, options, settings = library
    _note(vault, "source.md", "Body")
    output = {"existing": tmp_path / "existing", "inside": vault / "review", "parent": tmp_path}[
        target
    ]
    if target == "existing":
        output.mkdir()
    before = _bytes(vault)
    with pytest.raises((ValueError, FileExistsError)):
        await prepare_library(vault, output, options, settings)
    assert before == _bytes(vault)


def test_parser_ignores_examples_and_preserves_real_tasks() -> None:
    body = """# Real

- [ ] Follow up

~~~md
[[Fake]]
- [ ] Fake task
~~~

    [[Also fake]]

`[[Inline fake]]`

[[Actual#Heading]] and [Document](actual.md).
"""
    links, tasks = links_and_tasks(body)
    assert tasks == ["Follow up"]
    assert links == [("Actual#Heading", True), ("actual.md", False)]


async def test_lifecycle_uses_explicit_evidence_not_age(
    library: tuple[Path, LibraryOptions, Settings],
) -> None:
    vault, options, _ = library
    _note(vault, "old.md", "Old but valid", last_verified="2001-01-01")
    _note(vault, "archive.md", "Historical", archived=True)
    _note(vault, "unknown.md", "Unknown")
    notes = await read_library(vault, options)
    states = {n.title: lifecycle(n, notes) for n in notes}
    assert states == {"old": "active", "archive": "historical", "unknown": "review"}


async def test_duplicate_titles_and_anchor_checks_are_candidates(
    library: tuple[Path, LibraryOptions, Settings],
) -> None:
    vault, options, _ = library
    _note(
        vault,
        "first.md",
        "# Subject\n\n## Exists\n\n[[Shared]]\n[Bad](second.md#missing)",
        title="Shared",
    )
    _note(vault, "second.md", "# Other", title="Shared")
    report = audit_library(await read_library(vault, options), options)
    codes = {f.code for f in report.findings}
    assert {"AMBIGUOUS", "ANCHOR_UNVERIFIED", "DUPLICATE_TITLE_CANDIDATE"} <= codes


async def test_editorial_merge_preserves_sources_and_archives_explicitly(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, settings = library
    _note(vault, "source.md", "# Original\r\n\r\nDecision from 2020.\r\n", aliases=["Old name"])
    notes = await read_library(vault, options)
    source = notes[0]
    recipe = EditorialRecipe(
        notes=[
            EditorialNote(
                target="notes/summary.md",
                title="Summary",
                body="# Summary\n\nDecision from 2020.",
                tags=options.tags,
                sources=[SourceReference(path=source.rel_path, sha256=source.content_hash)],
                rationale="Consolidate the historical decision",
                archive_sources=[source.rel_path],
            )
        ]
    )
    output = tmp_path / "review"
    before = _bytes(vault)
    await prepare_library(vault, output, options, settings, recipe)
    assert before == _bytes(vault)
    raw = (output / "preview/notes/source.md").read_bytes().decode("utf-8")
    meta, body = parse(raw)
    assert meta["archived"] is True
    assert meta["aliases"] == ["Old name"]
    assert meta["id"] == source.id
    assert body == source.content
    assert "# Original\r\n\r\nDecision from 2020.\r\n" in raw
    assert "[source](source.md)" in (output / "preview/notes/summary.md").read_text(
        encoding="utf-8"
    )
    assert (await check_library(vault, output, settings))["semantic_truth"] == "not_verified"


@pytest.mark.parametrize("problem", ["stale", "open_task", "existing", "escape"])
async def test_editorial_refuses_unsafe_proposals(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path, problem: str
) -> None:
    vault, options, settings = library
    _note(vault, "source.md", "# Source\n\n- [ ] Open action\n")
    source = (await read_library(vault, options))[0]
    target = {"existing": source.rel_path, "escape": "../escape.md"}.get(problem, "notes/new.md")
    recipe = EditorialRecipe(
        notes=[
            EditorialNote(
                target=target,
                title="New",
                body="# New",
                tags=options.tags,
                sources=[
                    SourceReference(
                        path=source.rel_path,
                        sha256="0" * 64 if problem == "stale" else source.content_hash,
                    )
                ],
                rationale="Review",
                archive_sources=[source.rel_path] if problem == "open_task" else [],
            )
        ]
    )
    with pytest.raises(ValueError, match=r"changed|checkboxes|target|traverse"):
        await prepare_library(vault, tmp_path / "review", options, settings, recipe)
    assert not (tmp_path / "review").exists()


async def test_existing_home_is_never_silently_overwritten(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, settings = library
    _note(vault, "accueil.md", "# My personal home")
    with pytest.raises(ValueError, match="handwritten"):
        await prepare_library(vault, tmp_path / "review", options, settings)


def test_markdown_links_escape_titles_and_encode_paths() -> None:
    assert (
        markdown_link("notes/home.md", "notes/é [x].md", "[label](bad)")
        == r"[\[label\](bad)](%C3%A9%20%5Bx%5D.md)"
    )


def test_cli_audit_has_no_vault_side_effects(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, _ = library
    _note(vault, "source.md", "# Source")
    option_path = tmp_path / "options.json"
    option_path.write_text(options.model_dump_json(), encoding="utf-8")
    before = _bytes(vault)
    result = CliRunner().invoke(
        app, ["library", "audit", "--vault", str(vault), "--options", str(option_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["notes"] == 1
    assert before == _bytes(vault)


async def test_split_preserves_setext_and_duplicate_heading_sections(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
) -> None:
    from datacron.organization.library_workbench import propose_split

    vault, options, settings = library
    _note(
        vault,
        "long.md",
        "# Long\n\nShared context.\n\nFirst\n-----\n\nExact A.\n\n## First\n\nExact B.\n",
    )
    source = (await read_library(vault, options))[0]
    recipe = propose_split(source, options)
    assert len(recipe.notes) == 2
    assert "Exact A." in recipe.notes[0].body
    assert "Exact B." in recipe.notes[1].body
    output = tmp_path / "split"
    await prepare_library(vault, output, options, settings, recipe)
    assert (output / "preview/notes/long.md").read_bytes() == (vault / "notes/long.md").read_bytes()


async def test_bundle_applies_replays_and_refreshes_navigation(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
) -> None:
    from datacron.core.paths import sidecar_index_db
    from datacron.mcp.server import build_app
    from datacron.mcp.tools.organization import _apply_organization_manifest_impl

    vault, options, settings = library
    _note(vault, "source.md", "# Source\n\n## State\n\nConfirmed.")
    output = tmp_path / "review"
    result = await prepare_library(vault, output, options, settings)
    application = build_app(settings=settings, vault_root=vault)
    await application.store.open(sidecar_index_db(vault))
    try:
        preview = await _apply_organization_manifest_impl(
            application,
            manifest_path=str(output / "manifest.json"),
            expected_manifest_sha256=str(result["manifest_sha256"]),
            mode="validate",
        )
        assert "error" not in preview, preview
        applied = await _apply_organization_manifest_impl(
            application,
            manifest_path=str(output / "manifest.json"),
            expected_manifest_sha256=str(result["manifest_sha256"]),
            mode="apply",
            confirmation_token=preview["confirmation_token"],
        )
        assert applied.get("indexed") is True, applied
        assert (vault / options.home).exists()
        replay = await _apply_organization_manifest_impl(
            application,
            manifest_path=str(output / "manifest.json"),
            expected_manifest_sha256=str(result["manifest_sha256"]),
            mode="apply",
            confirmation_token=preview["confirmation_token"],
        )
        assert replay.get("already_committed") is True, replay
        await prepare_library(vault, tmp_path / "refresh", options, settings)
        home = vault / options.home
        home.write_text(home.read_text(encoding="utf-8") + "Personal addition.", encoding="utf-8")
        with pytest.raises(ValueError, match="handwritten or modified"):
            await prepare_library(vault, tmp_path / "unsafe-refresh", options, settings)
    finally:
        await application.store.close()


async def test_relative_output_and_extra_preview_note_are_checked(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, options, settings = library
    _note(vault, "source.md", "# Source")
    monkeypatch.chdir(tmp_path)
    await prepare_library(vault, Path("relative-review"), options, settings)
    assert (await check_library(vault, Path("relative-review"), settings))["valid"] is True
    (tmp_path / "relative-review/preview/extra.md").write_text("Unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="Preview note set changed"):
        await check_library(vault, Path("relative-review"), settings)


async def test_confined_export_ignores_remote_and_excluded_attachments(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    from typing import Any

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Network is forbidden in an offline library operation")

    monkeypatch.setattr(socket, "create_connection", no_network)
    vault, options, settings = library
    _note(
        vault,
        "source.md",
        "# Source\n\n[Metadata](../.datacron/VAULT.yaml)\n![Remote](https://example.invalid/image.png)\n![Outside](../../outside.png)\n",
    )
    (tmp_path / "outside.png").write_bytes(b"outside")
    output = tmp_path / "review"
    await prepare_library(vault, output, options, settings)
    assert not (output / "preview/.datacron").exists()
    snapshot = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["attachments"] == {}


async def test_obsidian_relative_attachment_is_available_in_preview(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
) -> None:
    vault, options, settings = library
    options = options.model_copy(update={"folder_labels": {"notes": "Mes connaissances"}})
    _note(vault, "source.md", "# Source\n\n![[diagram.png]]\n")
    (vault / "notes/diagram.png").write_bytes(b"local-image")
    output = tmp_path / "review"
    await prepare_library(vault, output, options, settings)
    assert (output / "preview/notes/diagram.png").read_bytes() == b"local-image"
    assert "Mes connaissances" in (output / "preview/notes/accueil.md").read_text(encoding="utf-8")
