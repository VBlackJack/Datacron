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
    "observation_label": "On Mets",
    "skeleton_line": "State: skeleton.",
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
            "roots": [{"folder": folder, "template": "infra"} for folder in folders or ("CPDC",)],
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
    {"tag": "project/wsus"}, {"tag": "project/ad", "aliases": ["active-directory"]}
)
_CHILDREN = {"CPDC": ["wsus-runbook", "ad-runbook", "notes", "-runbook"]}


def _resolve(subject: str, **overrides: Any) -> Any:
    return resolve_runbook(
        subject,
        overrides.get("runbooks", _runbooks()),
        overrides.get("organization", _ORGANIZATION),
        overrides.get("children", _CHILDREN),
        fold_case=overrides.get("fold_case", True),
    )


@pytest.mark.parametrize("subject", ["project/wsus", "wsus", "WSUS", "CPDC/wsus-runbook"])
def test_a_subject_resolves_to_its_existing_runbook(subject: str) -> None:
    location = _resolve(subject)

    assert (location.directory, location.slug, location.exists) == (
        "CPDC/wsus-runbook",
        "wsus",
        True,
    )
    assert location.template.language == "en"


def test_an_alias_finds_the_runbook_named_after_it() -> None:
    children = {"CPDC": ["active-directory-runbook"]}

    location = _resolve("project/ad", children=children)

    assert location.directory == "CPDC/active-directory-runbook"


def test_a_registered_subject_without_runbook_is_created_under_the_only_root() -> None:
    location = _resolve("project/ad", children={"CPDC": []})

    assert (location.directory, location.slug, location.exists) == ("CPDC/ad-runbook", "ad", False)


def test_a_folder_path_creates_a_runbook_without_a_registered_subject() -> None:
    location = _resolve("CPDC/snc-runbook", organization=None)

    assert (location.directory, location.exists) == ("CPDC/snc-runbook", False)


def test_an_unknown_word_never_creates_a_runbook() -> None:
    with pytest.raises(RunbookRoutingError, match="names no runbook and no registered subject"):
        _resolve("wsu")


def test_the_refusal_lists_the_known_runbooks() -> None:
    with pytest.raises(RunbookRoutingError, match="CPDC/wsus-runbook"):
        _resolve("wsu")


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("Other/wsus-runbook", "not a runbook folder of a declared root"),
        ("CPDC/notes", "not a runbook folder of a declared root"),
    ],
)
def test_a_folder_path_must_be_a_runbook_of_a_root(subject: str, fragment: str) -> None:
    with pytest.raises(RunbookRoutingError, match=fragment):
        _resolve(subject)


def test_an_ambiguous_subject_is_refused_with_its_candidates() -> None:
    organization = _organization({"tag": "project/wsus"}, {"tag": "project/x", "aliases": ["wsus"]})

    with pytest.raises(RunbookRoutingError, match="ambiguous"):
        _resolve("wsus", organization=organization)


def test_one_slug_in_two_roots_is_refused() -> None:
    children = {"CPDC": ["wsus-runbook"], "Lab": ["wsus-runbook"]}

    with pytest.raises(RunbookRoutingError, match="several runbooks"):
        _resolve("wsus", runbooks=_runbooks("CPDC", "Lab"), children=children)


def test_a_new_runbook_needs_a_path_when_several_roots_could_hold_it() -> None:
    with pytest.raises(RunbookRoutingError, match="several roots"):
        _resolve("project/ad", runbooks=_runbooks("CPDC", "Lab"), children={})


def test_the_case_rule_follows_the_filesystem() -> None:
    with pytest.raises(RunbookRoutingError):
        _resolve("cpdc/WSUS-RUNBOOK", fold_case=False, organization=None)
    assert _resolve("cpdc/WSUS-RUNBOOK", organization=None).directory == "CPDC/wsus-runbook"


def test_an_empty_subject_is_refused() -> None:
    with pytest.raises(RunbookRoutingError, match="must not be empty"):
        _resolve("  ")


def test_candidate_lists_are_bounded() -> None:
    children = {"CPDC": [f"r{index:02d}-runbook" for index in range(RUNBOOK_MAX_CANDIDATES + 3)]}

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
    location = _resolve("wsus")

    found = resolve_role_file(
        location, "troubleshooting", ["00-architecture", "70-troubleshooting"]
    )

    assert (found.rel_path, found.exists) == ("CPDC/wsus-runbook/70-troubleshooting.md", True)


def test_a_missing_role_file_takes_the_template_stem() -> None:
    found = resolve_role_file(_resolve("wsus"), "escalation", ["00-architecture"])

    assert (found.rel_path, found.exists) == ("CPDC/wsus-runbook/60-escalation.md", False)


def test_two_files_playing_one_role_are_refused() -> None:
    with pytest.raises(RunbookRoutingError, match="several files"):
        resolve_role_file(_resolve("wsus"), "escalation", ["60-escalation", "80-escalation"])


def test_a_quickref_sheet_path() -> None:
    assert quickref_rel_path(_resolve("wsus"), "WSUS") == "CPDC/wsus-runbook/quickref/wsus.md"
    with pytest.raises(RunbookRoutingError, match="lowercase name"):
        quickref_rel_path(_resolve("wsus"), "../x")
