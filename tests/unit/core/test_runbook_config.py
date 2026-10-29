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
"""The runbooks block of VAULT.yaml, the runbook vault setting and its write boundary."""

from __future__ import annotations

import copy
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
from datacron.core.runbook_scope import (
    RunbookScopeError,
    admit_runbook_write,
    runbook_vault_folder,
)

_RUNBOOK_VAULT = "Ops"

_TEMPLATE: dict[str, Any] = {
    "language": "en",
    "readme": "README",
    "roles": {
        "architecture": {"stem": "00-architecture", "match": r"^\d{2}-architecture$"},
        "troubleshooting": {"stem": "50-troubleshooting", "match": r"^\d{2}-troubleshooting$"},
        "escalation": {"stem": "60-escalation", "match": r"^\d{2}-.*escalation$"},
        "variables": {"stem": "90-variables", "match": r"^\d{2}-variables$"},
        "measured": {"stem": "MEASURED", "match": "^MEASURED$", "title": "Measured state"},
    },
    "quickref": {"folder": "quickref", "language": "fr"},
    "observation_label": "On site",
    "labels": {"symptom": "Symptom", "fix": "Fix"},
    "routing": {"error_fix": "troubleshooting", "contact": "escalation", "server": "variables"},
}


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {"templates": {"infra": copy.deepcopy(_TEMPLATE)}}
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


def test_a_block_loads_from_vault_yaml(tmp_path: Path) -> None:
    sidecar = tmp_path / "VAULT.yaml"
    sidecar.write_text(
        "runbooks:\n"
        "  templates:\n"
        "    infra:\n"
        "      language: en\n"
        "      readme: README\n"
        "      observation_label: On site\n"
        "      roles:\n"
        "        troubleshooting:\n"
        "          stem: 50-troubleshooting\n"
        "          match: '^\\d{2}-troubleshooting$'\n"
        "          title: Troubleshooting\n"
        "      routing:\n"
        "        error_fix: troubleshooting\n",
        encoding="utf-8",
    )

    config = load_vault_config(sidecar)

    assert config is not None
    assert config.runbooks is not None
    assert config.runbooks.directory_naming == "{slug}-runbook"
    template = config.runbooks.template_for("anything")
    assert template.routing == {"error_fix": "troubleshooting"}
    assert template.roles["troubleshooting"].title == "Troubleshooting"


@pytest.mark.parametrize("value", [None, {}])
def test_an_absent_or_empty_block_means_no_runbooks(value: object) -> None:
    assert VaultConfig.model_validate({"runbooks": value}).runbooks is None
    assert VaultConfig.model_validate({}).runbooks is None


def test_the_former_roots_key_is_refused_not_dropped() -> None:
    """Roots are gone: the runbook vault is a setting, so a stale block fails loudly."""
    _refused(_block(roots=[{"folder": "Ops", "template": "infra"}]), "roots")


def test_a_role_is_found_by_its_pattern_whatever_its_number() -> None:
    """One runbook files troubleshooting at 70, another at 50: both play the role."""
    role = RunbooksConfig.model_validate(_block()).templates["infra"].roles["troubleshooting"]

    assert role.matches("50-troubleshooting")
    assert role.matches("70-troubleshooting")
    assert not role.matches("50-troubleshooting-old")


def test_a_misspelled_key_is_refused_not_dropped() -> None:
    _refused(_block(templates={"infra": _template(skeleton="x")}), "skeleton")


def test_the_template_of_a_runbook_is_its_assignment_else_the_default() -> None:
    config = RunbooksConfig.model_validate(
        _block(
            templates={
                "infra": _template(),
                "lab": _template(observation_label="In the lab"),
            },
            default_template="infra",
            assignments={"bench": "lab"},
        )
    )

    assert config.template_for("bench").observation_label == "In the lab"
    assert config.template_for("mail").observation_label == "On site"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"templates": {"a": _TEMPLATE, "b": _TEMPLATE}}, "default_template is required"),
        ({"default_template": "other"}, "undeclared template"),
        ({"assignments": {"mail": "other"}}, "undeclared template"),
        ({"assignments": {"Mail Server": "infra"}}, "runbook slug"),
        ({"templates": {}}, "non-empty mapping"),
    ],
)
def test_templates_defaults_and_assignments_are_checked(
    overrides: dict[str, Any], fragment: str
) -> None:
    _refused(_block(**overrides), fragment)


def test_no_path_is_protected_unless_the_vault_declares_it() -> None:
    """The package ships no vault-specific name; only the generic exclusions are fixed."""
    config = RunbooksConfig.model_validate(_block())

    assert config.protected_paths == ()
    assert not config.protects(("reference", "pki", "x.md"))


def test_protected_paths_are_relative_paths() -> None:
    config = RunbooksConfig.model_validate(_block(protected_paths=["reference/", "Ops\\vault"]))

    assert config.protected_paths == ("reference", "Ops/vault")
    assert config.protects(("Reference", "notes.md"))
    for bad in (["/abs"], ["a/../b"], [""], 3):
        _refused(_block(protected_paths=bad), "protected")


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
    _refused(_block(directory_naming=naming), fragment)


def test_directory_naming_reads_and_writes_runbook_folder_names() -> None:
    config = RunbooksConfig.model_validate(_block())

    assert config.directory_name("mail") == "mail-runbook"
    assert config.directory_slug("mail-runbook") == "mail"
    assert config.directory_slug("MAIL-RUNBOOK", fold_case=True) == "MAIL"
    assert config.directory_slug("-runbook") is None
    assert config.directory_slug("notes") is None


