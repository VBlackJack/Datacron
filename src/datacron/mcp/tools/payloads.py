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

import time
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from datacron.core.config import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from datacron.core.logger import get_logger
from datacron.mcp.bounds import bounded_count as _bounded_count
from datacron.mcp.sandbox import (
    sanitize_metadata_value,
)

__all__ = ["_bounded_count"]

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

_LOGGER = get_logger("datacron.mcp.tools")
_INTERNAL_ERROR_CODE: Final[str] = "internal_error"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def _error_response(tool: str, exc: BaseException, started: float, **fields: Any) -> dict[str, Any]:
    message = sanitize_metadata_value(str(exc))
    _audit(tool, started, error=type(exc).__name__, error_message=message, **fields)
    error: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": message,
    }
    code = getattr(exc, "code", None)
    if code is not None:
        error["code"] = code
    return {"error": error}


def _internal_error_response(tool: str, started: float, **fields: Any) -> dict[str, Any]:
    """Log the real cause locally, and return a payload that can be joined to it.

    Nothing about the host filesystem leaves the MCP surface: no ``errno``, no
    ``winerror``, no path, no strerror. That opacity is deliberate and unchanged.

    What was missing was any way to connect an opaque ``internal error`` to the
    local log line that does carry the detail, so whoever hit one had nowhere to go
    and nothing to quote. ``correlation_id`` is that join, and ``code`` makes the
    class of failure stable enough to branch on without parsing prose.

    Call this from inside an ``except`` block: the traceback comes from the live
    exception context.
    """
    correlation_id = uuid4().hex[:12]
    _LOGGER.exception("%s failed correlation_id=%s %s", tool, correlation_id, fields or "")
    payload = _error_response(
        tool,
        RuntimeError("internal error"),
        started,
        correlation_id=correlation_id,
        **fields,
    )
    payload["error"]["code"] = _INTERNAL_ERROR_CODE
    payload["error"]["correlation_id"] = correlation_id
    return payload


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
