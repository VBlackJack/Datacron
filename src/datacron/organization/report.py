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
from typing import Final

from datacron.organization.planner import (
    DeviationKind,
    OrganizationPlan,
    organization_plan_mapping,
)

__all__ = [
    "render_json",
    "render_text",
]

_SUMMARY_HEADING: Final[str] = "Organization report"
_NO_RULES_MESSAGE: Final[str] = (
    "No organization rules declared in .datacron/VAULT.yaml -- nothing to measure."
)
_CLEAN_MESSAGE: Final[str] = "No deviation found."
_FRESHNESS_HEADING: Final[str] = "Freshness (older than {days} days): {count}"
_FRESHNESS_MISSING: Final[str] = "never verified"
_FRESHNESS_AGE: Final[str] = "{age} days"
_KIND_COLUMN_WIDTH: Final[int] = max(len(kind.value) for kind in DeviationKind)


def render_json(plan: OrganizationPlan) -> str:
    """Render a plan as deterministic JSON.

    The document is the same mapping the manifest binding hashes, so a CLI
    report and a bundle projection never disagree on shape.
    """
    return json.dumps(
        organization_plan_mapping(plan),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


def _freshness_lines(plan: OrganizationPlan) -> list[str]:
    """Render the informative freshness block, when the plan carries one."""
    if plan.freshness is None or plan.freshness_days is None:
        return []
    lines = [_FRESHNESS_HEADING.format(days=plan.freshness_days, count=len(plan.freshness))]
    for item in plan.freshness:
        age = (
            _FRESHNESS_MISSING
            if item.age_days is None
            else _FRESHNESS_AGE.format(age=item.age_days)
        )
        lines.append(f"  {item.rel_path} ({item.tag}, {age})")
    return lines


def render_text(plan: OrganizationPlan) -> str:
    """Render a plan as a compact operator-facing report."""
    if plan.scope is None:
        return "\n".join([_NO_RULES_MESSAGE, *_freshness_lines(plan)])

    lines: list[str] = [
        f"{_SUMMARY_HEADING} for {plan.vault_root}",
        f"  scanned {plan.scanned} notes, {plan.governed} governed, {plan.unmatched} out of scope",
    ]
    counts = plan.counts_by_kind()
    for kind in DeviationKind:
        lines.append(f"  {kind.value:<{_KIND_COLUMN_WIDTH}} {counts[kind.value]}")
    if plan.skipped:
        lines.append(f"  {'skipped':<{_KIND_COLUMN_WIDTH}} {len(plan.skipped)}")
    lines.extend(_freshness_lines(plan))

    if not plan.deviations:
        lines.append(_CLEAN_MESSAGE)
        return "\n".join(lines)

    lines.append("")
    for item in plan.deviations:
        target = f" -> {item.expected}" if item.expected is not None else ""
        lines.append(
            f"  {item.kind.value:<{_KIND_COLUMN_WIDTH}} {item.rel_path} ({item.detail}){target}"
        )
    return "\n".join(lines)