@pytest.mark.parametrize(
    ("role", "fragment"),
    [
        ({"stem": "50-troubleshooting.md", "match": ".*"}, "suffix"),
        ({"stem": "50-troubleshooting", "match": r"^\d{2}-faq$"}, "does not match its own"),
        ({"stem": "confluence-source", "match": ".*"}, "never write into"),
        ({"stem": "a/b", "match": ".*"}, "single folder or file name"),
        ({"stem": "50-x", "match": "(a+)+$"}, "catastrophic"),
        ({"stem": "50-x", "match": "("}, "not a valid regex"),
        ({"stem": "50-x", "match": ".*", "entry": "symptom"}, "entry"),
        ({"stem": "50-x", "match": ".*", "title": "a\nb"}, "single line"),
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
        ({"observation_label": "On\nsite"}, "single line"),
        ({"observation_label": "  "}, "must not be empty"),
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


def test_the_runbook_vault_is_its_own_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATACRON_RUNBOOK_VAULT", str(tmp_path / "ops"))

    settings = Settings()

    assert settings.runbook_vault == (tmp_path / "ops").resolve()
    assert settings.write_paths == []


def test_no_runbook_vault_by_default() -> None:
    assert Settings().runbook_vault is None


# --- write boundary ----------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / _RUNBOOK_VAULT / "mail-runbook").mkdir(parents=True)
    return root


def _admit(vault: Path, rel_path: str, *, runbook_vault: Path | None = None, **block: Any) -> Any:
    return admit_runbook_write(
        rel_path,
        vault_root=vault,
        runbook_vault=vault / _RUNBOOK_VAULT if runbook_vault is None else runbook_vault,
        runbooks=RunbooksConfig.model_validate(_block(**block)),
    )


def test_a_file_of_a_runbook_is_admitted(vault: Path) -> None:
    target = _admit(vault, "Ops/mail-runbook/50-troubleshooting.md")

    assert target.slug == "mail"
    assert target.directory == "Ops/mail-runbook"
    assert target.relative == "50-troubleshooting.md"
    assert target.resolved == (vault / "Ops/mail-runbook/50-troubleshooting.md").resolve()


def test_a_new_runbook_folder_and_its_quickref_are_admitted(vault: Path) -> None:
    assert _admit(vault, "Ops/dns-runbook/README.md").slug == "dns"
    assert _admit(vault, "Ops/mail-runbook/quickref/mail.md").relative == "quickref/mail.md"


@pytest.mark.parametrize("excluded", sorted(RUNBOOK_EXCLUDED_DIRECTORIES))
@pytest.mark.parametrize("spelling", [str.lower, str.upper])
def test_excluded_directories_are_refused_in_any_case(
    vault: Path, excluded: str, spelling: Any
) -> None:
    with pytest.raises(RunbookScopeError, match="never write into"):
        _admit(vault, f"Ops/mail-runbook/{spelling(excluded)}/page.md")


def test_a_protected_path_is_refused_inside_the_runbook_vault(vault: Path) -> None:
    """The file is a valid runbook file of the runbook vault, and still refused."""
    assert _admit(vault, "Ops/mail-runbook/secrets/notes.md")
    with pytest.raises(RunbookScopeError, match="in a protected path"):
        _admit(
            vault,
            "Ops/mail-runbook/secrets/notes.md",
            protected_paths=["mail-runbook/secrets"],
        )
    with pytest.raises(RunbookScopeError, match="in a protected path"):
        _admit(
            vault,
            "ops/MAIL-runbook/Secrets/notes.md",
            protected_paths=["mail-runbook/secrets"],
        )


@pytest.mark.parametrize(
    ("rel_path", "fragment"),
    [
        ("Ops/mail-notes/50-troubleshooting.md", "not named like a runbook"),
        ("Ops/-runbook/50-troubleshooting.md", "not named like a runbook"),
        ("Ops/README.md", "not inside a runbook folder"),
        ("_drafts/mail-runbook/50-troubleshooting.md", "outside DATACRON_RUNBOOK_VAULT"),
        ("Ops/mail-runbook/../../reference/x.md", "outside DATACRON_RUNBOOK_VAULT"),
        ("Ops/mail-runbook/scripts/run.ps1", "not a Markdown file"),
    ],
)
def test_only_markdown_inside_a_runbook_folder_is_admitted(
    vault: Path, rel_path: str, fragment: str
) -> None:
    with pytest.raises(RunbookScopeError, match=fragment):
        _admit(vault, rel_path)


def test_no_runbook_vault_refuses_every_write(vault: Path) -> None:
    with pytest.raises(RunbookScopeError, match="DATACRON_RUNBOOK_VAULT is unset"):
        admit_runbook_write(
            "Ops/mail-runbook/x.md",
            vault_root=vault,
            runbook_vault=None,
            runbooks=RunbooksConfig.model_validate(_block()),
        )


def test_the_runbook_vault_must_lie_inside_the_served_vault(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"

    with pytest.raises(RunbookScopeError, match="inside the served vault"):
        runbook_vault_folder(vault, outside)
    with pytest.raises(RunbookScopeError, match="inside the served vault"):
        _admit(vault, str(outside / "mail-runbook" / "x.md"), runbook_vault=outside)
    assert runbook_vault_folder(vault, vault / "Ops") == "Ops"
    assert runbook_vault_folder(vault, vault) == ""


def test_refusals_never_return_the_resolved_host_path(vault: Path) -> None:
    with pytest.raises(RunbookScopeError) as caught:
        _admit(vault, "_drafts/x-runbook/x.md")

    assert str(vault) not in str(caught.value)
