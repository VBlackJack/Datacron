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
from datetime import date
from typing import Final

from datacron.core.config import VALID_NAMING_TOKENS, OrganizationConfig, OrganizationRule

__all__ = [
    "expected_stem_pattern",
    "matches_naming",
    "resolve_rule",
    "rule_tags",
]

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{([^{}]*)\}")
_DATE_NAME: Final[str] = "date"
_ISO_DATE_NAME: Final[str] = "iso_date"
_ISO_DATE_PLACEHOLDER: Final[str] = "{iso_date}"
_ISO_DATE_GROUP_PREFIX: Final[str] = "iso_date_"
_ISO_DATE_PATTERN: Final[str] = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
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


def _expected_stem_pattern(
    naming: str,
    *,
    calendar_date: str | None = None,
) -> re.Pattern[str]:
    """Compile a naming template into an anchored structural pattern.

    Literal text between tokens is escaped, so a template stays a template and
    never becomes an accidental regular expression. Calendar validity is
    enforced separately by :func:`matches_naming`.
    """
    parts: list[str] = []
    cursor = 0
    iso_date_index = 0
    for match in _TOKEN_PATTERN.finditer(naming):
        parts.append(re.escape(naming[cursor : match.start()]))
        placeholder = match.group(1)
        if placeholder == _DATE_NAME:
            parts.append(
                re.escape(calendar_date) if calendar_date is not None else _NEVER_MATCH_PATTERN
            )
        elif placeholder == _ISO_DATE_NAME:
            group_name = f"{_ISO_DATE_GROUP_PREFIX}{iso_date_index}"
            parts.append(f"(?P<{group_name}>{_ISO_DATE_PATTERN})")
            iso_date_index += 1
        else:
            parts.append(_SLUG_PATTERN)
        cursor = match.end()
    parts.append(re.escape(naming[cursor:]))
    return re.compile(rf"\A{''.join(parts)}\Z")


def expected_stem_pattern(
    naming: str,
    *,
    calendar_date: str | None = None,
) -> re.Pattern[str]:
    """Compile the backward-compatible structural filename pattern.

    This public helper intentionally remains lexical. Call
    :func:`matches_naming` when calendar validity and validated template
    placement are required.
    """
    return _expected_stem_pattern(naming, calendar_date=calendar_date)


def matches_naming(
    stem: str,
    naming: str,
    *,
    calendar_date: str | None = None,
) -> bool:
    """Report whether a filename stem satisfies the declared naming contract.

    ``{date}`` keeps its exact lifecycle-date semantics. Every ``{iso_date}``
    occurrence is independent from lifecycle fields and must be a valid ASCII
    ``YYYY-MM-DD`` calendar date.
    """
    if not _is_supported_naming(naming):
        return False
    match = _expected_stem_pattern(naming, calendar_date=calendar_date).fullmatch(stem)
    if match is None:
        return False
    iso_dates = (
        value
        for name, value in match.groupdict().items()
        if name.startswith(_ISO_DATE_GROUP_PREFIX)
    )
    return all(value is not None and _is_valid_iso_date(value) for value in iso_dates)


def _is_supported_naming(naming: str) -> bool:
    """Return whether a direct helper call obeys the validated template contract."""
    token_occurrences = _TOKEN_PATTERN.findall(naming)
    if not set(token_occurrences).issubset(VALID_NAMING_TOKENS):
        return False
    iso_date_count = token_occurrences.count(_ISO_DATE_NAME)
    return not iso_date_count or (iso_date_count == 1 and naming.startswith(_ISO_DATE_PLACEHOLDER))


def _is_valid_iso_date(value: str) -> bool:
    """Return whether ``value`` is a real ISO calendar date."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def rule_tags(config: OrganizationConfig | None) -> Sequence[str]:
    """Return declared tags in priority order, for diagnostics and reporting."""
    if config is None:
        return ()
    return tuple(rule.tag for rule in config.rules)
