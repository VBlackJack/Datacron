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
"""Read-only vault organization diagnosis.

This package measures the gap between a vault and the organization intent its
own sidecar declares. It never mutates the vault, and it deliberately depends
on neither the index nor the MCP surface: a report must stay obtainable on a
vault whose index is stale or absent.
"""

from __future__ import annotations

from datacron.organization.planner import (
    Deviation,
    DeviationKind,
    OrganizationPlan,
    SkippedNote,
    plan_organization,
)
from datacron.organization.report import render_json, render_text
from datacron.organization.rules import matches_naming, resolve_rule, rule_tags

__all__ = [
    "Deviation",
    "DeviationKind",
    "OrganizationPlan",
    "SkippedNote",
    "matches_naming",
    "plan_organization",
    "render_json",
    "render_text",
    "resolve_rule",
    "rule_tags",
]
