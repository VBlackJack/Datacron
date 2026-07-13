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
"""Shared response bounds, audit, redaction, and sanitization helpers."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Final

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.hashing import HASH_HEX_LENGTH
from datacron.core.logger import get_logger
from datacron.mcp.sandbox import (
    sanitize_metadata_value,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_LOGGER = get_logger(__name__)

GetNoteFormat = str  # "full" | "map" -- kept loose for FastMCP schema
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"full", "map"})
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_HEADING_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CHUNK_ID_SEPARATOR: Final[str] = "::"
_MEMORY_ORIGINS: Final[frozenset[str]] = frozenset({"ai", "human", "merged"})
_MEMORY_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low", "needs_verification"}
)
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
_ULID_CREATE_ATTEMPTS: Final[int] = 5


def _bounded_count(requested: int, ceiling: int) -> int:
    if requested <= 0:
        return ceiling
    return min(requested, ceiling)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def _error_response(tool: str, exc: BaseException, started: float, **fields: Any) -> dict[str, Any]:
    message = sanitize_metadata_value(str(exc))
    _audit(tool, started, error=type(exc).__name__, error_message=message, **fields)
    return {
        "error": {
            "type": type(exc).__name__,
            "message": message,
        }
    }


def _audit(tool: str, started: float, **fields: Any) -> None:
    duration_ms = (time.perf_counter() - started) * 1000.0
    rendered = " ".join(f"{key}={value!r}" for key, value in fields.items() if value is not None)
    _LOGGER.info("AUDIT tool=%s duration_ms=%.2f %s", tool, duration_ms, rendered)


def _redact_retrieval_text(app: DatacronApp, value: str) -> str:
    if not app.secret_redactor.retrieval_enabled(app.settings):
        return value
    return app.secret_redactor.redact_text(value)


def _sanitize_retrieval_metadata(app: DatacronApp, value: str) -> str:
    return sanitize_metadata_value(_redact_retrieval_text(app, value))


def _sanitize_optional_retrieval_metadata(
    app: DatacronApp,
    value: str | None,
) -> str | None:
    return _sanitize_retrieval_metadata(app, value) if value is not None else None
