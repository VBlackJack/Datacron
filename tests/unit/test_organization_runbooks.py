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
"""Routing of a runbook capture: subject to runbook, kind to role, role to file."""

from __future__ import annotations

from typing import Any

import pytest

from datacron.core.config import OrganizationConfig, RunbooksConfig
from datacron.organization.runbooks import (
    RUNBOOK_MAX_CANDIDATES,
    RunbookRoutingError,
    quickref_rel_path,
    resolve_role,
    resolve_role_file,
    resolve_runbook,
)

_TEMPLATE: dict[str, Any] = {
    "language": "en",
    "readme": "README",
    "observation_label": "On site",
    "roles": {
        "architecture": {"stem": "00-architecture", "match": r"^\d{2}-architecture$"},
        "troubleshooting": {"stem": "50-troubleshooting", "match": r"^\d{2}-troubleshooting$"},
        "escalation": {"stem": "60-escalation", "match": r"^\d{2}-.*escalation$"},
    },
    "quickref": {"folder": "quickref", "language": "fr"},
    "routing": {"error_fix": "troubleshooting", "contact": "escalation"},
}
_RUNBOOKS = RunbooksConfig.model_validate({"templates": {"infra": _TEMPLATE}})


def _organization(*subjects: dict[str, Any]) -> OrganizationConfig:
    return OrganizationConfig.model_validate(
        {
            "scope": "_memory",
            "rules": [{"tag": "type/fact", "folder": "_memory/facts"}],
            "tags": {
                "placement_namespace": "type",
                "subject_namespace": "project",
                "subjects": list(subjects),
            },
        }
    )


_ORGANIZATION = _organization(
    {"tag": "project/mail"}, {"tag": "project/dns", "aliases": ["domain-name-system"]}
)
_CHILDREN = ["mail-runbook", "dns-runbook", "notes", "-runbook"]


def _resolve(subject: str, **overrides: Any) -> Any:
    return resolve_runbook(
        subject,
        overrides.get("runbooks", _RUNBOOKS),
        overrides.get("organization", _ORGANIZATION),
        vault_folder=overrides.get("vault_folder", "Ops"),
        children=overrides.get("children", _CHILDREN),
        fold_case=overrides.get("fold_case", True),
        create=overrides.get("create", False),
    )


@pytest.mark.parametrize("subject", ["project/mail", "mail", "MAIL", "Ops/mail-runbook"])
def test_a_subject_resolves_to_its_existing_runbook(subject: str) -> None:
    location = _resolve(subject)

    assert (location.directory, location.slug, location.exists) == (
        "Ops/mail-runbook",
        "mail",
        True,
    )
    assert location.template.language == "en"


def test_an_alias_finds_the_runbook_named_after_it() -> None:
    location = _resolve("project/dns", children=["domain-name-system-runbook"])

    assert location.directory == "Ops/domain-name-system-runbook"


def test_a_project_named_in_conversation_gets_a_runbook_only_on_request() -> None:
    """The name comes from the conversation; asking for creation keeps a typo from starting one."""
    with pytest.raises(RunbookRoutingError, match="create_runbook=true"):
        _resolve("billing")

    location = _resolve("Billing", create=True)

    assert (location.directory, location.slug, location.exists) == (
        "Ops/billing-runbook",
        "billing",
        False,
    )


def test_a_registered_subject_starts_its_runbook_under_its_own_name() -> None:
    location = _resolve("project/dns", children=[], create=True)

    assert (location.directory, location.slug, location.exists) == ("Ops/dns-runbook", "dns", False)


def test_a_folder_path_names_a_new_runbook() -> None:
    with pytest.raises(RunbookRoutingError, match="does not exist yet"):
        _resolve("Ops/proxy-runbook", organization=None)

    location = _resolve("Ops/proxy-runbook", organization=None, create=True)

    assert (location.directory, location.exists) == ("Ops/proxy-runbook", False)


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("Other/mail-runbook", "not a runbook folder of the runbook vault"),
        ("Ops/a/b-runbook", "not a runbook folder of the runbook vault"),
        ("Ops/notes", "not named like a runbook"),
    ],
)
def test_a_folder_path_must_be_a_runbook_folder_of_the_runbook_vault(
    subject: str, fragment: str
) -> None:
    with pytest.raises(RunbookRoutingError, match=fragment):
        _resolve(subject, create=True)


@pytest.mark.parametrize("subject", ["new project", "bad_name!", "-lead"])
def test_a_new_runbook_needs_a_usable_name(subject: str) -> None:
    with pytest.raises(RunbookRoutingError):
        _resolve(subject, create=True)


def test_the_refusal_lists_the_known_runbooks() -> None:
    with pytest.raises(RunbookRoutingError, match="Ops/mail-runbook"):
        _resolve("mai")


def test_an_ambiguous_subject_is_refused_with_its_candidates() -> None:
    organization = _organization({"tag": "project/mail"}, {"tag": "project/x", "aliases": ["mail"]})

    with pytest.raises(RunbookRoutingError, match="ambiguous"):
        _resolve("mail", organization=organization)


def test_the_case_rule_follows_the_filesystem() -> None:
    with pytest.raises(RunbookRoutingError):
        _resolve("ops/MAIL-RUNBOOK", fold_case=False, organization=None)
    assert _resolve("ops/MAIL-RUNBOOK", organization=None).directory == "Ops/mail-runbook"


def test_a_runbook_vault_at_the_vault_root_joins_without_a_folder() -> None:
    assert _resolve("mail", vault_folder="").directory == "mail-runbook"
    assert _resolve("billing", vault_folder="", create=True).directory == "billing-runbook"


def test_an_empty_subject_is_refused() -> None:
    with pytest.raises(RunbookRoutingError, match="must not be empty"):
        _resolve("  ")


def test_candidate_lists_are_bounded() -> None:
    children = [f"r{index:02d}-runbook" for index in range(RUNBOOK_MAX_CANDIDATES + 3)]

    with pytest.raises(RunbookRoutingError, match=r"\(and 3 more\)"):
        _resolve("nothing", children=children)


# --- roles and files ---------------------------------------------------------


def test_routing_and_an_explicit_role() -> None:
    template = _RUNBOOKS.template_for("mail")

    assert resolve_role("error_fix", None, template) == "troubleshooting"
    assert resolve_role("error_fix", "architecture", template) == "architecture"
    with pytest.raises(RunbookRoutingError, match="routes no role for kind 'command'"):
        resolve_role("command", None, template)
    with pytest.raises(RunbookRoutingError, match="not declared"):
        resolve_role("command", "build", template)


def test_a_role_file_is_found_whatever_its_number() -> None:
    found = resolve_role_file(
        _resolve("mail"), "troubleshooting", ["00-architecture", "70-troubleshooting"]
    )

    assert (found.rel_path, found.exists) == ("Ops/mail-runbook/70-troubleshooting.md", True)


def test_a_missing_role_file_takes_the_template_stem() -> None:
    found = resolve_role_file(_resolve("mail"), "escalation", ["00-architecture"])

    assert (found.rel_path, found.exists) == ("Ops/mail-runbook/60-escalation.md", False)


def test_two_files_playing_one_role_are_refused() -> None:
    with pytest.raises(RunbookRoutingError, match="several files"):
        resolve_role_file(_resolve("mail"), "escalation", ["60-escalation", "80-escalation"])


def test_a_quickref_sheet_path() -> None:
    assert quickref_rel_path(_resolve("mail"), "MAIL") == "Ops/mail-runbook/quickref/mail.md"
    with pytest.raises(RunbookRoutingError, match="lowercase name"):
        quickref_rel_path(_resolve("mail"), "../x")
