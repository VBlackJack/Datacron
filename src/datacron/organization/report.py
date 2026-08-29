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
"""Rendering of an organization plan.

Every operator-facing string in this feature lives here and nowhere else. The
JSON form is the contract other tools read; the text form is for the eye.
"""

from __future__ import annotations

import json
from typing import Any, Final

from datacron.organization.planner import DeviationKind, OrganizationPlan

__all__ = [
    "render_json",
    "render_text",
]

_SUMMARY_HEADING: Final[str] = "Organization report"
_NO_RULES_MESSAGE: Final[str] = (
    "No organization rules declared in .datacron/VAULT.yaml -- nothing to measure."
)
_CLEAN_MESSAGE: Final[str] = "No deviation found."
_SCHEMA_VERSION: Final[str] = "organization-plan-v1"


def _as_mapping(plan: OrganizationPlan) -> dict[str, Any]:
    """Build the stable, machine-readable shape of a plan."""
    return {
        "schema": _SCHEMA_VERSION,
        "vault_root": plan.vault_root,
        "scanned": plan.scanned,
        "governed": plan.governed,
        "unmatched": plan.unmatched,
        "counts": plan.counts_by_kind(),
        "deviations": [
            {
                "rel_path": item.rel_path,
                "kind": str(item.kind),
                "tag": item.tag,
                "detail": item.detail,
                "expected": item.expected,
            }
            for item in plan.deviations
        ],
        "skipped": [{"rel_path": item.rel_path, "reason": item.reason} for item in plan.skipped],
    }


def render_json(plan: OrganizationPlan) -> str:
    """Render a plan as deterministic JSON."""
    return json.dumps(_as_mapping(plan), indent=2, sort_keys=True, ensure_ascii=False)


def render_text(plan: OrganizationPlan) -> str:
    """Render a plan as a compact operator-facing report."""
    if plan.scanned == 0 and not plan.deviations:
        return _NO_RULES_MESSAGE

    lines: list[str] = [
        f"{_SUMMARY_HEADING} for {plan.vault_root}",
        f"  scanned {plan.scanned} notes, {plan.governed} governed, {plan.unmatched} out of scope",
    ]
    counts = plan.counts_by_kind()
    for kind in DeviationKind:
        lines.append(f"  {kind.value:<14} {counts[kind.value]}")
    if plan.skipped:
        lines.append(f"  skipped        {len(plan.skipped)}")

    if not plan.deviations:
        lines.append(_CLEAN_MESSAGE)
        return "\n".join(lines)

    lines.append("")
    for item in plan.deviations:
        target = f" -> {item.expected}" if item.expected is not None else ""
        lines.append(f"  {item.kind.value:<14} {item.rel_path} ({item.detail}){target}")
    return "\n".join(lines)
