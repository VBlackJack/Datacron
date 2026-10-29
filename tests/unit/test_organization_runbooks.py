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


def _runbooks(*folders: str) -> RunbooksConfig:
    return RunbooksConfig.model_validate(
        {
            "roots": [{"folder": folder, "template": "infra"} for folder in folders or ("Ops",)],
            "templates": {"infra": _TEMPLATE},
        }
    )


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
_CHILDREN = {"Ops": ["mail-runbook", "dns-runbook", "notes", "-runbook"]}


def _resolve(subject: str, **overrides: Any) -> Any:
    return resolve_runbook(
        subject,
        overrides.get("runbooks", _runbooks()),
        overrides.get("organization", _ORGANIZATION),
        overrides.get("children", _CHILDREN),
        fold_case=overrides.get("fold_case", True),
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
    children = {"Ops": ["domain-name-system-runbook"]}

    location = _resolve("project/dns", children=children)

    assert location.directory == "Ops/domain-name-system-runbook"


def test_a_registered_subject_without_runbook_is_created_under_the_only_root() -> None:
    location = _resolve("project/dns", children={"Ops": []})

    assert (location.directory, location.slug, location.exists) == ("Ops/dns-runbook", "dns", False)


def test_a_folder_path_creates_a_runbook_without_a_registered_subject() -> None:
    location = _resolve("Ops/proxy-runbook", organization=None)

    assert (location.directory, location.exists) == ("Ops/proxy-runbook", False)


def test_an_unknown_word_never_creates_a_runbook() -> None:
    with pytest.raises(RunbookRoutingError, match="names no runbook and no registered subject"):
        _resolve("wsu")


def test_the_refusal_lists_the_known_runbooks() -> None:
    with pytest.raises(RunbookRoutingError, match="Ops/mail-runbook"):
        _resolve("wsu")


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("Other/mail-runbook", "not a runbook folder of a declared root"),
        ("Ops/notes", "not a runbook folder of a declared root"),
    ],
)
def test_a_folder_path_must_be_a_runbook_of_a_root(subject: str, fragment: str) -> None:
    with pytest.raises(RunbookRoutingError, match=fragment):
        _resolve(subject)


def test_an_ambiguous_subject_is_refused_with_its_candidates() -> None:
    organization = _organization({"tag": "project/mail"}, {"tag": "project/x", "aliases": ["mail"]})

    with pytest.raises(RunbookRoutingError, match="ambiguous"):
        _resolve("mail", organization=organization)


def test_one_slug_in_two_roots_is_refused() -> None:
    children = {"Ops": ["mail-runbook"], "Lab": ["mail-runbook"]}

    with pytest.raises(RunbookRoutingError, match="several runbooks"):
        _resolve("mail", runbooks=_runbooks("Ops", "Lab"), children=children)


def test_a_new_runbook_needs_a_path_when_several_roots_could_hold_it() -> None:
    with pytest.raises(RunbookRoutingError, match="several roots"):
        _resolve("project/dns", runbooks=_runbooks("Ops", "Lab"), children={})


def test_the_case_rule_follows_the_filesystem() -> None:
    with pytest.raises(RunbookRoutingError):
        _resolve("ops/MAIL-RUNBOOK", fold_case=False, organization=None)
    assert _resolve("ops/MAIL-RUNBOOK", organization=None).directory == "Ops/mail-runbook"


def test_an_empty_subject_is_refused() -> None:
    with pytest.raises(RunbookRoutingError, match="must not be empty"):
        _resolve("  ")


def test_candidate_lists_are_bounded() -> None:
    children = {"Ops": [f"r{index:02d}-runbook" for index in range(RUNBOOK_MAX_CANDIDATES + 3)]}

    with pytest.raises(RunbookRoutingError, match=r"\(and 3 more\)"):
        _resolve("nothing", children=children)


# --- roles and files ---------------------------------------------------------


def test_routing_and_an_explicit_role() -> None:
    template = _runbooks().templates["infra"]

    assert resolve_role("error_fix", None, template) == "troubleshooting"
    assert resolve_role("error_fix", "architecture", template) == "architecture"
    with pytest.raises(RunbookRoutingError, match="routes no role for kind 'command'"):
        resolve_role("command", None, template)
    with pytest.raises(RunbookRoutingError, match="not declared"):
        resolve_role("command", "build", template)


def test_a_role_file_is_found_whatever_its_number() -> None:
    location = _resolve("mail")

    found = resolve_role_file(
        location, "troubleshooting", ["00-architecture", "70-troubleshooting"]
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
