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
"""Per-type freshness of ``last_verified`` and its ``review_after_days`` setting."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from datacron.core.config import OrganizationConfig, OrganizationRule
from datacron.organization.freshness import (
    FreshnessStatus,
    assess_freshness,
    declares_review_windows,
)

_TODAY = date(2026, 10, 1)
_WINDOW = 30


def _config() -> OrganizationConfig:
    return OrganizationConfig(
        scope="_memory",
        rules=(
            OrganizationRule(
                tag="memory/decision", folder="_memory/decisions", review_after_days=90
            ),
            OrganizationRule(tag="memory/fact", folder="_memory/facts", review_after_days=_WINDOW),
            OrganizationRule(tag="memory/journal", folder="_memory/journal"),
        ),
    )


def _assess(tags: list[str], frontmatter: dict[str, object]) -> FreshnessStatus:
    return assess_freshness(tags, frontmatter, _config(), today=_TODAY).status


def test_age_equal_to_the_window_is_still_fresh() -> None:
    verdict = assess_freshness(
        ["memory/fact"], {"last_verified": "2026-09-01"}, _config(), today=_TODAY
    )

    assert verdict.status is FreshnessStatus.FRESH
    assert verdict.age_days == _WINDOW
    assert verdict.as_payload() == {
        "status": "fresh",
        "rule_tag": "memory/fact",
        "review_after_days": _WINDOW,
        "last_verified": "2026-09-01",
        "age_days": _WINDOW,
    }


def test_one_day_past_the_window_is_stale() -> None:
    verdict = assess_freshness(
        ["memory/fact"], {"last_verified": "2026-08-31"}, _config(), today=_TODAY
    )

    assert verdict.status is FreshnessStatus.STALE
    assert verdict.age_days == _WINDOW + 1


def test_yaml_dates_and_datetimes_are_read_like_strings() -> None:
    assert _assess(["memory/fact"], {"last_verified": date(2026, 9, 20)}) is FreshnessStatus.FRESH
    assert (
        _assess(["memory/fact"], {"last_verified": datetime(2026, 8, 1, 12, 0)})
        is FreshnessStatus.STALE
    )


def test_a_missing_verification_is_unverified_and_never_derived_from_updated() -> None:
    verdict = assess_freshness(
        ["memory/fact"], {"updated": "2026-09-30T10:00:00+00:00"}, _config(), today=_TODAY
    )

    assert verdict.status is FreshnessStatus.UNVERIFIED
    assert verdict.last_verified is None
    assert verdict.age_days is None
    assert _assess(["memory/fact"], {"last_verified": "  "}) is FreshnessStatus.UNVERIFIED


@pytest.mark.parametrize("value", ["2026-02-30", "soon", "2027-01-01", 42])
def test_an_unusable_or_future_date_is_invalid_and_never_echoed(value: object) -> None:
    verdict = assess_freshness(["memory/fact"], {"last_verified": value}, _config(), today=_TODAY)

    assert verdict.status is FreshnessStatus.INVALID
    assert verdict.last_verified is None


def test_one_day_ahead_is_clock_skew_not_a_typo() -> None:
    verdict = assess_freshness(
        ["memory/fact"], {"last_verified": "2026-10-02"}, _config(), today=_TODAY
    )

    assert verdict.status is FreshnessStatus.FRESH
    assert verdict.age_days == 0


def test_a_type_without_a_window_and_an_untyped_note_are_not_governed() -> None:
    typed = assess_freshness(
        ["memory/journal"], {"last_verified": "2020-01-01"}, _config(), today=_TODAY
    )
    untyped = assess_freshness(["kind/misc"], {}, _config(), today=_TODAY)

    assert typed.status is FreshnessStatus.NOT_GOVERNED
    assert typed.rule_tag == "memory/journal"
    assert typed.last_verified == "2020-01-01"
    assert untyped.status is FreshnessStatus.NOT_GOVERNED
    assert untyped.rule_tag is None
    assert assess_freshness(["memory/fact"], {}, None, today=_TODAY).status is (
        FreshnessStatus.NOT_GOVERNED
    )


def test_the_first_declared_rule_sets_the_window() -> None:
    """A note carrying two typed tags is judged by the rule placement uses."""
    verdict = assess_freshness(
        ["memory/fact", "memory/decision"],
        # 61 days: inside the decision window, outside the fact window.
        {"last_verified": "2026-08-01"},
        _config(),
        today=_TODAY,
    )

    assert verdict.rule_tag == "memory/decision"
    assert verdict.status is FreshnessStatus.FRESH


@pytest.mark.parametrize("value", [True, 0, -3, "30", 1.5])
def test_review_after_days_is_read_strictly(value: object) -> None:
    with pytest.raises(ValidationError, match="review_after_days"):
        OrganizationRule.model_validate(
            {"tag": "memory/fact", "folder": "_memory/facts", "review_after_days": value}
        )


def test_review_after_days_is_optional() -> None:
    assert OrganizationRule(tag="memory/fact", folder="_memory/facts").review_after_days is None


def test_a_verdict_is_attached_only_when_the_vault_declares_a_window() -> None:
    windowless = OrganizationConfig(
        scope="_memory", rules=(OrganizationRule(tag="memory/fact", folder="_memory/facts"),)
    )

    assert declares_review_windows(_config()) is True
    assert declares_review_windows(windowless) is False
    assert declares_review_windows(None) is False


def test_an_ungoverned_verdict_renders_as_its_status_alone() -> None:
    verdict = assess_freshness(
        ["kind/misc"], {"last_verified": "2026-09-01"}, _config(), today=_TODAY
    )

    assert verdict.as_payload() == {"status": "not_governed"}
