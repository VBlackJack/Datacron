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
"""Rule resolution and naming templates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from datacron.core.config import OrganizationConfig, OrganizationRule, VaultConfig
from datacron.organization.rules import matches_naming, resolve_rule, rule_tags


def _config(*pairs: tuple[str, str]) -> OrganizationConfig:
    return OrganizationConfig(
        scope="_memory",
        rules=tuple(OrganizationRule(tag=tag, folder=folder) for tag, folder in pairs),
    )


def test_first_declared_rule_wins_when_several_tags_match() -> None:
    """Declaration order is the tie-break, and it must be the only one.

    Real notes carry both tags at once; this is the case that decides whether
    the feature is trustworthy.
    """
    config = _config(("memory/decision", "_memory/decisions"), ("memory/fact", "_memory/facts"))

    resolved = resolve_rule(["memory/fact", "memory/decision"], config)

    assert resolved is not None
    assert resolved.tag == "memory/decision"


def test_reordering_rules_reverses_the_winner() -> None:
    """The vault owner controls the tie-break by reordering, without code."""
    config = _config(("memory/fact", "_memory/facts"), ("memory/decision", "_memory/decisions"))

    resolved = resolve_rule(["memory/fact", "memory/decision"], config)

    assert resolved is not None
    assert resolved.tag == "memory/fact"


def test_tag_order_on_the_note_does_not_matter() -> None:
    config = _config(("memory/decision", "_memory/decisions"), ("memory/fact", "_memory/facts"))

    first = resolve_rule(["memory/decision", "memory/fact"], config)
    second = resolve_rule(["memory/fact", "memory/decision"], config)

    assert first == second


def test_note_without_matching_tag_resolves_to_nothing() -> None:
    config = _config(("memory/fact", "_memory/facts"))

    assert resolve_rule(["project/datacron"], config) is None


def test_absent_configuration_resolves_to_nothing() -> None:
    assert resolve_rule(["memory/fact"], None) is None


@pytest.mark.parametrize(
    ("stem", "naming", "calendar_date", "expected"),
    [
        ("2026-08-29-release-notes", "{date}-{slug}", "2026-08-29", True),
        ("2026-08-28-release-notes", "{date}-{slug}", "2026-08-29", False),
        ("release-notes", "{date}-{slug}", "2026-08-29", False),
        ("2026-08-29", "{date}-{slug}", "2026-08-29", False),
        ("heimdall", "{slug}", None, True),
        ("heimdall-release-v2026.072401", "{slug}", None, True),
        ("2026-8-9-short-date", "{date}-{slug}", "2026-08-09", False),
    ],
)
def test_naming_templates(
    stem: str,
    naming: str,
    calendar_date: str | None,
    expected: bool,
) -> None:
    assert matches_naming(stem, naming, calendar_date=calendar_date) is expected


def test_date_template_never_matches_without_a_frontmatter_date() -> None:
    assert matches_naming("2026-08-29-release-notes", "{date}-{slug}") is False


def test_literal_text_in_a_template_is_not_a_regex() -> None:
    """A dot in a template matches a dot, never any character."""
    assert matches_naming("note.md-x", "note.md-{slug}") is True
    assert matches_naming("noteXmd-x", "note.md-{slug}") is False


def test_unknown_naming_token_is_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError, match="unknown token"):
        OrganizationRule(tag="memory/fact", folder="_memory/facts", naming="{author}-{slug}")


def test_duplicate_tags_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate organization rule"):
        _config(("memory/fact", "_memory/facts"), ("memory/fact", "_memory/other"))


def test_active_rules_require_an_explicit_scope() -> None:
    with pytest.raises(ValidationError, match="scope is required"):
        OrganizationConfig(rules=(OrganizationRule(tag="memory/fact", folder="_memory/facts"),))


def test_empty_organization_configuration_remains_inert() -> None:
    assert OrganizationConfig().scope is None
    assert OrganizationConfig().rules == ()
    assert VaultConfig.model_validate({"organization": {}}).organization is None


@pytest.mark.parametrize("organization", [[], False, 0, ""])
def test_false_like_non_mapping_organization_is_rejected(organization: object) -> None:
    with pytest.raises(ValidationError):
        VaultConfig.model_validate({"organization": organization})


@pytest.mark.parametrize("folder", ["../escape", "/absolute", "C:/drive", "a/../b"])
def test_folder_must_stay_inside_the_vault(folder: str) -> None:
    with pytest.raises(ValidationError):
        OrganizationRule(tag="memory/fact", folder=folder)


@pytest.mark.parametrize("scope", ["../escape", "/absolute", "C:/drive", "a/../b", "."])
def test_scope_must_stay_inside_the_vault(scope: str) -> None:
    with pytest.raises(ValidationError):
        OrganizationConfig(scope=scope)


def test_unknown_rule_key_is_rejected_rather_than_ignored() -> None:
    """A typo must fail loudly instead of silently disabling the rule."""
    with pytest.raises(ValidationError):
        OrganizationRule(tag="memory/fact", foldr="_memory/facts")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "payload",
    [
        {"tag": None, "folder": "_memory/facts"},
        {"tag": ["memory/fact"], "folder": "_memory/facts"},
        {"tag": "memory/fact", "folder": 42},
        {"tag": "memory/fact", "folder": "_memory/facts", "naming": None},
    ],
)
def test_rule_string_fields_reject_non_strings(payload: dict[str, object]) -> None:
    """A YAML type error must fail instead of silently changing rule meaning."""
    with pytest.raises(ValidationError, match="must be a string"):
        OrganizationRule.model_validate(payload)


@pytest.mark.parametrize("scope", [["_memory"], 42, False])
def test_scope_rejects_non_strings(scope: object) -> None:
    with pytest.raises(ValidationError, match="must be a string"):
        OrganizationConfig.model_validate({"scope": scope})


def test_max_kb_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        OrganizationRule(tag="memory/fact", folder="_memory/facts", max_kb=0)


def test_rule_tags_reports_priority_order() -> None:
    config = _config(("memory/decision", "_memory/decisions"), ("memory/fact", "_memory/facts"))

    assert rule_tags(config) == ("memory/decision", "memory/fact")
    assert rule_tags(None) == ()


def test_backslash_folder_is_normalized_to_posix() -> None:
    rule = OrganizationRule(tag="memory/fact", folder="_memory\\facts")

    assert rule.folder == "_memory/facts"


def test_backslash_scope_is_normalized_to_posix() -> None:
    assert OrganizationConfig(scope="_memory\\projects").scope == "_memory/projects"


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["memory/project", "memory/decision"], "memory/project"),
        (["memory/fact", "memory/decision"], "memory/fact"),
        (["memory/project", "memory/fact", "memory/decision"], "memory/project"),
        (["memory/session", "memory/decision"], "memory/session"),
        (["memory/decision"], "memory/decision"),
    ],
)
def test_declared_primary_tag_priority(tags: list[str], expected: str) -> None:
    config = _config(
        ("memory/preference", "_memory/preferences"),
        ("memory/contact", "_memory/people"),
        ("memory/session", "_memory/sessions"),
        ("memory/project", "_memory/projects"),
        ("memory/fact", "_memory/facts"),
        ("memory/decision", "_memory/decisions"),
    )

    resolved = resolve_rule(tags, config)

    assert resolved is not None
    assert resolved.tag == expected
