# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Offline library commands; writes are confined to new external review directories."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from datacron.core.config import get_settings
from datacron.core.frontmatter import FrontmatterError
from datacron.organization.library import audit_library, read_library
from datacron.organization.library_models import EditorialRecipe, LibraryOptions
from datacron.organization.library_workbench import check_library, prepare_library, propose_split

app = typer.Typer(help="Prepare readable offline navigation and sourced consolidation reviews.")
VaultArgument = Annotated[Path, typer.Option("--vault", exists=True, file_okay=False)]
OptionsArgument = Annotated[Path, typer.Option("--options", exists=True, dir_okay=False)]
OutputArgument = Annotated[Path, typer.Option("--output")]


def _options(path: Path) -> LibraryOptions:
    return LibraryOptions.model_validate_json(path.read_text(encoding="utf-8"))


@app.command()
def audit(vault: VaultArgument, options: OptionsArgument) -> None:
    """Print a scoped readability report as JSON without changing the vault."""
    try:
        config = _options(options)
        notes = asyncio.run(read_library(vault, config))
        typer.echo(audit_library(notes, config).model_dump_json(indent=2))
    except (OSError, ValueError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command()
def prepare(
    vault: VaultArgument,
    options: OptionsArgument,
    output: OutputArgument,
    recipe: Annotated[Path | None, typer.Option("--recipe", exists=True, dir_okay=False)] = None,
) -> None:
    """Create an external preview, source snapshot, diff and exact apply manifest."""
    try:
        editorial = (
            EditorialRecipe.model_validate_json(recipe.read_text(encoding="utf-8"))
            if recipe
            else None
        )
        result = asyncio.run(
            prepare_library(vault, output, _options(options), get_settings(), editorial)
        )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command()
def check(vault: VaultArgument, output: OutputArgument) -> None:
    """Recheck source freshness, preview integrity and exact manifest admission."""
    try:
        result = asyncio.run(check_library(vault, output, get_settings()))
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command()
def split(
    vault: VaultArgument, options: OptionsArgument, source: Annotated[str, typer.Option("--source")]
) -> None:
    """Print an exact H2 split recipe; retain original notes and anchors."""
    try:
        config = _options(options)
        notes = asyncio.run(read_library(vault, config))
        note = next((n for n in notes if n.rel_path == source), None)
        if note is None:
            raise ValueError("Source is not an admitted note in the selected scope")
        typer.echo(propose_split(note, config).model_dump_json(indent=2))
    except (OSError, ValueError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
