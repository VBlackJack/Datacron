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
"""The runbooks block of VAULT.yaml, its write setting and its write boundary."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from datacron.core.config import (
    RUNBOOK_EXCLUDED_DIRECTORIES,
    RunbooksConfig,
    Settings,
    VaultConfig,
    load_vault_config,
)
from datacron.core.runbook_scope import RunbookScopeError, admit_runbook_write

_ROOT = "CPDC"

_TEMPLATE: dict[str, Any] = {
    "language": "en",
    "readme": "README",
    "roles": {
        "architecture": {"stem": "00-architecture", "match": r"^\d{2}-architecture$"},
        "troubleshooting": {
            "stem": "50-troubleshooting",
            "match": r"^\d{2}-troubleshooting$",
            "entry": "symptom",
        },
        "escalation": {
            "stem": "60-escalation",
            "match": r"^\d{2}-.*escalation$",
            "entry": "contact",
        },
        "variables": {
            "stem": "90-variables",
            "match": r"^\d{2}-variables$",
            "entry": "table_row",
        },
        "measured": {"stem": "MEASURED", "match": "^MEASURED$", "optional": True},
    },
    "quickref": {"folder": "quickref", "language": "fr"},
    "observation_label": "On Mets",
    "labels": {"symptom": "Symptom", "fix": "Fix"},
    "skeleton_line": "State: skeleton. Nothing in this file is measured yet.",
    "routing": {"error_fix": "troubleshooting", "contact": "escalation", "server": "variables"},
}


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "roots": [{"folder": _ROOT, "template": "infra"}],
        "templates": {"infra": copy.deepcopy(_TEMPLATE)},
    }
    block.update(overrides)
    return block


def _template(**overrides: Any) -> dict[str, Any]:
    template = copy.deepcopy(_TEMPLATE)
    template.update(overrides)
    return template


def _refused(block: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError) as caught:
        RunbooksConfig.model_validate(block)
    assert fragment in str(caught.value)


# --- model -------------------------------------------------------------------


def test_a_reference_shaped_block_loads_from_vault_yaml(tmp_path: Path) -> None:
    sidecar = tmp_path / "VAULT.yaml"
    sidecar.write_text(
        "runbooks:\n"
        "  roots:\n"
        "    - folder: CPDC\n"
        "      template: infra\n"
        "  templates:\n"
        "    infra:\n"
        "      language: en\n"
        "      readme: README\n"
        "      observation_label: On Mets\n"
        "      skeleton_line: 'State: skeleton.'\n"
        "      roles:\n"
        "        troubleshooting:\n"
        "          stem: 50-troubleshooting\n"
        "          match: '^\\d{2}-troubleshooting$'\n"
        "          entry: symptom\n"
        "      routing:\n"
        "        error_fix: troubleshooting\n",
        encoding="utf-8",
    )

    config = load_vault_config(sidecar)

    assert config is not None
    assert config.runbooks is not None
    root = config.runbooks.roots[0]
    assert (root.folder, root.directory_naming, root.template) == (
        "CPDC",
        "{slug}-runbook",
        "infra",
    )
    template = config.runbooks.templates["infra"]
    assert template.routing == {"error_fix": "troubleshooting"}
    assert template.roles["troubleshooting"].entry == "symptom"


@pytest.mark.parametrize("value", [None, {}])
def test_an_absent_or_empty_block_means_no_runbooks(value: object) -> None:
    assert VaultConfig.model_validate({"runbooks": value}).runbooks is None
    assert VaultConfig.model_validate({}).runbooks is None


def test_a_role_is_found_by_its_pattern_whatever_its_number() -> None:
    """The AD runbook files troubleshooting at 70, WSUS at 50: both play the role."""
    role = RunbooksConfig.model_validate(_block()).templates["infra"].roles["troubleshooting"]

    assert role.matches("50-troubleshooting")
    assert role.matches("70-troubleshooting")
    assert not role.matches("50-troubleshooting-old")


def test_a_misspelled_key_is_refused_not_dropped() -> None:
    _refused(_block(templates={"infra": _template(skeleton="x")}), "skeleton")


@pytest.mark.parametrize("folder", ["knowledge", "Knowledge/pki", "knowledge/x/y"])
def test_a_root_in_the_read_only_folder_is_refused(folder: str) -> None:
    _refused(
        _block(roots=[{"folder": folder, "template": "infra"}]), "lies in a read-only vault folder"
    )


@pytest.mark.parametrize("excluded", sorted(RUNBOOK_EXCLUDED_DIRECTORIES))
def test_a_root_inside_an_excluded_directory_is_refused(excluded: str) -> None:
    _refused(
        _block(roots=[{"folder": f"CPDC/{excluded.upper()}", "template": "infra"}]),
        "never write into",
    )


@pytest.mark.parametrize("folder", ["/CPDC", "G:/CPDC", "CPDC/../knowledge", ""])
def test_a_root_must_be_a_plain_vault_relative_folder(folder: str) -> None:
    with pytest.raises(ValidationError):
        RunbooksConfig.model_validate(_block(roots=[{"folder": folder, "template": "infra"}]))


@pytest.mark.parametrize(
    ("naming", "fragment"),
    [
        ("{slug}", "needs a literal part"),
        ("{slug}-{slug}", "exactly once"),
        ("{date}-runbook", "exactly once"),
        ("runbook", "exactly once"),
        ("runbooks/{slug}", "single folder or file name"),
    ],
)
def test_directory_naming_bounds_the_write_scope(naming: str, fragment: str) -> None:
    _refused(
        _block(roots=[{"folder": _ROOT, "directory_naming": naming, "template": "infra"}]),
        fragment,
    )


def test_roots_must_name_a_declared_template_and_must_not_overlap() -> None:
    _refused(_block(roots=[{"folder": _ROOT, "template": "other"}]), "undeclared template")
    _refused(
        _block(
            roots=[
                {"folder": _ROOT, "template": "infra"},
                {"folder": "cpdc/sub", "template": "infra"},
            ]
        ),
        "overlaps another declared root",
    )


def test_roots_and_templates_must_not_be_empty() -> None:
    _refused(_block(roots=[]), "non-empty list")
    _refused(_block(templates={}), "non-empty mapping")


@pytest.mark.parametrize(
    ("role", "fragment"),
    [
        ({"stem": "50-troubleshooting.md", "match": ".*"}, "suffix"),
        ({"stem": "50-troubleshooting", "match": r"^\d{2}-faq$"}, "does not match its own"),
        ({"stem": "confluence-source", "match": ".*"}, "never write into"),
        ({"stem": "a/b", "match": ".*"}, "single folder or file name"),
        ({"stem": "50-x", "match": "(a+)+$"}, "catastrophic"),
        ({"stem": "50-x", "match": "("}, "not a valid regex"),
        ({"stem": "50-x", "match": ".*", "entry": "prose"}, "entry must be one of"),
        ({"stem": "50-x", "match": ".*", "optional": "yes"}, "true or false"),
    ],
)
def test_a_role_is_validated_before_it_can_route_a_write(
    role: dict[str, Any], fragment: str
) -> None:
    _refused(_block(templates={"infra": _template(roles={"extra": role})}), fragment)


def test_a_stem_claimed_by_two_roles_is_refused() -> None:
    roles = {
        "maintenance": {"stem": "40-maintenance", "match": r"^\d{2}-.*$"},
        "build": {"stem": "10-build", "match": r"^\d{2}-build$"},
    }
    _refused(_block(templates={"infra": _template(roles=roles)}), "matched by several roles")


def test_two_roles_cannot_share_a_stem_or_the_readme() -> None:
    _refused(
        _block(
            templates={"infra": _template(roles={"x": {"stem": "README", "match": "^README$"}})}
        ),
        "reuses the file stem",
    )


def test_routing_names_known_kinds_and_declared_roles() -> None:
    _refused(_block(templates={"infra": _template(routing={"rumour": "escalation"})}), "rumour")
    _refused(
        _block(templates={"infra": _template(routing={"contact": "nowhere"})}), "undeclared role"
    )


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"language": "English"}, "language tag"),
        ({"observation_label": "On\nMets"}, "single line"),
        ({"skeleton_line": "  "}, "must not be empty"),
        ({"labels": {"Fix": "Fix"}}, "lowercase identifier"),
        ({"quickref": {"folder": "quickref", "language": "fr", "naming": "sheet"}}, "exactly once"),
        ({"quickref": {"folder": "README", "language": "fr"}}, "collides with a file stem"),
    ],
)
def test_template_texts_and_quickref_are_validated(
    overrides: dict[str, Any], fragment: str
) -> None:
    _refused(_block(templates={"infra": _template(**overrides)}), fragment)


# --- setting -----------------------------------------------------------------


def test_runbook_write_paths_is_its_own_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("DATACRON_RUNBOOK_WRITE_PATHS", os.pathsep.join([str(first), str(second)]))

    settings = Settings()

    assert settings.runbook_write_paths == [first.resolve(), second.resolve()]
    assert settings.write_paths == []


def test_runbook_write_paths_defaults_to_nothing_writable() -> None:
    assert Settings().runbook_write_paths == []


# --- write boundary ----------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / _ROOT / "wsus-runbook").mkdir(parents=True)
    return root


def _admit(vault: Path, rel_path: str, write_paths: list[Path] | None = None) -> Any:
    return admit_runbook_write(
        rel_path,
        vault_root=vault,
        runbooks=RunbooksConfig.model_validate(_block()),
        write_paths=[vault] if write_paths is None else write_paths,
    )


def test_a_file_of_a_declared_runbook_is_admitted(vault: Path) -> None:
    target = _admit(vault, "CPDC/wsus-runbook/50-troubleshooting.md")

    assert target.slug == "wsus"
    assert target.directory == "CPDC/wsus-runbook"
    assert target.relative == "50-troubleshooting.md"
    assert target.resolved == (vault / "CPDC/wsus-runbook/50-troubleshooting.md").resolve()


def test_a_quickref_sheet_is_admitted(vault: Path) -> None:
    assert _admit(vault, "CPDC/wsus-runbook/quickref/wsus.md").relative == "quickref/wsus.md"


@pytest.mark.parametrize("excluded", sorted(RUNBOOK_EXCLUDED_DIRECTORIES))
@pytest.mark.parametrize("spelling", [str.lower, str.upper])
def test_excluded_directories_are_refused_in_any_case(
    vault: Path, excluded: str, spelling: Any
) -> None:
    with pytest.raises(RunbookScopeError, match="never write into"):
        _admit(vault, f"CPDC/wsus-runbook/{spelling(excluded)}/page.md")


def test_the_read_only_folder_is_refused_even_inside_the_write_paths(vault: Path) -> None:
    with pytest.raises(RunbookScopeError, match="read-only folder"):
        _admit(vault, "knowledge/pki/14-depannage.md")


def test_a_traversal_is_judged_after_resolution(vault: Path) -> None:
    with pytest.raises(RunbookScopeError, match="read-only folder"):
        _admit(vault, "CPDC/wsus-runbook/../../knowledge/x.md")


@pytest.mark.parametrize(
    ("rel_path", "fragment"),
    [
        ("CPDC/wsus-notes/50-troubleshooting.md", "not named like a runbook"),
        ("CPDC/-runbook/50-troubleshooting.md", "not named like a runbook"),
        ("CPDC/README.md", "not inside a declared runbook root"),
        ("_drafts/wsus-runbook/50-troubleshooting.md", "not inside a declared runbook root"),
        ("CPDC/wsus-runbook/scripts/run.ps1", "not a Markdown file"),
    ],
)
def test_only_markdown_inside_a_runbook_directory_is_admitted(
    vault: Path, rel_path: str, fragment: str
) -> None:
    with pytest.raises(RunbookScopeError, match=fragment):
        _admit(vault, rel_path)


def test_the_runbook_write_paths_bound_every_write(vault: Path, tmp_path: Path) -> None:
    with pytest.raises(RunbookScopeError, match="No write paths are configured"):
        _admit(vault, "CPDC/wsus-runbook/50-troubleshooting.md", write_paths=[])
    with pytest.raises(RunbookScopeError, match="outside the allowed write roots"):
        _admit(
            vault,
            "CPDC/wsus-runbook/50-troubleshooting.md",
            write_paths=[vault / "_drafts"],
        )
    outside = tmp_path / "elsewhere" / "wsus-runbook" / "x.md"
    with pytest.raises(RunbookScopeError, match="outside the vault"):
        _admit(vault, str(outside), write_paths=[tmp_path])


def test_a_write_path_narrower_than_the_root_admits_only_its_runbook(vault: Path) -> None:
    only_wsus = [vault / _ROOT / "wsus-runbook"]

    assert _admit(vault, "CPDC/wsus-runbook/00-architecture.md", write_paths=only_wsus)
    with pytest.raises(RunbookScopeError, match="outside the allowed write roots"):
        _admit(vault, "CPDC/ad-runbook/00-architecture.md", write_paths=only_wsus)
