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
"""Guided end-to-end setup for a Datacron vault.

:func:`run_setup` is the non-interactive orchestration behind the
``datacron setup`` command: it initializes the sidecar, builds the search
index, and (optionally) wires the vault into an MCP client. Keeping the logic
here — as a pure function over a :class:`SetupPlan` — makes the whole flow unit
testable without simulating terminal prompts; the CLI layer only gathers the
plan (from flags or interactive questions) and renders the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from datacron.bootstrap import BootstrapResult, initialize_vault
from datacron.core.config import (
    VALID_DURABILITY_MODES,
    Settings,
    VaultConfig,
    get_settings,
    load_vault_config,
)
from datacron.core.logger import get_logger
from datacron.core.paths import sidecar_index_db, sidecar_vault_config
from datacron.core.vault import build_configured_reader
from datacron.indexing.chunker import MarkdownChunker
from datacron.indexing.fts5_store import SQLiteFTS5Store
from datacron.indexing.reconcile import reconcile
from datacron.installers.claude_desktop import (
    ClaudeDesktopConfigError,
    install_claude_desktop_config,
)

__all__ = [
    "CLIENT_CHOICES",
    "CLIENT_CLAUDE_DESKTOP",
    "CLIENT_NONE",
    "DEFAULT_WRITE_SUBFOLDER",
    "SetupPlan",
    "SetupResult",
    "run_setup",
]

_LOGGER = get_logger(__name__)

CLIENT_CLAUDE_DESKTOP: Final[str] = "claude-desktop"
CLIENT_NONE: Final[str] = "none"
CLIENT_CHOICES: Final[tuple[str, ...]] = (CLIENT_CLAUDE_DESKTOP, CLIENT_NONE)

# Default write-enabled subfolder, matching the ``_memory`` convention that the
# write tools (create_note_ai) target. Only used when the operator opts in to
# writing without naming an explicit subfolder.
DEFAULT_WRITE_SUBFOLDER: Final[str] = "_memory"

# Runtime environment keys embedded into the MCP client config. Datacron's
# Settings derive these from the ``DATACRON_`` prefix; they are named
# explicitly here because the client config is plain JSON, not a Settings load.
_ENV_WRITE_PATHS: Final[str] = "DATACRON_WRITE_PATHS"
_ENV_DURABILITY: Final[str] = "DATACRON_DURABILITY"
_ENV_READ_ONLY: Final[str] = "DATACRON_READ_ONLY"
_ENV_TRUE: Final[str] = "true"


@dataclass(frozen=True)
class SetupPlan:
    """Fully-resolved choices for a setup run.

    All paths are taken as-is; :func:`run_setup` resolves them. This object is
    what the CLI produces from flags and/or interactive answers.

    Attributes:
        vault_path: Markdown vault root to initialize and serve.
        build_index: Whether to build the FTS5 index during setup.
        enable_write: Whether to enable the confined write tools.
        write_path: Write-allowlisted directory when ``enable_write`` is set.
            ``None`` falls back to ``<vault>/_memory``.
        client: Target MCP client (:data:`CLIENT_CHOICES`).
        durability: Durability mode (:data:`VALID_DURABILITY_MODES`).
        read_only: Whether to run the server in certified read-only mode.
        force: Overwrite an existing ``VAULT.yaml``.
    """

    vault_path: Path
    build_index: bool = True
    enable_write: bool = False
    write_path: Path | None = None
    client: str = CLIENT_CLAUDE_DESKTOP
    durability: str = "best-effort"
    read_only: bool = False
    force: bool = False


@dataclass(frozen=True)
class SetupResult:
    """Outcome of :func:`run_setup`, for rendering a summary.

    Attributes:
        bootstrap: The vault initialization result.
        indexed_notes: Notes indexed during setup, or ``None`` if skipped.
        client_config_path: Written MCP client config, or ``None`` when no
            client was configured.
        write_path: The write-allowlisted directory, or ``None`` when writing
            stays disabled.
        read_only: Whether certified read-only mode was selected.
        durability: The selected durability mode.
        warnings: Non-fatal messages surfaced to the operator.
    """

    bootstrap: BootstrapResult
    indexed_notes: int | None
    client_config_path: Path | None
    write_path: Path | None
    read_only: bool
    durability: str
    warnings: list[str] = field(default_factory=list)


def _validate_plan(plan: SetupPlan) -> None:
    """Reject a plan with out-of-range enumerated values before any I/O."""
    if plan.client not in CLIENT_CHOICES:
        raise ValueError(f"Unknown client {plan.client!r}. Expected one of {list(CLIENT_CHOICES)}.")
    if plan.durability not in VALID_DURABILITY_MODES:
        raise ValueError(
            f"Unknown durability {plan.durability!r}. "
            f"Expected one of {sorted(VALID_DURABILITY_MODES)}."
        )


async def _build_index(vault_root: Path, settings: Settings) -> int:
    """Reconcile the FTS5 index for ``vault_root`` and return notes indexed.

    Mirrors the incremental path of ``datacron index``: unchanged notes are
    skipped, changed ones re-chunked, vanished ones deleted.
    """
    config = load_vault_config(sidecar_vault_config(vault_root)) or VaultConfig()
    reader = build_configured_reader(vault_root)
    chunker = MarkdownChunker(max_tokens=settings.chunk_max_tokens)
    store = SQLiteFTS5Store(term_map=config.query_expansion)
    await store.open(sidecar_index_db(vault_root))
    try:
        stats = await reconcile(store, reader, chunker, mtime_gate=True)
    finally:
        await store.close()
    return int(stats["reindexed_notes"])


def _resolve_write_path(plan: SetupPlan, vault_root: Path) -> Path:
    """Resolve the write-allowlisted directory, defaulting to ``<vault>/_memory``."""
    if plan.write_path is not None:
        return plan.write_path.expanduser().resolve()
    return vault_root / DEFAULT_WRITE_SUBFOLDER


async def run_setup(plan: SetupPlan) -> SetupResult:
    """Run the full setup sequence for ``plan`` and return a summary.

    Steps: initialize the sidecar, build the index (optional), and configure the
    selected MCP client with the chosen write/durability/read-only options.
    Client configuration failures are captured as warnings rather than aborting
    an otherwise-successful vault initialization.

    Args:
        plan: The resolved setup choices.

    Returns:
        A :class:`SetupResult` describing everything that was done.

    Raises:
        ValueError: If the plan carries an invalid enumerated value.
        NotADirectoryError: If the vault path exists but is not a directory.
    """
    _validate_plan(plan)
    warnings: list[str] = []
    settings = get_settings()

    _LOGGER.info("cli.setup started (vault=%s, client=%s)", plan.vault_path, plan.client)
    bootstrap = initialize_vault(plan.vault_path, force=plan.force)
    vault_root = bootstrap.vault_path

    indexed_notes: int | None = None
    if plan.build_index:
        indexed_notes = await _build_index(vault_root, settings)
        _LOGGER.info("cli.setup indexed %d notes for %s", indexed_notes, vault_root)

    write_path: Path | None = None
    if plan.enable_write:
        write_path = _resolve_write_path(plan, vault_root)
        write_path.mkdir(parents=True, exist_ok=True)

    client_config_path: Path | None = None
    if plan.client == CLIENT_CLAUDE_DESKTOP:
        extra_env = _build_extra_env(plan, write_path)
        try:
            client_config_path = install_claude_desktop_config(vault_root, extra_env=extra_env)
        except ClaudeDesktopConfigError as exc:
            warnings.append(f"Claude Desktop config not written: {exc}")
            _LOGGER.warning("cli.setup client config failed: %s", exc)

    _LOGGER.info("cli.setup completed for %s", vault_root)
    return SetupResult(
        bootstrap=bootstrap,
        indexed_notes=indexed_notes,
        client_config_path=client_config_path,
        write_path=write_path,
        read_only=plan.read_only,
        durability=plan.durability,
        warnings=warnings,
    )


def _build_extra_env(plan: SetupPlan, write_path: Path | None) -> dict[str, str]:
    """Build the extra client-config environment from the plan's options."""
    env: dict[str, str] = {_ENV_DURABILITY: plan.durability}
    if write_path is not None:
        env[_ENV_WRITE_PATHS] = str(write_path)
    if plan.read_only:
        env[_ENV_READ_ONLY] = _ENV_TRUE
    return env
