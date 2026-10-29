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
"""Pure per-type freshness assessment of a note's ``last_verified`` date.

A note's type is the organization rule that governs it, resolved exactly as
placement resolves it, and the vault declares how long a verification of that
type stays good through the rule's ``review_after_days``. This module reads
nothing and writes nothing: retrieval tools attach its verdict to what they
return, and only a caller who has re-verified a note ever moves the date.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final, final

from datacron.core.config import OrganizationConfig
from datacron.organization.rules import resolve_rule
from datacron.organization.subject_state import LAST_VERIFIED_KEY, frontmatter_day

__all__ = [
    "FreshnessAssessment",
    "FreshnessStatus",
    "assess_freshness",
    "declares_review_windows",
    "utc_today",
]

# A date written in a timezone ahead of UTC can be one calendar day past the
# UTC run date. Anything further ahead is a typo, not a verification.
_CLOCK_SKEW_DAYS: Final[int] = 1


class FreshnessStatus(StrEnum):
    """Verdict on whether a note's last verification is still within its window."""

    FRESH = "fresh"
    STALE = "stale"
    # Governed by a window but never verified. Deliberately not ``stale``, and
    # never derived from ``updated``: an edit is not a verification.
    UNVERIFIED = "unverified"
    # A ``last_verified`` value that is not a usable date, or lies in the future.
    INVALID = "invalid"
    # No rule governs the note, or its rule declares no review window.
    NOT_GOVERNED = "not_governed"


@final
@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """One note's freshness verdict and the facts it was derived from."""

    status: FreshnessStatus
    rule_tag: str | None
    review_after_days: int | None
    last_verified: str | None
    age_days: int | None

    def as_payload(self) -> dict[str, Any]:
        """Render the verdict as the JSON mapping retrieval tools return.

        An ungoverned note renders as its status alone. Every returned source
        pays for this field out of a fixed budget, and a vault that declares no
        review window must not lose a source to fields that are all null.
        """
        if self.status is FreshnessStatus.NOT_GOVERNED:
            return {"status": self.status.value}
        return {
            "status": self.status.value,
            "rule_tag": self.rule_tag,
            "review_after_days": self.review_after_days,
            "last_verified": self.last_verified,
            "age_days": self.age_days,
        }


def declares_review_windows(config: OrganizationConfig | None) -> bool:
    """True when at least one rule of the vault declares ``review_after_days``.

    Retrieval attaches no verdict at all to a vault that declares none: the field
    is paid for out of a fixed response budget, and even a status-only mapping is
    enough to push a source out of a tight ``session_context``.
    """
    return config is not None and any(rule.review_after_days is not None for rule in config.rules)


def utc_today() -> date:
    """The run date every freshness age is measured against."""
    return datetime.now(UTC).date()


def assess_freshness(
    tags: Iterable[str],
    frontmatter: Mapping[str, object],
    config: OrganizationConfig | None,
    *,
    today: date,
) -> FreshnessAssessment:
    """Judge a note's ``last_verified`` against the window of its type.

    Args:
        tags: The note's tags, used to resolve its governing rule.
        frontmatter: The note's parsed frontmatter.
        config: The vault's organization policy, or ``None`` when it has none.
        today: The run date; injected so a verdict never depends on the clock.

    Returns:
        The verdict. An unparseable or future date is reported as ``invalid``
        instead of raising, so one hand-typed value cannot fail a whole read.
    """
    raw_value = frontmatter.get(LAST_VERIFIED_KEY)
    rule = resolve_rule(tags, config)
    rule_tag = None if rule is None else rule.tag
    window = None if rule is None else rule.review_after_days
    # Only a parsed date is ever echoed: the raw value is hand-typed vault text
    # and is returned without the redaction that guards every other field.
    day = None if raw_value is None else frontmatter_day(raw_value)
    if window is None:
        return FreshnessAssessment(
            status=FreshnessStatus.NOT_GOVERNED,
            rule_tag=rule_tag,
            review_after_days=None,
            last_verified=day,
            age_days=None,
        )
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return FreshnessAssessment(
            status=FreshnessStatus.UNVERIFIED,
            rule_tag=rule_tag,
            review_after_days=window,
            last_verified=None,
            age_days=None,
        )
    age_days = None if day is None else (today - date.fromisoformat(day)).days
    if day is None or age_days is None or age_days < -_CLOCK_SKEW_DAYS:
        return FreshnessAssessment(
            status=FreshnessStatus.INVALID,
            rule_tag=rule_tag,
            review_after_days=window,
            last_verified=None,
            age_days=None,
        )
    age_days = max(age_days, 0)
    return FreshnessAssessment(
        status=FreshnessStatus.STALE if age_days > window else FreshnessStatus.FRESH,
        rule_tag=rule_tag,
        review_after_days=window,
        last_verified=day,
        age_days=age_days,
    )
