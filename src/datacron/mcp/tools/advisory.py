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
"""Strictly non-blocking advisory tool implementations."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from datacron.contradictions import (
    FrozenContradictionError,
    build_advisory_report,
    unavailable_advisory_report,
)
from datacron.mcp.tools.payloads import _LOGGER, _audit

__all__ = ["_contradiction_scan_impl"]


async def _contradiction_scan_impl() -> dict[str, Any]:
    """Replay the packaged cache without touching live application state."""
    started = time.perf_counter()
    try:
        payload = await asyncio.to_thread(build_advisory_report)
    except FrozenContradictionError:
        _LOGGER.warning("contradiction_scan frozen replay unavailable", exc_info=True)
        payload = unavailable_advisory_report()
    except Exception:
        _LOGGER.exception("contradiction_scan failed without affecting application state")
        payload = unavailable_advisory_report()
    _audit(
        "contradiction_scan",
        started,
        available=payload["available"],
        candidate_count=payload["candidate_count"],
        advisory_only=True,
    )
    return payload
