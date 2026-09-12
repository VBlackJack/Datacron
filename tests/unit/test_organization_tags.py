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
"""Vault-declared tag policy: configuration, evaluation, planner and manifest gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from datacron.core.config import (
    OrganizationConfig,
    OrganizationRule,
    OrganizationSubject,
    OrganizationTagPolicy,
    Settings,
    VaultConfig,
)
from datacron.core.scope import SingleTenantVaultScope
from datacron.organization.manifest import (
    OrganizationManifestError,
    load_organization_bundle,
    validate_organization_bundle,
)
from datacron.organization.planner import DeviationKind, plan_organization
from datacron.organization.rules import resolve_rule
from datacron.organization.tags import (
    TagPolicyError,
    TagViolationKind,
    evaluate_tag_policy,
    format_violations,
    path_within_scope,
)


def _rules() -> tuple[OrganizationRule, ...]:
    return (
        OrganizationRule(tag="memory/contact", folder="_memory/people"),
        OrganizationRule(tag="memory/project", folder="_memory/projects"),
        OrganizationRule(tag="memory/fact", folder="_memory/facts", naming="{iso_date}-{slug}"),
        OrganizationRule(tag="memory/decision", folder="_memory/decisions"),
    )


def _policy(**overrides: object) -> OrganizationTagPolicy:
    values: dict[str, object] = {
        "placement_namespace": "memory",
        "markers": ["memory/decision"],
        "subject_namespace": "project",
        "subjects": [
            {"tag": "project/heimdall", "aliases": ["heimdall", "projet/heimdall"]},
            "project/datacron",
        ],
        "subject_exempt_tags": ["memory/contact"],
        "allowed_namespaces": ["org", "meta"],
    }
    values.update(overrides)
    return OrganizationTagPolicy.model_validate(values)


def _organization(policy: OrganizationTagPolicy | None = None) -> OrganizationConfig:
    return OrganizationConfig(scope="_memory", rules=_rules(), tags=policy)


def _kinds(tags: list[str], policy: OrganizationTagPolicy | None = None) -> list[str]:
    return [
        item.kind.value for item in evaluate_tag_policy(tags, _organization(policy or _policy()))
    ]


# --- evaluation -----------------------------------------------------------


def test_vault_without_policy_reports_nothing() -> None:
    assert evaluate_tag_policy(["whatever", "type/fact"], _organization(None)) == ()


def test_compliant_note_has_no_violation() -> None:
    assert (
        _kinds(["memory/fact", "memory/decision", "project/heimdall", "org/magellan", "ssh"]) == []
    )


def test_missing_placement_tag_is_ungoverned() -> None:
    violations = evaluate_tag_policy(["project/heimdall", "ssh"], _organization(_policy()))

    assert [item.kind for item in violations] == [TagViolationKind.UNGOVERNED]
    assert violations[0].expected is not None
    assert "memory/fact" in violations[0].expected


def test_two_placement_tags_break_cardinality_but_a_marker_does_not() -> None:
    assert _kinds(["memory/fact", "memory/project"]) == ["TAG_CARDINALITY"]
    assert _kinds(["memory/fact", "memory/decision"]) == []
    assert _kinds(["memory/decision"]) == []


def test_alias_and_bare_subject_names_are_unknown_tags() -> None:
    violations = evaluate_tag_policy(["memory/fact", "heimdall"], _organization(_policy()))

    assert [item.kind for item in violations] == [TagViolationKind.UNKNOWN_TAG]
    assert violations[0].expected == "project/heimdall"
    assert _kinds(["memory/fact", "projet/heimdall"]) == ["UNKNOWN_TAG"]


def test_unregistered_subject_and_undeclared_namespace_are_unknown_tags() -> None:
    assert _kinds(["memory/fact", "project/ghost"]) == ["UNKNOWN_TAG"]
    assert _kinds(["memory/fact", "topic/rdp"]) == ["UNKNOWN_TAG"]
    assert _kinds(["memory/fact", "memory/bug"]) == ["UNKNOWN_TAG"]


def test_second_subject_breaks_cardinality_unless_placement_is_exempt() -> None:
    assert _kinds(["memory/fact", "project/heimdall", "project/datacron"]) == ["TAG_CARDINALITY"]
    assert _kinds(["memory/contact", "project/heimdall", "project/datacron"]) == []


def test_evaluation_is_case_insensitive_and_ignores_inline_hash() -> None:
    assert _kinds(["Memory/Fact", "#project/heimdall"]) == []


def test_format_violations_names_kind_tag_and_expectation() -> None:
    violations = evaluate_tag_policy(["memory/fact", "heimdall"], _organization(_policy()))

    rendered = format_violations(violations)

    assert rendered.startswith("UNKNOWN_TAG [heimdall]: alias of the registered subject")
    assert "(expected project/heimdall)" in rendered


def test_tag_policy_error_carries_a_stable_code() -> None:
    error = TagPolicyError("_memory/facts/x.md", evaluate_tag_policy([], _organization(_policy())))

    assert error.code == "tag_policy_violation"
    assert str(error).startswith("_memory/facts/x.md: UNGOVERNED")


# --- configuration --------------------------------------------------------


def test_subject_accepts_plain_string_and_rejects_self_alias() -> None:
    policy = _policy(subjects=["project/x"])

    assert policy.subjects == (OrganizationSubject(tag="project/x"),)
    with pytest.raises(ValidationError, match="lists itself as an alias"):
        _policy(subjects=[{"tag": "project/x", "aliases": ["PROJECT/X"]}])


def test_policy_rejects_duplicate_names_and_wrong_namespaces() -> None:
    with pytest.raises(ValidationError, match="declared twice"):
        _policy(subjects=[{"tag": "project/a", "aliases": ["project/b"]}, {"tag": "project/b"}])
    with pytest.raises(ValidationError, match="must live in the subject namespace"):
        _policy(subjects=["memory/a"])
    with pytest.raises(ValidationError, match="must live in the placement namespace"):
        _policy(markers=["project/a"])
    with pytest.raises(ValidationError, match="must differ from placement_namespace"):
        _policy(subject_namespace="memory")
    with pytest.raises(ValidationError, match="duplicates a declared namespace"):
        _policy(allowed_namespaces=["project"])
    with pytest.raises(ValidationError, match="subject_namespace is required"):
        _policy(subject_namespace=None)


def test_policy_refuses_unknown_keys_loudly() -> None:
    with pytest.raises(ValidationError):
        _policy(subjetcs=["project/x"])


def test_organization_binds_policy_to_its_rules() -> None:
    with pytest.raises(ValidationError, match="requires at least one rule"):
        OrganizationConfig(scope="_memory", rules=(), tags=_policy())
    with pytest.raises(ValidationError, match="must be a declared rule tag"):
        _organization(_policy(markers=["memory/session"]))
    with pytest.raises(ValidationError, match="must be a declared rule tag"):
        _organization(_policy(subject_exempt_tags=["memory/session"]))
    with pytest.raises(ValidationError, match="outside the placement namespace"):
        OrganizationConfig(
            scope="_memory",
            rules=(OrganizationRule(tag="kind/fact", folder="_memory/facts"),),
            tags=_policy(),
        )


def test_policy_round_trips_through_vault_yaml() -> None:
    document = {
        "organization": {
            "scope": "_memory",
            "rules": [{"tag": "memory/fact", "folder": "_memory/facts"}],
            "tags": {
                "placement_namespace": "memory",
                "subject_namespace": "project",
                "subjects": [{"tag": "project/x", "aliases": ["x"]}],
            },
        }
    }

    config = VaultConfig.model_validate(document)

    assert config.organization is not None
    assert config.organization.tags is not None
    assert config.organization.tags.subjects[0].aliases == ("x",)


def test_alias_is_refused_in_any_spelling_and_cannot_collide_with_a_rule() -> None:
    policy = _policy(
        subjects=[{"tag": "project/a", "aliases": ["org/a", "projet/a"]}],
        allowed_namespaces=["org"],
    )

    assert _kinds(["memory/fact", "org/a"], policy) == ["UNKNOWN_TAG"]
    violations = evaluate_tag_policy(["memory/fact", "projet/a"], _organization(policy))
    assert violations[0].expected == "project/a"
    with pytest.raises(ValidationError, match="collides with a declared rule tag"):
        _organization(_policy(subjects=[{"tag": "project/a", "aliases": ["memory/fact"]}]))


def test_at_most_one_marker_accompanies_the_placement_tag() -> None:
    policy = _policy(markers=["memory/decision", "memory/project"])

    assert _kinds(["memory/fact", "memory/decision"], policy) == []
    assert _kinds(["memory/fact", "memory/decision", "memory/project"], policy) == [
        "TAG_CARDINALITY"
    ]
    assert _kinds(["memory/decision", "memory/project"], policy) == ["TAG_CARDINALITY"]


_SUBJECT_FOLDER = "_memory/subjects/perso/heimdall"


def _rules_with_subject() -> tuple[OrganizationRule, ...]:
    contact, *fallbacks = _rules()
    subject = OrganizationRule(tag="project/heimdall", folder=_SUBJECT_FOLDER, max_kb=121)
    return (contact, subject, *fallbacks)


def _subject_organization(policy: OrganizationTagPolicy | None = None) -> OrganizationConfig:
    return OrganizationConfig(
        scope="_memory", rules=_rules_with_subject(), tags=policy or _policy()
    )


def _subject_kinds(tags: list[str]) -> list[str]:
    return [item.kind.value for item in evaluate_tag_policy(tags, _subject_organization())]


def test_subject_rule_is_accepted_and_never_counts_as_placement() -> None:
    organization = _subject_organization()

    assert organization.rules[1].tag == "project/heimdall"
    assert _subject_kinds(["memory/fact", "project/heimdall"]) == []
    assert _subject_kinds(["memory/fact", "memory/decision", "project/heimdall"]) == []
    assert _subject_kinds(["project/heimdall", "ssh"]) == ["UNGOVERNED"]
    assert _subject_kinds(["memory/fact", "memory/project", "project/heimdall"]) == [
        "TAG_CARDINALITY"
    ]
    assert _subject_kinds(["memory/fact", "project/heimdall", "project/datacron"]) == [
        "TAG_CARDINALITY"
    ]
    ungoverned = evaluate_tag_policy(["project/heimdall"], organization)
    assert ungoverned[0].expected is not None
    assert "project/heimdall" not in ungoverned[0].expected


def test_subject_rule_must_name_a_registered_subject_and_leave_a_placement_rule() -> None:
    with pytest.raises(ValidationError, match="is not a declared subject"):
        OrganizationConfig(
            scope="_memory",
            rules=(*_rules(), OrganizationRule(tag="project/ghost", folder="_memory/x")),
            tags=_policy(),
        )
    with pytest.raises(ValidationError, match="is not a declared subject"):
        OrganizationConfig(
            scope="_memory",
            rules=(*_rules(), OrganizationRule(tag="heimdall", folder="_memory/x")),
            tags=_policy(),
        )
    with pytest.raises(ValidationError, match="must live in the placement namespace"):
        _subject_organization(_policy(markers=["project/heimdall"]))
    with pytest.raises(ValidationError, match="must be a declared placement rule tag"):
        _subject_organization(_policy(subject_exempt_tags=["project/heimdall"]))
    with pytest.raises(ValidationError, match="requires at least one placement rule"):
        OrganizationConfig(
            scope="_memory",
            rules=(OrganizationRule(tag="project/heimdall", folder=_SUBJECT_FOLDER),),
            tags=_policy(markers=[], subject_exempt_tags=[]),
        )


def test_subject_rule_admission_uses_the_evaluator_normalization() -> None:
    sharp = "project/straße"
    policy = _policy(subjects=[sharp])

    with pytest.raises(ValidationError, match="is not a declared subject"):
        OrganizationConfig(
            scope="_memory",
            rules=(*_rules(), OrganizationRule(tag="project/strasse", folder="_memory/s")),
            tags=policy,
        )
    organization = OrganizationConfig(
        scope="_memory",
        rules=(OrganizationRule(tag=sharp, folder="_memory/s"), *_rules()),
        tags=policy,
    )
    assert evaluate_tag_policy(["memory/fact", sharp], organization) == ()
    assert resolve_rule(["memory/fact", sharp], organization) is organization.rules[0]


def test_subject_rule_namespace_uses_the_evaluator_normalization() -> None:
    sharp = "straße"
    folded = "strasse"
    with pytest.raises(ValidationError, match="is not a declared subject"):
        OrganizationConfig(
            scope="_memory",
            rules=(OrganizationRule(tag=f"{folded}/demo", folder="_memory/s"), *_rules()),
            tags=_policy(subject_namespace=sharp, subjects=[f"{folded}/demo"]),
        )
    organization = OrganizationConfig(
        scope="_memory",
        rules=(OrganizationRule(tag=f"{sharp}/demo", folder="_memory/s"), *_rules()),
        tags=_policy(subject_namespace=sharp, subjects=[f"{sharp}/demo"]),
    )
    assert evaluate_tag_policy(["memory/fact", f"{sharp}/demo"], organization) == ()
    assert resolve_rule(["memory/fact", f"{sharp}/demo"], organization) is organization.rules[0]


def test_subject_rule_round_trips_through_vault_yaml() -> None:
    document = {
        "organization": {
            "scope": "_memory",
            "rules": [
                {"tag": "memory/contact", "folder": "_memory/people"},
                {"tag": "project/x", "folder": "_memory/subjects/x", "max_kb": 121},
                {"tag": "memory/fact", "folder": "_memory/facts"},
            ],
            "tags": {
                "placement_namespace": "memory",
                "subject_namespace": "project",
                "subjects": [{"tag": "project/x", "aliases": ["x"]}],
            },
        }
    }

    config = VaultConfig.model_validate(document)

    assert config.organization is not None
    assert [rule.tag for rule in config.organization.rules][1] == "project/x"


def test_rule_tags_must_be_lowercase_when_a_policy_is_declared() -> None:
    rules = (OrganizationRule(tag="Memory/Fact", folder="_memory/facts"),)

    OrganizationConfig(scope="_memory", rules=rules)
    with pytest.raises(ValidationError, match="must be lowercase"):
        OrganizationConfig(scope="_memory", rules=rules, tags=_policy(markers=[]))


@pytest.mark.parametrize(
    ("rel_path", "scope", "inside"),
    [
        ("_memory/facts/x.md", "_memory", True),
        ("./_memory/facts/x.md", "_memory", True),
        ("_memory//facts/x.md", "_memory//", True),
        ("_memory\\facts\\x.md", "_memory", True),
        ("_memory.md", "_memory", False),
        ("_memoryx/x.md", "_memory", False),
        ("_memory/../_drafts/x.md", "_memory", False),
        ("_drafts/x.md", "_memory", False),
        ("_memory/x.md", "", False),
    ],
)
def test_path_within_scope_canonicalizes_before_comparing(
    rel_path: str, scope: str, inside: bool
) -> None:
    assert path_within_scope(rel_path, scope) is inside


# --- planner --------------------------------------------------------------


def _write(root: Path, rel_path: str, tags: list[str], body: str = "content\n") -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump({"title": "note", "tags": tags}, sort_keys=False).strip()
    path.write_text(f"---\n{rendered}\n---\n\n{body}", encoding="utf-8")


def _vault_config(policy: OrganizationTagPolicy | None) -> VaultConfig:
    return VaultConfig.model_validate({"organization": _organization(policy)})


def test_planner_reports_policy_gaps_only_when_the_policy_is_declared(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-09-12-fine.md", ["memory/fact", "project/heimdall"])
    _write(tmp_path, "_memory/facts/2026-09-12-bare.md", ["memory/fact", "heimdall"])
    _write(tmp_path, "_memory/facts/2026-09-12-orphan.md", ["ssh"])
    _write(tmp_path, "_memory/facts/2026-09-12-inline.md", ["memory/fact"], body="see #topic/rdp\n")

    silent = plan_organization(tmp_path, _vault_config(None))
    assert silent.deviations == ()
    assert silent.unmatched == 1

    plan = plan_organization(tmp_path, _vault_config(_policy()))

    assert plan.unmatched == 1
    assert plan.governed == 3
    assert [(item.rel_path.rsplit("/", 1)[1], item.kind) for item in plan.deviations] == [
        ("2026-09-12-bare.md", DeviationKind.UNKNOWN_TAG),
        ("2026-09-12-inline.md", DeviationKind.UNKNOWN_TAG),
        ("2026-09-12-orphan.md", DeviationKind.UNGOVERNED),
    ]
    assert plan.counts_by_kind()["UNGOVERNED"] == 1
    assert plan.deviations[0].expected == "project/heimdall"
    assert sorted(silent.counts_by_kind()) == [
        "NAMING",
        "OVER_SIZE",
        "TAG_CARDINALITY",
        "UNGOVERNED",
        "UNKNOWN_TAG",
        "WRONG_FOLDER",
    ]


def test_planner_places_subject_notes_by_the_subject_rule(tmp_path: Path) -> None:
    _write(tmp_path, "_memory/facts/2026-09-12-moved.md", ["memory/fact", "project/heimdall"])
    _write(tmp_path, f"{_SUBJECT_FOLDER}/heimdall.md", ["memory/project", "project/heimdall"])
    _write(tmp_path, f"{_SUBJECT_FOLDER}/2026-09-12-fact.md", ["memory/fact", "project/heimdall"])
    _write(tmp_path, f"{_SUBJECT_FOLDER}/bare.md", ["project/heimdall"])
    _write(
        tmp_path,
        "_memory/people/ada.md",
        ["memory/contact", "project/heimdall", "project/datacron"],
    )
    _write(tmp_path, "_memory/facts/2026-09-12-orphan.md", ["memory/fact"])

    plan = plan_organization(
        tmp_path, VaultConfig.model_validate({"organization": _subject_organization()})
    )

    assert plan.governed == 6
    assert plan.unmatched == 0
    assert [
        (item.rel_path.rsplit("/", 1)[1], item.kind, item.expected) for item in plan.deviations
    ] == [
        ("2026-09-12-moved.md", DeviationKind.WRONG_FOLDER, _SUBJECT_FOLDER),
        (
            "bare.md",
            DeviationKind.UNGOVERNED,
            "one of: memory/contact, memory/project, memory/fact, memory/decision",
        ),
    ]
    assert plan.deviations[0].tag == "project/heimdall"


# --- manifest -------------------------------------------------------------

_NOTE_ID = "01J00000000000000000000001"


def _note_bytes(tags: list[str], body: str) -> bytes:
    tag_lines = "".join(f"  - {tag}\n" for tag in tags)
    return f"---\nid: {_NOTE_ID}\ntitle: note\ntags:\n{tag_lines}---\n# note\n\n{body}\n".encode()


def _config_bytes(with_policy: bool) -> bytes:
    document: dict[str, object] = {
        "organization": {
            "scope": "memory",
            "rules": [{"tag": "memory/fact", "folder": "memory", "naming": "{slug}"}],
        }
    }
    if with_policy:
        document["organization"]["tags"] = {  # type: ignore[index]
            "placement_namespace": "memory",
            "subject_namespace": "project",
            "subjects": [{"tag": "project/heimdall", "aliases": ["heimdall"]}],
        }
    return yaml.safe_dump(document, sort_keys=False).encode()


def _bundle(
    tmp_path: Path, *, with_policy: bool, result_tags: list[str], body: str
) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / ".datacron").mkdir()
    before = _note_bytes(["memory/fact"], "before")
    (vault / "memory" / "note.md").write_bytes(before)
    (vault / ".datacron" / "VAULT.yaml").write_bytes(_config_bytes(with_policy))
    after = _note_bytes(result_tags, body)
    digest = hashlib.sha256(after).hexdigest()
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "payloads").mkdir(parents=True)
    (bundle_dir / "payloads" / f"{digest}.md").write_bytes(after)
    manifest = {
        "schema": "organization-apply-v1",
        "operations": [
            {
                "kind": "replace_exact",
                "target": "memory/note.md",
                "expected_sha256": hashlib.sha256(before).hexdigest(),
                "expected": {"id": _NOTE_ID, "aliases": []},
                "payload_sha256": digest,
                "result": {"id": _NOTE_ID, "aliases": []},
            }
        ],
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    return vault, manifest_path


def _scope(vault: Path) -> SingleTenantVaultScope:
    settings = Settings(vault_root=vault, read_paths=[vault], write_paths=[vault / "memory"])
    return SingleTenantVaultScope(vault, settings)


def _validate(vault: Path, manifest_path: Path) -> None:
    bundle = load_organization_bundle(manifest_path, vault_root=vault)
    validate_organization_bundle(bundle, vault_root=vault, scope=_scope(vault))


def test_manifest_refuses_results_that_break_the_declared_policy(tmp_path: Path) -> None:
    vault, manifest_path = _bundle(
        tmp_path, with_policy=True, result_tags=["memory/fact", "heimdall"], body="after"
    )

    with pytest.raises(OrganizationManifestError) as caught:
        _validate(vault, manifest_path)

    assert caught.value.code == "tag_policy_violation"
    assert "memory/note.md" in str(caught.value)
    assert "alias of the registered subject project/heimdall" in str(caught.value)


def test_manifest_judges_inline_body_tags_too(tmp_path: Path) -> None:
    vault, manifest_path = _bundle(
        tmp_path, with_policy=True, result_tags=["memory/fact"], body="see #topic/ghost"
    )

    with pytest.raises(OrganizationManifestError, match="namespace 'topic' is not declared"):
        _validate(vault, manifest_path)


def test_manifest_accepts_a_target_configuration_with_a_subject_rule(tmp_path: Path) -> None:
    vault, manifest_path = _bundle(
        tmp_path, with_policy=True, result_tags=["memory/fact", "project/heimdall"], body="after"
    )
    document = yaml.safe_load(_config_bytes(True))
    document["organization"]["rules"].insert(
        0, {"tag": "project/heimdall", "folder": "memory/heimdall", "max_kb": 121}
    )
    target = yaml.safe_dump(document, sort_keys=False).encode()
    digest = hashlib.sha256(target).hexdigest()
    (manifest_path.parent / "payloads" / f"{digest}.yaml").write_bytes(target)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"] = {
        "kind": "replace_exact",
        "target": ".datacron/VAULT.yaml",
        "expected_sha256": hashlib.sha256(_config_bytes(True)).hexdigest(),
        "payload_sha256": digest,
    }
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")

    _validate(vault, manifest_path)


def test_manifest_accepts_compliant_results_and_ignores_policy_when_absent(tmp_path: Path) -> None:
    vault, manifest_path = _bundle(
        tmp_path, with_policy=True, result_tags=["memory/fact", "project/heimdall"], body="after"
    )
    _validate(vault, manifest_path)

    vault, manifest_path = _bundle(
        tmp_path / "other", with_policy=False, result_tags=["memory/fact", "heimdall"], body="x"
    )
    _validate(vault, manifest_path)
