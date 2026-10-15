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
"""Content-free operator guidance derived from explicit diagnostics."""

from __future__ import annotations

from typing import Any, Final

from datacron.core.memory_protocol import FOLLOW_UP_MAX_RECORDS
from datacron.i18n import get_section, t

_RAW_ERROR_ACTIONS = get_section("guidance.errors")
ERROR_ACTIONS: Final[dict[str, str]] = {
    **_RAW_ERROR_ACTIONS,
    "follow_up_record_count_exceeded": t(
        "guidance.errors.follow_up_record_count_exceeded",
        max_records=FOLLOW_UP_MAX_RECORDS,
    ),
}


def health_guidance(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Explain measured health failures without inventing repair authority."""
    guidance = []
    if payload["recovery"].get("required"):
        guidance.append(
            {
                "code": "recovery_required",
                "severity": "blocking",
                "action": ERROR_ACTIONS["recovery_required"],
            }
        )
    if not payload["index"]["consistent_with_vault"]:
        guidance.append(
            {
                "code": "index_stale",
                "severity": "warning",
                "action": t("guidance.health.index_stale"),
            }
        )
    if payload["integrity"].get("id_mismatches"):
        guidance.append(
            {
                "code": "identity_mismatch",
                "severity": "warning",
                "action": t("guidance.health.identity_mismatch"),
            }
        )
    if not payload["durability"].get("effective_writes_enabled"):
        guidance.append(
            {
                "code": "writes_disabled",
                "severity": "information",
                "action": t("guidance.health.writes_disabled"),
            }
        )
    if payload["status"] != "healthy" and not guidance:
        guidance.append(
            {
                "code": "integrity_review",
                "severity": "warning",
                "action": t("guidance.health.integrity_review"),
            }
        )
    return guidance
