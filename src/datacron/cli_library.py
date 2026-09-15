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
"""The ``datacron library`` commands: offline navigation and sourced consolidation reviews."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Final

import typer

from datacron.core.config import Settings, get_settings
from datacron.core.frontmatter import FrontmatterError
from datacron.organization.library import audit_library, read_library
from datacron.organization.library_models import EditorialRecipe, LibraryOptions
from datacron.organization.library_workbench import check_library, prepare_library, propose_split

_EXIT_INPUT_ERROR: Final[int] = 2
_VAULT_HELP: Final[str] = (
    "Vault root. Fallback: DATACRON_VAULT_ROOT, then cwd containing VAULT.yaml under .datacron."
)
_OPTIONS_HELP: Final[str] = (
    "JSON file with the library options: scope, home, tags, language, areas and limits."
)
_OUTPUT_HELP: Final[str] = "New directory, outside the vault, that receives the review bundle."
_RECIPE_HELP: Final[str] = "Optional JSON editorial recipe of sourced consolidation notes."
_SOURCE_HELP: Final[str] = "Vault-relative path of the note to split into H2 section notes."

app = typer.Typer(help="Prepare readable offline navigation and sourced consolidation reviews.")
VaultArgument = Annotated[
    Path | None, typer.Option("--vault", exists=True, file_okay=False, help=_VAULT_HELP)
]
OptionsArgument = Annotated[
    Path, typer.Option("--options", exists=True, dir_okay=False, help=_OPTIONS_HELP)
]
OutputArgument = Annotated[Path, typer.Option("--output", help=_OUTPUT_HELP)]
RecipeArgument = Annotated[
    Path | None, typer.Option("--recipe", exists=True, dir_okay=False, help=_RECIPE_HELP)
]
SourceArgument = Annotated[str, typer.Option("--source", help=_SOURCE_HELP)]


def _options(path: Path) -> LibraryOptions:
    return LibraryOptions.model_validate_json(path.read_text(encoding="utf-8"))


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return f"Missing required note field: {exc.args[0]}"
    return str(exc)


def _vault_root(vault: Path | None) -> tuple[Path, Settings]:
    """Resolve the vault the way every other command does: flag, environment, then cwd.

    The settings are bound to that vault like every other vault command binds them,
    so an explicit vault needs no separate read or write path configuration.
    """
    # The root CLI module mounts this sub-application, so the shared helpers are
    # imported when a command runs, not when the module loads.
    from datacron.cli import _resolve_vault_root, _settings_for_cli_vault  # noqa: PLC0415

    settings = get_settings()
    vault_root = _resolve_vault_root(vault, settings, error_exit_code=_EXIT_INPUT_ERROR)
    return vault_root, _settings_for_cli_vault(settings, vault_root)


@app.command()
def audit(options: OptionsArgument, vault: VaultArgument = None) -> None:
    """Print a scoped readability report as JSON without changing the vault.

    Exit code 0 means success; 2 means the vault, the options, the recipe or the bundle
    could not be read or is invalid.
    """
    try:
        vault_root, _settings = _vault_root(vault)
        config = _options(options)
        notes = asyncio.run(read_library(vault_root, config))
        typer.echo(audit_library(notes, config).model_dump_json(indent=2))
    except (OSError, ValueError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(_EXIT_INPUT_ERROR) from exc


@app.command()
def prepare(
    options: OptionsArgument,
    output: OutputArgument,
    vault: VaultArgument = None,
    recipe: RecipeArgument = None,
) -> None:
    """Create an external preview, source snapshot, diff and exact apply manifest.

    Exit code 0 means success; 2 means the vault, the options, the recipe or the bundle
    could not be read or is invalid.
    """
    try:
        vault_root, settings = _vault_root(vault)
        editorial = (
            EditorialRecipe.model_validate_json(recipe.read_text(encoding="utf-8"))
            if recipe
            else None
        )
        result = asyncio.run(
            prepare_library(vault_root, output, _options(options), settings, editorial)
        )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, FrontmatterError) as exc:
        typer.echo(_failure_message(exc), err=True)
        raise typer.Exit(_EXIT_INPUT_ERROR) from exc


@app.command()
def check(output: OutputArgument, vault: VaultArgument = None) -> None:
    """Recheck source freshness, preview integrity and exact manifest admission.

    Exit code 0 means success; 2 means the vault, the options, the recipe or the bundle
    could not be read or is invalid.
    """
    try:
        vault_root, settings = _vault_root(vault)
        result = asyncio.run(check_library(vault_root, output, settings))
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, FrontmatterError) as exc:
        typer.echo(_failure_message(exc), err=True)
        raise typer.Exit(_EXIT_INPUT_ERROR) from exc


@app.command()
def split(options: OptionsArgument, source: SourceArgument, vault: VaultArgument = None) -> None:
    """Print an exact H2 split recipe; retain original notes and anchors.

    Exit code 0 means success; 2 means the vault, the options, the recipe or the bundle
    could not be read or is invalid.
    """
    try:
        vault_root, _settings = _vault_root(vault)
        config = _options(options)
        notes = asyncio.run(read_library(vault_root, config))
        note = next((n for n in notes if n.rel_path == source), None)
        if note is None:
            raise ValueError("Source is not an admitted note in the selected scope")
        typer.echo(propose_split(note, config).model_dump_json(indent=2))
    except (OSError, ValueError, FrontmatterError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(_EXIT_INPUT_ERROR) from exc
