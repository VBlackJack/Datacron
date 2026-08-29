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
"""Pure resolution of organization rules against a note's frontmatter tags.

This module performs no I/O and holds no state. It answers two questions and
nothing else: which rule governs a note, and does a filename satisfy that
rule's naming template.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from datacron.core.config import OrganizationConfig, OrganizationRule

__all__ = [
    "expected_stem_pattern",
    "matches_naming",
    "resolve_rule",
    "rule_tags",
]

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{([^{}]*)\}")
# A slug is whatever the vault owner already writes: word characters, dots and
# separators. Deliberately permissive -- this lot reports naming shape, it does
# not police vocabulary.
_SLUG_PATTERN: Final[str] = r"[^/\\]+"
_NEVER_MATCH_PATTERN: Final[str] = r"(?!)"


def resolve_rule(
    tags: Iterable[str],
    config: OrganizationConfig | None,
) -> OrganizationRule | None:
    """Return the rule governing a note, or ``None`` when no rule applies.

    Declaration order is priority order: the first rule whose tag is present on
    the note wins and the search stops. A note carrying both ``memory/decision``
    and ``memory/fact`` is therefore governed by whichever the vault owner
    declared first, which is the only tie-break that stays visible in the
    sidecar without reading this code.

    A note matching no rule is *not* a deviation. It is out of scope, and the
    caller must never invent a placement for it.
    """
    if config is None:
        return None
    present = set(tags)
    for rule in config.rules:
        if rule.tag in present:
            return rule
    return None


def expected_stem_pattern(
    naming: str,
    *,
    calendar_date: str | None = None,
) -> re.Pattern[str]:
    """Compile a naming template into an anchored filename-stem pattern.

    Literal text between tokens is escaped, so a template stays a template and
    never becomes an accidental regular expression.
    """
    parts: list[str] = []
    cursor = 0
    for match in _TOKEN_PATTERN.finditer(naming):
        parts.append(re.escape(naming[cursor : match.start()]))
        placeholder = match.group(1)
        if placeholder == "date":
            parts.append(
                re.escape(calendar_date) if calendar_date is not None else _NEVER_MATCH_PATTERN
            )
        else:
            parts.append(_SLUG_PATTERN)
        cursor = match.end()
    parts.append(re.escape(naming[cursor:]))
    return re.compile(rf"\A{''.join(parts)}\Z")


def matches_naming(
    stem: str,
    naming: str,
    *,
    calendar_date: str | None = None,
) -> bool:
    """Report whether a filename stem satisfies ``naming`` and its exact date."""
    return expected_stem_pattern(naming, calendar_date=calendar_date).fullmatch(stem) is not None


def rule_tags(config: OrganizationConfig | None) -> Sequence[str]:
    """Return declared tags in priority order, for diagnostics and reporting."""
    if config is None:
        return ()
    return tuple(rule.tag for rule in config.rules)
