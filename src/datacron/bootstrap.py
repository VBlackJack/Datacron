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
"""Vault bootstrap primitives shared by ``datacron init`` and ``datacron setup``.

This module owns the side-effecting sequence that turns any Markdown folder into
a Datacron vault: creating the ``.datacron/`` sidecar tree and writing
``VAULT.yaml``. Keeping it here (rather than inline in the CLI) lets both the
low-level ``init`` command and the guided ``setup`` wizard share one
implementation, so the two can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import yaml
from ulid import ULID

from datacron import __version__
from datacron.core.config import (
    DEFAULT_ENCODING,
    DEFAULT_EXCLUDED_FILES,
    DEFAULT_EXCLUDED_FOLDERS,
    DEFAULT_HISTORY_MODE,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_LINE_ENDINGS,
    HISTORY_DIR_NAME,
    OPLOG_DIR_NAME,
    OPLOG_PENDING_DIR_NAME,
    VAULT_VERSION_KEY,
)
from datacron.core.logger import get_logger
from datacron.core.paths import (
    sidecar_dir,
    sidecar_index_dir,
    sidecar_vault_config,
)
from datacron.core.query_expansion import query_expansion_seed

__all__ = ["DEFAULT_DRAFTS_FOLDER", "DEFAULT_JOURNAL_FOLDER", "BootstrapResult", "initialize_vault"]

_LOGGER = get_logger(__name__)

DEFAULT_DRAFTS_FOLDER: Final[str] = "_drafts"
DEFAULT_JOURNAL_FOLDER: Final[str] = "_journal"
_LOGS_DIR_NAME: Final[str] = "logs"


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of :func:`initialize_vault`.

    Attributes:
        vault_path: The resolved vault root that was initialized.
        sidecar_path: The ``.datacron/`` sidecar directory.
        config_path: The ``.datacron/VAULT.yaml`` file.
        vault_id: The vault identifier. ``None`` when an existing config was
            kept (``created`` is then ``False``).
        created: ``True`` when a fresh ``VAULT.yaml`` was written, ``False``
            when an existing config was preserved.
    """

    vault_path: Path
    sidecar_path: Path
    config_path: Path
    vault_id: str | None
    created: bool


def _format_vault_yaml(vault_id: str, created: datetime) -> str:
    """Serialize a fresh ``VAULT.yaml`` payload with seeded defaults."""
    payload = {
        VAULT_VERSION_KEY: __version__,
        "vault_id": vault_id,
        "created": created.isoformat(),
        "encoding": DEFAULT_ENCODING,
        "line_endings": DEFAULT_LINE_ENDINGS,
        "history_retention_days": DEFAULT_HISTORY_RETENTION_DAYS,
        "history_mode": DEFAULT_HISTORY_MODE,
        "folders": {
            "drafts": DEFAULT_DRAFTS_FOLDER,
            "journal": DEFAULT_JOURNAL_FOLDER,
        },
        "excluded_folders": list(DEFAULT_EXCLUDED_FOLDERS),
        "excluded_files": list(DEFAULT_EXCLUDED_FILES),
        "query_expansion": query_expansion_seed(),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def initialize_vault(vault_path: Path, *, force: bool = False) -> BootstrapResult:
    """Create the ``.datacron/`` sidecar tree and ``VAULT.yaml`` for a vault.

    The vault directory is created if it does not exist. The sidecar
    subdirectories (index, logs, history, operation journal) are created
    idempotently. ``VAULT.yaml`` is written only when absent, unless ``force``
    is set, so re-running never clobbers an existing vault identity by accident.

    Args:
        vault_path: Path to the Markdown vault to initialize. It is expanded and
            resolved to an absolute path.
        force: When ``True``, overwrite an existing ``VAULT.yaml``.

    Returns:
        A :class:`BootstrapResult` describing what was created or preserved.

    Raises:
        NotADirectoryError: If ``vault_path`` exists and is not a directory.
    """
    resolved = vault_path.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise NotADirectoryError(f"{resolved} exists and is not a directory.")

    if not resolved.exists():
        resolved.mkdir(parents=True, exist_ok=True)
        _LOGGER.info("Created vault directory %s", resolved)

    sidecar = sidecar_dir(resolved)
    sidecar.mkdir(parents=True, exist_ok=True)
    sidecar_index_dir(resolved).mkdir(parents=True, exist_ok=True)
    (sidecar / _LOGS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (sidecar / HISTORY_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (sidecar / OPLOG_DIR_NAME / OPLOG_PENDING_DIR_NAME).mkdir(parents=True, exist_ok=True)

    config_path = sidecar_vault_config(resolved)
    if config_path.exists() and not force:
        _LOGGER.info("VAULT.yaml already present at %s; keeping existing config", config_path)
        return BootstrapResult(
            vault_path=resolved,
            sidecar_path=sidecar,
            config_path=config_path,
            vault_id=None,
            created=False,
        )

    vault_id = str(ULID())
    now = datetime.now(tz=UTC)
    config_path.write_text(_format_vault_yaml(vault_id, now), encoding=DEFAULT_ENCODING)
    _LOGGER.info("Initialized Datacron vault at %s (vault_id=%s)", resolved, vault_id)

    return BootstrapResult(
        vault_path=resolved,
        sidecar_path=sidecar,
        config_path=config_path,
        vault_id=vault_id,
        created=True,
    )
