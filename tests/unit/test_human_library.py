# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Offline navigation and editorial workbench safety contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from typer.testing import CliRunner, Result
from ulid import ULID

from datacron.cli import app
from datacron.core.config import Settings, get_settings
from datacron.core.frontmatter import parse, serialize
from datacron.core.hashing import hash_text
from datacron.core.models import Note
from datacron.core.scope import SingleTenantVaultScope
from datacron.organization import library_workbench
from datacron.organization.library import (
    audit_library,
    build_link_index,
    lifecycle,
    links_and_tasks,
    markdown_link,
    navigation,
    parse_links_and_tasks,
    read_library,
    resolve_link,
)
from datacron.organization.library_models import (
    PREVIEW_DIRECTORY,
    EditorialNote,
    EditorialRecipe,
    Finding,
    LibraryAudit,
    LibraryOptions,
    SourceReference,
)
from datacron.organization.library_text import TEXT
from datacron.organization.library_workbench import check_library, prepare_library
from datacron.organization.manifest import OrganizationBundle


@pytest.fixture(params=["fr", "en"])
def library(
    tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[Path, LibraryOptions, Settings]:
    """The library fixture, in both rendering languages.

    Every LibraryOptions in the suite pinned ``language="fr"``, so the default
    English rendering path - the one a first-time reader gets - had no test at all.
    """
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / ".datacron").mkdir()
    (vault / ".datacron/VAULT.yaml").write_text(
        "organization:\n  scope: notes\n  rules:\n"
        "    - tag: memory/fact\n      folder: notes\n      naming: '{slug}'\n",
        encoding="utf-8",
    )
    options = LibraryOptions(
        scope="notes",
        home="notes/accueil.md",
        tags=["memory/fact"],
        language=cast("Literal['en', 'fr']", request.param),
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
    index = build_link_index(preview_notes)
    for note in generated:
        for target, wiki in links_and_tasks(note.content)[0]:
            assert resolve_link(note.rel_path, target, wiki, index)[0] == "note"
    home_text = (output / "preview/notes/accueil.md").read_text(encoding="utf-8")
    assert TEXT[options.language]["tasks"] in home_text


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


async def test_a_scope_note_changed_after_validation_refuses_the_apply(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vault that moves between the apply's own preview and its lock must not commit.

    The apply used to rebuild the whole preview a second time under the mutation
    lock and compare projected report hashes, which walked and hashed every note in
    the scope again for an answer the transaction already reaches: the batch
    compares the scope inventory the preview captured against a live one, in both
    directions and by exact hash, under that same lock. The interesting drift is not
    the one between validate and apply, which the confirmation token already refuses
    before any of this runs, but the one inside the apply itself. The write here
    lands in exactly that window, so the test pins the refusal rather than the
    mechanism that produced it.
    """
    from datacron.core.paths import sidecar_index_db
    from datacron.mcp.server import build_app
    from datacron.mcp.tools import organization as organization_tools
    from datacron.mcp.tools.organization import _apply_organization_manifest_impl

    vault, options, settings = library
    bystander = _note(vault, "bystander.md", "# Bystander\n\n## State\n\nUntouched.")
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

        # The apply reloads the bundle once on entry and once under the mutation
        # lock, just before the transaction reads live state. Editing on the second
        # call puts the drift after the apply's own preview and after the token has
        # been accepted, which is the only window the removed rebuild covered.
        loader = organization_tools._load_expected_bundle
        loads = 0

        def edit_then_load(*args: Any, **kwargs: Any) -> OrganizationBundle:
            nonlocal loads
            loads += 1
            if loads == 2:
                bystander.write_text(
                    bystander.read_text(encoding="utf-8") + "\nEdited mid-apply.\n",
                    encoding="utf-8",
                    newline="",
                )
            return loader(*args, **kwargs)

        monkeypatch.setattr(organization_tools, "_load_expected_bundle", edit_then_load)

        applied = await _apply_organization_manifest_impl(
            application,
            manifest_path=str(output / "manifest.json"),
            expected_manifest_sha256=str(result["manifest_sha256"]),
            mode="apply",
            confirmation_token=preview["confirmation_token"],
        )

        assert loads == 2
        assert applied["error"]["type"] == "BatchConflictError", applied
        assert "notes/bystander.md" in applied["error"]["message"]
        assert not (vault / options.home).exists()
        assert "Edited mid-apply." in bystander.read_text(encoding="utf-8")
    finally:
        await application.store.close()


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


def _indexed_note(rel_path: str, title: str, body: str, aliases: list[str] | None = None) -> Note:
    raw = serialize({"id": str(ULID()), "title": title, "aliases": aliases or []}, body)
    return Note(
        id=str(ULID()),
        path=Path("vault") / rel_path,
        rel_path=rel_path,
        title=title,
        frontmatter={"title": title, "aliases": aliases or []},
        content=body,
        raw_content=raw,
        created=datetime(2026, 9, 1, tzinfo=UTC),
        updated=datetime(2026, 9, 1, tzinfo=UTC),
        content_hash=hash_text(raw),
        tags=[],
        aliases=aliases or [],
    )


def test_resolve_link_covers_every_outcome_with_case_collisions() -> None:
    notes = [
        _indexed_note("notes/alpha.md", "Alpha", "# Alpha\n\n## Intro\n\nBody.\n", ["Alias One"]),
        # The title of one note is the stem of another: the title tier must win.
        _indexed_note("notes/beta.md", "Gamma", "# Beta\n"),
        _indexed_note("notes/gamma.md", "beta", "# Gamma\n"),
        _indexed_note("notes/dup1.md", "Dup", "# Dup one\n"),
        _indexed_note("notes/dup2.md", "DUP", "# Dup two\n"),
    ]
    index = build_link_index(notes)
    resolve = partial(resolve_link, "notes/alpha.md")

    assert resolve("https://example.com/x", False, index) == ("external", "https://example.com/x")
    assert resolve("notes/alpha", True, index) == ("note", "notes/alpha.md")
    assert resolve("ALPHA", True, index) == ("note", "notes/alpha.md")
    assert resolve("alias one", True, index) == ("note", "notes/alpha.md")
    assert resolve("beta", True, index) == ("note", "notes/gamma.md")
    assert resolve("gamma", True, index) == ("note", "notes/beta.md")
    assert resolve("dup", True, index) == ("ambiguous", "dup")
    assert resolve("missing", True, index) == ("local_unresolved", "missing")
    assert resolve("./Beta.md", False, index) == ("note", "notes/beta.md")
    assert resolve("./nope.md", False, index) == ("local_unresolved", "notes/nope.md")
    assert resolve("Alpha#Intro", True, index) == ("note", "notes/alpha.md")
    assert resolve("Alpha#intro", True, index) == ("note", "notes/alpha.md")
    assert resolve("Alpha#Nope", True, index) == ("anchor_unverified", "notes/alpha.md#Nope")
    assert resolve("#intro", True, index) == ("note", "notes/alpha.md")
    assert resolve("", True, index) == ("note", "notes/alpha.md")


def test_wikilink_names_are_matched_literally_not_as_urls() -> None:
    """A colon or a question mark in a wikilink is part of the note name.

    Read as a URL, [[Project: Alpha]] was external, a broken [[Missing: thing]]
    was never reported, and [[What is X?]] was looked up as "What is X".
    """
    notes = [
        _indexed_note("notes/project.md", "Project: Alpha", "# Project\n\n## Scope\n"),
        _indexed_note("notes/question.md", "What is X?", "# Question\n"),
        _indexed_note("notes/percent.md", "100% done", "# Percent\n"),
    ]
    index = build_link_index(notes)
    resolve = partial(resolve_link, "notes/question.md")

    assert resolve("Project: Alpha", True, index) == ("note", "notes/project.md")
    assert resolve("Project: Alpha#Scope", True, index) == ("note", "notes/project.md")
    assert resolve("Project: Alpha|label", True, index) == ("note", "notes/project.md")
    assert resolve("Missing: thing", True, index) == ("local_unresolved", "Missing: thing")
    assert resolve("What is X?", True, index) == ("note", "notes/question.md")
    assert resolve("100% done", True, index) == ("note", "notes/percent.md")
    assert resolve("https://example.com/x?y", False, index)[0] == "external"


def test_every_wikilink_parser_agrees_on_the_probe() -> None:
    from datacron.core.models import ChunkType
    from datacron.indexing.wikilinks import extract_wikilink_targets
    from datacron.organization.planner import snapshot_note

    body = (
        "`[[inline-code]]` \\[[escaped]] [[Real Target|label]] [[ spaced ]]\n\n"
        "~~~\n[[in-tilde-fence]]\n~~~\n"
    )
    expected = ["Real Target", "spaced"]
    assert extract_wikilink_targets(body, ChunkType.NARRATIVE) == expected
    snapshot = snapshot_note("_memory/x.md", len(body), {}, body)
    assert snapshot.wikilink_targets == ("real target", "spaced")
    # The inner whitespace is normalized by the canon; a naive regex kept " spaced ".
    assert [target for target, wiki in links_and_tasks(body)[0] if wiki] == expected


def test_links_keep_document_order_and_header_anchors() -> None:
    body = "See [Md](./a.md) then [[Target#Section|label]] and [Img](./i.png) [[Other]].\n"
    assert links_and_tasks(body)[0] == [
        ("./a.md", False),
        ("Target#Section", True),
        ("./i.png", False),
        ("Other", True),
    ]


async def test_navigation_output_is_stable_with_a_shared_parse(
    library: tuple[Path, LibraryOptions, Settings],
) -> None:
    vault, options, _ = library
    _note(vault, "state.md", "# State\n\nCurrent.\n", tags=["memory/fact", "kind/platform"])
    _note(
        vault, "person.md", "# Person\n\n- [ ] Call back\n", tags=["memory/fact", "memory/contact"]
    )
    _note(vault, "old.md", "# Old\n\nGone.\n", tags=["memory/fact", "memory/archive"])
    _note(vault, "howto.md", "# Howto\n\n- [ ] Step\n\n- [ ] Other\n", tags=["memory/procedure"])
    notes = await read_library(vault, options)
    captured = "2026-09-15T00:00:00+00:00"

    fresh = navigation(notes, options, captured)
    shared = navigation(notes, options, captured, parsed_links=parse_links_and_tasks(notes))

    assert shared == fresh
    home = fresh[options.home]
    assert "[person](person.md)" in home
    assert "[howto (2)](howto.md)" in home
    assert "[old](old.md)" in home
    assert "[state](state.md)" in fresh["notes/navigation-notes.md"]


def _library_cli(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> tuple[Path, Path]:
    vault, options, _ = library
    _note(vault, "source.md", "# Source\n\n## First\n\nOne.\n\n## Second\n\nTwo.\n")
    option_path = tmp_path / "options.json"
    option_path.write_text(options.model_dump_json(), encoding="utf-8")
    return vault, option_path


def _invoke(*arguments: str, env: dict[str, str] | None = None) -> Result:
    return CliRunner().invoke(app, ["library", *arguments], env=env)


def test_cli_help_documents_every_option_and_the_exit_codes(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    for command in ("audit", "prepare", "check", "split"):
        result = _invoke(command, "--help")
        assert result.exit_code == 0, result.output
        assert "DATACRON_VAULT_ROOT" in result.output
        assert "Exit code 0" in result.output
    prepare_help = _invoke("prepare", "--help").output
    assert "review bundle" in prepare_help
    assert "editorial recipe" in prepare_help
    assert "library options" in prepare_help
    assert "H2 section notes" in _invoke("split", "--help").output


def test_cli_prepare_and_check_succeed_then_refuse_a_reused_output(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, option_path = _library_cli(library, tmp_path)
    output = tmp_path / "review"

    prepared = _invoke(
        "prepare", "--vault", str(vault), "--options", str(option_path), "--output", str(output)
    )
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.output)["vault_changed"] is False

    checked = _invoke("check", "--vault", str(vault), "--output", str(output))
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["valid"] is True

    reused = _invoke(
        "prepare", "--vault", str(vault), "--options", str(option_path), "--output", str(output)
    )
    assert reused.exit_code == 2
    assert "must be a new directory" in reused.output

    missing = _invoke("check", "--vault", str(vault), "--output", str(tmp_path / "nowhere"))
    assert missing.exit_code == 2
    assert missing.output.strip()


def test_cli_split_prints_a_recipe_and_refuses_an_unknown_source(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, option_path = _library_cli(library, tmp_path)

    split = _invoke(
        "split", "--vault", str(vault), "--options", str(option_path), "--source", "notes/source.md"
    )
    assert split.exit_code == 0, split.output
    titles = [item["title"] for item in json.loads(split.output)["notes"]]
    assert titles == ["source / First", "source / Second"]

    unknown = _invoke(
        "split", "--vault", str(vault), "--options", str(option_path), "--source", "notes/nope.md"
    )
    assert unknown.exit_code == 2
    assert "not an admitted note" in unknown.output


def test_cli_vault_falls_back_to_the_environment_and_refuses_without_it(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, option_path = _library_cli(library, tmp_path)
    monkeypatch.chdir(tmp_path)
    # Settings are cached per process; each invocation must read the environment anew.
    get_settings.cache_clear()

    from_environment = _invoke(
        "audit", "--options", str(option_path), env={"DATACRON_VAULT_ROOT": str(vault)}
    )
    assert from_environment.exit_code == 0, from_environment.output
    assert json.loads(from_environment.output)["notes"] == 1

    monkeypatch.delenv("DATACRON_VAULT_ROOT", raising=False)
    get_settings.cache_clear()
    without = _invoke("audit", "--options", str(option_path))
    get_settings.cache_clear()
    assert without.exit_code == 2
    assert "No vault root provided" in without.output


async def test_same_note_anchors_are_audited(
    library: tuple[Path, LibraryOptions, Settings],
) -> None:
    vault, options, _ = library
    _note(vault, "anchors.md", "# Anchors\n\n## Intro\n\nSee [[#Intro]] and [[#Nope]].\n")

    report = audit_library(await read_library(vault, options), options)

    unverified = [f for f in report.findings if f.code == "ANCHOR_UNVERIFIED"]
    assert [(f.path, f.detail) for f in unverified] == [
        ("notes/anchors.md", "notes/anchors.md#Nope")
    ]
    assert links_and_tasks("[[#Intro]] [[Note#Intro]] [[#Nope|label]]")[0] == [
        ("#Intro", True),
        ("Note#Intro", True),
        ("#Nope", True),
    ]


async def test_editorial_archive_keeps_the_heading_title_of_an_untitled_note(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, settings = library
    (vault / "notes" / "old.md").write_text(
        serialize({"id": str(ULID()), "tags": ["memory/fact"]}, "# Old note\n\nDecision.\n"),
        encoding="utf-8",
        newline="",
    )
    source = next(n for n in await read_library(vault, options) if n.rel_path == "notes/old.md")
    assert source.title == "Old note"
    recipe = EditorialRecipe(
        notes=[
            EditorialNote(
                target="notes/summary.md",
                title="Summary",
                body="# Summary\n\nDecision.",
                tags=options.tags,
                sources=[SourceReference(path=source.rel_path, sha256=source.content_hash)],
                rationale="Consolidate the decision",
                archive_sources=[source.rel_path],
            )
        ]
    )
    output = tmp_path / "review"

    await prepare_library(vault, output, options, settings, recipe)

    meta, body = parse((output / "preview/notes/old.md").read_text(encoding="utf-8"))
    assert meta["archived"] is True
    assert "title" not in meta
    assert body == source.content
    archived = next(
        n for n in await read_library(output / "preview", options) if n.rel_path == "notes/old.md"
    )
    assert archived.title == "Old note"


def test_cli_prepare_reports_an_invalid_entry_without_a_traceback(
    library: tuple[Path, LibraryOptions, Settings], tmp_path: Path
) -> None:
    vault, options, _ = library
    (vault / "notes" / "orphan.md").write_text(
        serialize({"tags": ["memory/fact"]}, "# Orphan\n\nBody.\n"), encoding="utf-8", newline=""
    )
    orphan = next(
        n for n in asyncio.run(read_library(vault, options)) if n.rel_path == "notes/orphan.md"
    )
    recipe = EditorialRecipe(
        notes=[
            EditorialNote(
                target="notes/summary.md",
                title="Summary",
                body="# Summary\n\nBody.",
                tags=options.tags,
                sources=[SourceReference(path=orphan.rel_path, sha256=orphan.content_hash)],
                rationale="Consolidate the orphan",
                archive_sources=[orphan.rel_path],
            )
        ]
    )
    option_path = tmp_path / "options.json"
    option_path.write_text(options.model_dump_json(), encoding="utf-8")
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(recipe.model_dump_json(), encoding="utf-8")
    before = _bytes(vault)

    result = CliRunner().invoke(
        app,
        [
            "library",
            "prepare",
            "--vault",
            str(vault),
            "--options",
            str(option_path),
            "--output",
            str(tmp_path / "review"),
            "--recipe",
            str(recipe_path),
        ],
    )

    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Adopt the stable source identity before consolidation" in result.output
    assert "Traceback" not in result.output
    assert before == _bytes(vault)


def test_cli_prepare_reports_a_missing_note_field_without_a_traceback(
    library: tuple[Path, LibraryOptions, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, options, _ = library
    _note(vault, "source.md", "# Source")
    option_path = tmp_path / "options.json"
    option_path.write_text(options.model_dump_json(), encoding="utf-8")

    async def missing_field(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise KeyError("title")

    monkeypatch.setattr("datacron.cli_library.prepare_library", missing_field)
    result = CliRunner().invoke(
        app,
        [
            "library",
            "prepare",
            "--vault",
            str(vault),
            "--options",
            str(option_path),
            "--output",
            str(tmp_path / "review"),
        ],
    )

    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Missing required note field: title" in result.output
    assert "Traceback" not in result.output


def test_both_rendering_languages_declare_the_same_keys() -> None:
    """A key present in one language and missing in the other renders a KeyError.

    The suite pinned French everywhere, so the English table was never exercised
    and a key added to one side only would have shipped.
    """
    assert set(TEXT["en"]) == set(TEXT["fr"])
    assert all(TEXT["en"][key] and TEXT["fr"][key] for key in TEXT["en"])


def test_one_attachment_shared_by_two_notes_is_counted_once(tmp_path: Path) -> None:
    """Dedup ran on the raw link text, accounting ran on the resolved path.

    A wiki-style link is rewritten relative to the note holding it, so two
    notes at different depths write the same file two different ways:
    "img/logo.png" from notes/first.md and "logo.png" from notes/img/deep.md
    both resolve to notes/img/logo.png. The guard compared the details, missed,
    and the bytes were added to the running total twice before the write guard
    suppressed the duplicate copy. A bundle that fits was then refused, after
    the output directory had already been written.

    The accounting is what this measures, so it calls the copier directly: the
    export bound covers the notes as well, and their size would decide the
    outcome instead.
    """
    vault = tmp_path / "vault"
    (vault / "notes" / "img").mkdir(parents=True)
    payload = b"x" * 4096
    (vault / "notes" / "img" / "logo.png").write_bytes(payload)
    audit = LibraryAudit(
        scope="notes",
        notes=2,
        source_hashes={},
        findings=[
            Finding(
                code="LOCAL_UNRESOLVED",
                path="notes/first.md",
                detail="img/logo.png",
                link_style="wiki",
            ),
            Finding(
                code="LOCAL_UNRESOLVED",
                path="notes/img/deep.md",
                detail="logo.png",
                link_style="wiki",
            ),
        ],
    )
    settings = Settings(read_paths=[vault], write_paths=[vault], vault_root=vault)
    scope = SingleTenantVaultScope(vault, settings)
    # Room for the attachment once, and not twice.
    options = LibraryOptions(
        scope="notes",
        home="notes/accueil.md",
        tags=["memory/fact"],
        max_export_bytes=len(payload) + 1,
    )

    attachments = library_workbench._copy_attachments(
        vault,
        tmp_path / "review",
        options,
        audit,
        scope,
        0,
    )

    assert list(attachments) == ["notes/img/logo.png"]
    assert (tmp_path / "review" / PREVIEW_DIRECTORY / "notes/img/logo.png").read_bytes() == payload
