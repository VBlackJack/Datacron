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
"""Read-only distribution diagnostics; never infer client behavior from files."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from datacron.core.logger import get_logger
from datacron.core.memory_protocol import (
    CONTRACT_HASH,
    CONTRACT_ID,
    CONTRACT_VERSION,
    PROTOCOL_BLOCK,
)
from datacron.installers.mcp_clients import (
    ANTIGRAVITY,
    CLAUDE_DESKTOP,
    CURSOR,
    SCOPE_PROJECT,
    SCOPE_USER,
    VS_CODE,
)
from datacron.installers.protocol import (
    PROTOCOL_ALL,
    PROTOCOL_CLIENT_IDS,
    _antigravity_project_instruction_path,
    _cursor_project_rule_path,
    _find_protocol_span,
    _install_path,
    _validate_scope,
    _vscode_user_rule_path,
)

_LOGGER = get_logger(__name__)
_VERSION = re.compile(r"datacron:contract id=([^ ]+) version=([^ ]+) sha256=([0-9a-f]{64})")


def protocol_status(
    client: str = PROTOCOL_ALL,
    *,
    scope: str = SCOPE_USER,
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect all supported targets without creating or refreshing any files."""
    _validate_scope(scope)
    if client != PROTOCOL_ALL and client not in PROTOCOL_CLIENT_IDS:
        raise ValueError(f"Unknown protocol client {client!r}")
    clients = PROTOCOL_CLIENT_IDS if client == PROTOCOL_ALL else (client,)
    rows = []
    for client_id in clients:
        path = _target(client_id, scope, project_dir)
        row: dict[str, Any] = {
            "client": client_id,
            "scope": scope,
            "distribution": "manual"
            if client_id == CURSOR and scope == SCOPE_USER
            else "unverified",
            "activation": "unverified",
            "behavior": "unverified",
        }
        if path is not None:
            row.update(_inspect(path))
        rows.append(row)
    return {
        "contract_id": CONTRACT_ID,
        "expected_version": CONTRACT_VERSION,
        "expected_hash": CONTRACT_HASH,
        "clients": rows,
    }


def _target(client: str, scope: str, project: Path | None) -> Path | None:
    if scope == SCOPE_PROJECT:
        if client not in {CURSOR, ANTIGRAVITY}:
            return None
        if project is None:
            raise ValueError("Project scope requires a project directory")
        return (
            _cursor_project_rule_path(project)
            if client == CURSOR
            else _antigravity_project_instruction_path(project)
        )
    if client in {CURSOR, CLAUDE_DESKTOP, ANTIGRAVITY}:
        return None
    return _vscode_user_rule_path() if client == VS_CODE else _install_path(client)


def _inspect(path: Path) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path), "distribution": "missing"}
    try:
        if not path.is_file():
            return row
        text = path.read_text(encoding="utf-8-sig")
        span = _find_protocol_span(text)
        if span is None:
            return row
        block = text[slice(*span)].rstrip("\r\n")
        found = _VERSION.search(block)
        row.update(
            {
                "distribution": "current" if block == PROTOCOL_BLOCK else "outdated",
                "found_version": found.group(2) if found else None,
                "declared_hash": found.group(3) if found else None,
                "block_hash": sha256(block.encode()).hexdigest(),
            }
        )
    except (OSError, UnicodeError, ValueError) as exc:
        _LOGGER.warning("Protocol inspection failed for %s: %s", path, type(exc).__name__)
        row["distribution"] = "invalid"
    return row
