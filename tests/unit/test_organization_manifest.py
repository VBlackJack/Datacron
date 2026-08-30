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
"""Fail-closed tests for content-addressed organization bundles."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import pytest
from pydantic import ValidationError

import datacron.organization.manifest as manifest_module
from datacron.core.config import Settings
from datacron.core.scope import (
    LinkedPathError,
    SingleTenantVaultScope,
    assert_path_chain_without_links,
)
from datacron.organization.manifest import (
    IdentitySidecarCaseCanonicalization,
    OrganizationBundle,
    OrganizationManifest,
    OrganizationManifestError,
    ValidatedOrganizationBundle,
    canonicalize_identity_sidecar_case_collisions,
    hash_identity_sidecar_case_canonicalizations,
    load_organization_bundle,
    parse_organization_config_document,
    sha256_bytes,
    validate_organization_bundle,
)
from datacron.organization.planner import (
    hash_organization_plan,
    plan_organization,
    plan_organization_snapshot,
)

_REPLACE_ID: Final[str] = "01J00000000000000000000001"
_MOVE_ID: Final[str] = "01J00000000000000000000002"
_CREATE_ID: Final[str] = "01J00000000000000000000003"
_GLOBAL_ID: Final[str] = "01J00000000000000000000004"
_UNRELATED_ID: Final[str] = "01J00000000000000000000005"
_SIDECAR_ID_A: Final[str] = "01J00000000000000000000006"
_SIDECAR_ID_B: Final[str] = "01J00000000000000000000007"
_LEGACY_EXISTING_ID: Final[str] = "01JXJ4K9Q2SKILLSARCH000001"


@dataclass(frozen=True, slots=True)
class _BundleCase:
    vault: Path
    manifest_path: Path
    payloads: dict[str, bytes]
    manifest: dict[str, object]


def _note(note_id: str, title: str, aliases: tuple[str, ...], body: str) -> bytes:
    alias_lines = "".join(f"  - {alias}\n" for alias in aliases)
    return (
        f"---\nid: {note_id}\ntitle: {title}\naliases:\n{alias_lines}"
        f"tags:\n  - memory/fact\n---\n# {title}\n\n{body}\n"
    ).encode()


def _config(*, max_kb: int) -> bytes:
    return (
        "organization:\n"
        "  scope: memory\n"
        "  rules:\n"
        "    - tag: memory/fact\n"
        "      folder: memory\n"
        "      naming: '{slug}'\n"
        f"      max_kb: {max_kb}\n"
    ).encode()


def _operation_payload(
    payloads: dict[str, bytes],
    content: bytes,
    *,
    suffix: str = ".md",
) -> str:
    digest = sha256_bytes(content)
    payloads[f"{digest}{suffix}"] = content
    return digest


def _write_bundle(case: _BundleCase) -> None:
    payload_root = case.manifest_path.parent / "payloads"
    payload_root.mkdir(parents=True)
    for name, content in case.payloads.items():
        (payload_root / name).write_bytes(content)
    case.manifest_path.write_text(
        json.dumps(case.manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _build_case(
    tmp_path: Path,
    *,
    replace_id: str = _REPLACE_ID,
    create_id: str = _CREATE_ID,
    create_alias: str = "created-alias",
    target_config: bytes | None = None,
) -> _BundleCase:
    vault = tmp_path / "vault"
    memory = vault / "memory"
    other = vault / "other"
    sidecar = vault / ".datacron"
    memory.mkdir(parents=True)
    other.mkdir()
    sidecar.mkdir()

    replace_before = _note(replace_id, "replace-title", ("replace-old",), "before")
    move_before = _note(_MOVE_ID, "move-title", (), "before")
    unrelated = _note(_UNRELATED_ID, "unrelated-title", (), "unrelated")
    global_note = _note(_GLOBAL_ID, "global-title", (), "outside organization scope")
    (memory / "replace.md").write_bytes(replace_before)
    (memory / "move.md").write_bytes(move_before)
    (memory / "unrelated.md").write_bytes(unrelated)
    (other / "global.md").write_bytes(global_note)
    live_config = _config(max_kb=120)
    (sidecar / "VAULT.yaml").write_bytes(live_config)

    payloads: dict[str, bytes] = {}
    replace_after = _note(replace_id, "replace-title", ("replace-old",), "after")
    move_after = _note(_MOVE_ID, "move-title", ("move",), "after")
    create_after = _note(create_id, "created-title", (create_alias,), "created")
    replace_digest = _operation_payload(payloads, replace_after)
    move_digest = _operation_payload(payloads, move_after)
    create_digest = _operation_payload(payloads, create_after)
    resolved_target_config = target_config or _config(max_kb=121)
    config_digest = _operation_payload(payloads, resolved_target_config, suffix=".yaml")

    manifest: dict[str, object] = {
        "schema": "organization-apply-v1",
        "operations": [
            {
                "kind": "replace_exact",
                "target": "memory/replace.md",
                "expected_sha256": sha256_bytes(replace_before),
                "expected": {"id": replace_id, "aliases": ["replace-old"]},
                "payload_sha256": replace_digest,
                "result": {"id": replace_id, "aliases": ["replace-old"]},
            },
            {
                "kind": "move_replace_exact",
                "source": "memory/move.md",
                "target": "memory/moved.md",
                "expected_sha256": sha256_bytes(move_before),
                "expected": {"id": _MOVE_ID, "aliases": []},
                "payload_sha256": move_digest,
                "result": {"id": _MOVE_ID, "aliases": ["move"]},
            },
            {
                "kind": "create_exact",
                "target": "memory/created.md",
                "payload_sha256": create_digest,
                "result": {"id": create_id, "aliases": [create_alias]},
            },
        ],
        "config": {
            "kind": "replace_exact",
            "target": ".datacron/VAULT.yaml",
            "expected_sha256": sha256_bytes(live_config),
            "payload_sha256": config_digest,
        },
    }
    case = _BundleCase(
        vault=vault,
        manifest_path=tmp_path / "bundle" / "manifest.json",
        payloads=payloads,
        manifest=manifest,
    )
    case.manifest_path.parent.mkdir()
    _write_bundle(case)
    return case


def _scope(vault: Path) -> SingleTenantVaultScope:
    settings = Settings(
        vault_root=vault,
        read_paths=[vault],
        write_paths=[vault / "memory"],
    )
    return SingleTenantVaultScope(vault, settings)


def _load_and_validate(case: _BundleCase) -> tuple[OrganizationBundle, ValidatedOrganizationBundle]:
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)
    return bundle, validate_organization_bundle(
        bundle,
        vault_root=case.vault,
        scope=_scope(case.vault),
    )


def _validate_manifest_document(document: dict[str, object]) -> OrganizationManifest:
    return OrganizationManifest.model_validate_json(json.dumps(document))


def test_valid_bundle_authenticates_payloads_and_live_scope(tmp_path: Path) -> None:
    case = _build_case(tmp_path)

    bundle, validated = _load_and_validate(case)

    assert bundle.manifest_sha256 == sha256_bytes(case.manifest_path.read_bytes())
    assert bundle.payload_set_sha256
    assert bundle.total_payload_bytes == sum(len(content) for content in case.payloads.values())
    assert [operation.kind for operation in validated.operations] == [
        "replace_exact",
        "move_replace_exact",
        "create_exact",
    ]
    assert validated.config_path == (case.vault / ".datacron" / "VAULT.yaml").resolve()
    assert validated.config_before_sha256 == sha256_bytes(_config(max_kb=120))
    assert validated.identity_sidecar_path == case.vault / ".datacron" / "ulids.json"
    assert validated.identity_sidecar_before_sha256 is None
    assert validated.migrated_identity_sidecar_path == (
        case.vault / ".datacron" / "ulids.json.migrated"
    )
    assert validated.migrated_identity_sidecar_before_sha256 is None
    assert len(validated.scope_note_preconditions) == 3
    assert validated.scope_note_count == 3
    assert validated.scope_digest


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    raw = case.manifest_path.read_text(encoding="utf-8")
    case.manifest_path.write_text(
        raw[:-1] + ',"schema":"organization-apply-v1"}',
        encoding="utf-8",
    )

    with pytest.raises(OrganizationManifestError, match="duplicate JSON object key") as error:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)

    assert error.value.code == "manifest_invalid"


@pytest.mark.parametrize("scalar", [".nan", ".inf", "-.inf"])
def test_config_parser_rejects_nonfinite_yaml_scalars(scalar: str) -> None:
    with pytest.raises(OrganizationManifestError, match="non-finite") as error:
        parse_organization_config_document(
            f"organization: {{scope: memory, rules: []}}\nother: {scalar}\n",
            label="test VAULT.yaml",
        )

    assert error.value.code == "config_payload_invalid"


def test_config_parser_rejects_yaml_aliases_and_merge_keys() -> None:
    with pytest.raises(OrganizationManifestError, match=r"merge keys|aliases") as error:
        parse_organization_config_document(
            "base: &base\n  scope: memory\norganization:\n  <<: *base\n  rules: []\n",
            label="test VAULT.yaml",
        )

    assert error.value.code == "config_payload_invalid"


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "CON",
        "AUX/facts",
        "memory/*",
        "memory/?",
        "memory/<facts",
        "memory/facts>",
        "memory/facts|archive",
        "memory/trailing.",
        "memory/trailing ",
        "memory//facts",
    ],
)
@pytest.mark.parametrize("field", ["scope", "folder"])
def test_config_parser_rejects_noncanonical_windows_organization_paths(
    unsafe_path: str,
    field: str,
) -> None:
    scope = unsafe_path if field == "scope" else "memory"
    folder = unsafe_path if field == "folder" else "memory"
    document = {
        "organization": {
            "scope": scope,
            "rules": [
                {
                    "tag": "memory/fact",
                    "folder": folder,
                    "naming": "{slug}",
                }
            ],
        }
    }

    with pytest.raises(OrganizationManifestError) as error:
        parse_organization_config_document(
            json.dumps(document),
            label="unsafe organization config",
        )

    assert error.value.code == "config_payload_invalid"


def test_note_payload_rejects_duplicate_frontmatter_keys(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    operation = cast("dict[str, object]", cast("list[object]", case.manifest["operations"])[0])
    original_digest = cast("str", operation["payload_sha256"])
    payload_root = case.manifest_path.parent / "payloads"
    original_path = payload_root / f"{original_digest}.md"
    duplicate = original_path.read_bytes().replace(
        f"id: {_REPLACE_ID}\n".encode(),
        f"id: {_REPLACE_ID}\nid: {_REPLACE_ID}\n".encode(),
        1,
    )
    duplicate_digest = sha256_bytes(duplicate)
    original_path.unlink()
    (payload_root / f"{duplicate_digest}.md").write_bytes(duplicate)
    operation["payload_sha256"] = duplicate_digest
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")

    with pytest.raises(OrganizationManifestError, match="duplicate mapping key") as error:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)

    assert error.value.code == "payload_identity_invalid"


def test_existing_legacy_identity_is_preserved_but_cannot_be_created(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path, replace_id=_LEGACY_EXISTING_ID)

    _bundle, validated = _load_and_validate(case)

    assert validated.operations[0].expected_identity is not None
    assert validated.operations[0].expected_identity.id == _LEGACY_EXISTING_ID
    assert validated.operations[0].result_identity.id == _LEGACY_EXISTING_ID

    invalid_create = _build_case(tmp_path / "create", create_id=_LEGACY_EXISTING_ID)
    with pytest.raises(OrganizationManifestError, match="canonical"):
        load_organization_bundle(invalid_create.manifest_path, vault_root=invalid_create.vault)


@pytest.mark.parametrize("character", ["<", ">", '"', "|", "?", "*"])
def test_manifest_rejects_windows_invalid_note_path_characters(character: str) -> None:
    with pytest.raises(ValidationError, match="Windows-invalid"):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "create_exact",
                        "target": f"memory/a{character}.md",
                        "payload_sha256": "1" * 64,
                        "result": {"id": _CREATE_ID, "aliases": []},
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "reserved_name",
    ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$", "CONOUT$"],
)
def test_manifest_rejects_extended_windows_reserved_names(reserved_name: str) -> None:
    with pytest.raises(ValidationError, match="reserved Windows name"):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "create_exact",
                        "target": f"memory/{reserved_name}.md",
                        "payload_sha256": "1" * 64,
                        "result": {"id": _CREATE_ID, "aliases": []},
                    }
                ],
            }
        )


def test_existing_identity_change_remains_forbidden() -> None:
    with pytest.raises(ValidationError, match="preserve"):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "replace_exact",
                        "target": "memory/legacy.md",
                        "expected_sha256": "1" * 64,
                        "expected": {"id": _LEGACY_EXISTING_ID, "aliases": []},
                        "payload_sha256": "2" * 64,
                        "result": {"id": _REPLACE_ID, "aliases": []},
                    }
                ],
            }
        )


def test_scope_digest_changes_when_unrelated_governed_note_changes(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    bundle, first = _load_and_validate(case)
    unrelated = case.vault / "memory" / "unrelated.md"
    unrelated.write_bytes(_note(_UNRELATED_ID, "unrelated-title", (), "changed outside manifest"))

    second = validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert second.scope_digest != first.scope_digest


def test_note_only_manifest_rejects_live_rule_folder_that_is_a_file(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    config_entry = cast("dict[str, object]", case.manifest.pop("config"))
    config_digest = cast("str", config_entry["payload_sha256"])
    case.payloads.pop(f"{config_digest}.yaml")
    payload_path = case.manifest_path.parent / "payloads" / f"{config_digest}.yaml"
    payload_path.unlink()
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")
    (case.vault / "memory" / "blocked-folder").write_text("not a directory", encoding="utf-8")
    (case.vault / ".datacron" / "VAULT.yaml").write_text(
        "organization:\n"
        "  scope: memory\n"
        "  rules:\n"
        "    - tag: memory/fact\n"
        "      folder: memory/blocked-folder\n"
        "      naming: '{slug}'\n",
        encoding="utf-8",
    )

    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)
    with pytest.raises(OrganizationManifestError, match="existing non-directory") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "organization_rule_path_invalid"


def test_scope_digest_changes_when_sidecar_identity_changes(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    no_frontmatter_id = case.vault / "memory" / "sidecar-identity.md"
    no_frontmatter_id.write_text("# Sidecar identity\n\nBody.\n", encoding="utf-8")
    sidecar = case.vault / ".datacron" / "ulids.json"
    sidecar.write_text(
        json.dumps({"memory/sidecar-identity.md": _SIDECAR_ID_A}),
        encoding="utf-8",
    )
    bundle, first = _load_and_validate(case)

    sidecar.write_text(
        json.dumps({"memory/sidecar-identity.md": _SIDECAR_ID_B}),
        encoding="utf-8",
    )
    second = validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert second.scope_digest != first.scope_digest


def test_projected_planner_snapshot_matches_materialized_filesystem(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    _bundle, validated = _load_and_validate(case)
    snapshot = plan_organization_snapshot(
        case.vault,
        validated.target_config,
        validated.projected_notes,
    )

    for resolved in validated.operations:
        target = case.vault / resolved.operation.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resolved.payload.raw_bytes)
        if resolved.kind == "move_replace_exact" and resolved.source_path is not None:
            resolved.source_path.unlink()
    assert validated.config_path is not None
    assert validated.config_payload is not None
    validated.config_path.write_bytes(validated.config_payload.raw_bytes)
    filesystem = plan_organization(
        case.vault,
        validated.target_config,
        settings=Settings(
            vault_root=case.vault,
            read_paths=[case.vault],
            write_paths=[case.vault],
        ),
    )

    assert hash_organization_plan(snapshot) == hash_organization_plan(filesystem)


def test_sidecar_reserved_id_blocks_create_even_for_excluded_path(tmp_path: Path) -> None:
    case = _build_case(tmp_path, create_id=_SIDECAR_ID_A)
    (case.vault / ".datacron" / "ulids.json").write_text(
        json.dumps({"_archive/reserved.md": _SIDECAR_ID_A}),
        encoding="utf-8",
    )

    with pytest.raises(OrganizationManifestError, match="reserved by sidecar") as error:
        _load_and_validate(case)

    assert error.value.code == "result_id_collision"


def test_create_target_excluded_by_note_admission_is_rejected(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    create = cast("dict[str, object]", cast("list[object]", case.manifest["operations"])[2])
    create["target"] = "memory/.hidden/created.md"
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")

    with pytest.raises(OrganizationManifestError, match="not admitted") as error:
        _load_and_validate(case)

    assert error.value.code == "target_not_admitted"


def test_sidecar_mapped_source_derives_exact_mapping_migration(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    (case.vault / ".datacron" / "ulids.json").write_text(
        json.dumps({"memory/move.md": _MOVE_ID}),
        encoding="utf-8",
    )

    _bundle, validated = _load_and_validate(case)

    assert validated.identity_sidecar_path == case.vault / ".datacron" / "ulids.json"
    assert validated.identity_sidecar_before_sha256 is not None
    assert validated.identity_sidecar_after_sha256 is not None
    assert validated.identity_sidecar_after_bytes is not None
    migrated = json.loads(validated.identity_sidecar_after_bytes)
    assert "memory/move.md" not in migrated
    assert migrated["memory/moved.md"] == _MOVE_ID


def test_sidecar_case_alias_is_canonicalized_into_journaled_after_image(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)
    physical = case.vault / "memory" / "case-alias.md"
    physical.write_text("# Case alias\n\nBody.\n", encoding="utf-8")
    sidecar = case.vault / ".datacron" / "ulids.json"
    before = json.dumps(
        {
            "memory/Case-Alias.md": _SIDECAR_ID_A,
            "memory/case-alias.md": _SIDECAR_ID_B,
        },
        sort_keys=True,
    ).encode()
    sidecar.write_bytes(before)

    _bundle, validated = _load_and_validate(case)

    assert validated.identity_sidecar_before_sha256 == sha256_bytes(before)
    assert validated.identity_sidecar_after_bytes is not None
    assert validated.identity_sidecar_after_sha256 == sha256_bytes(
        validated.identity_sidecar_after_bytes
    )
    assert json.loads(validated.identity_sidecar_after_bytes) == {
        "memory/case-alias.md": _SIDECAR_ID_B
    }
    assert validated.identity_sidecar_case_canonicalizations == (
        IdentitySidecarCaseCanonicalization(
            stale_path="memory/Case-Alias.md",
            stale_id=_SIDECAR_ID_A,
            live_path="memory/case-alias.md",
            live_id=_SIDECAR_ID_B,
        ),
    )
    assert validated.identity_sidecar_case_canonicalization_count == 1
    assert validated.identity_sidecar_case_canonicalization_sha256 == (
        hash_identity_sidecar_case_canonicalizations(
            tuple(reversed(validated.identity_sidecar_case_canonicalizations))
        )
    )


def test_sidecar_case_canonicalization_digest_is_order_independent() -> None:
    first = IdentitySidecarCaseCanonicalization(
        stale_path="memory/B.md",
        stale_id=_SIDECAR_ID_A,
        live_path="memory/b.md",
        live_id=_SIDECAR_ID_B,
    )
    second = IdentitySidecarCaseCanonicalization(
        stale_path="memory/A.md",
        stale_id=_UNRELATED_ID,
        live_path="memory/a.md",
        live_id=_GLOBAL_ID,
    )

    assert hash_identity_sidecar_case_canonicalizations((first, second)) == (
        hash_identity_sidecar_case_canonicalizations((second, first))
    )


@pytest.mark.parametrize(
    (
        "primary",
        "migrated",
        "live_ids",
        "live_aliases",
        "operation_paths",
        "result_ids",
        "result_aliases",
    ),
    [
        (
            {
                "memory/Case.md": _SIDECAR_ID_A,
                "memory/case.md": _SIDECAR_ID_B,
                "MEMORY/case.md": _UNRELATED_ID,
            },
            {},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {},
            {},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/Case.md": None, "memory/case.md": None},
            {"memory/Case.md": (), "memory/case.md": ()},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/CASE.md": None},
            {"memory/CASE.md": ()},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/case.md": _SIDECAR_ID_B},
            {"memory/case.md": ()},
            (),
            (),
            (),
        ),
        (
            {
                "memory/Case.md": _SIDECAR_ID_A,
                "memory/case.md": _SIDECAR_ID_B,
                "other/reuse.md": _SIDECAR_ID_A,
            },
            {},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {"memory/Case.md": _SIDECAR_ID_A},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/case.md": None},
            {"memory/case.md": (_SIDECAR_ID_A,)},
            (),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            ("memory/case.md",),
            (),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            (),
            (_SIDECAR_ID_A,),
            (),
        ),
        (
            {"memory/Case.md": _SIDECAR_ID_A, "memory/case.md": _SIDECAR_ID_B},
            {},
            {"memory/case.md": None},
            {"memory/case.md": ()},
            (),
            (),
            (_SIDECAR_ID_A,),
        ),
    ],
)
def test_sidecar_case_canonicalization_fails_closed_on_ambiguous_claims(
    primary: dict[str, str],
    migrated: dict[str, str],
    live_ids: dict[str, str | None],
    live_aliases: dict[str, tuple[str, ...]],
    operation_paths: tuple[str, ...],
    result_ids: tuple[str, ...],
    result_aliases: tuple[str, ...],
) -> None:
    before = dict(primary)

    with pytest.raises(OrganizationManifestError) as error:
        canonicalize_identity_sidecar_case_collisions(
            primary,
            migrated,
            live_frontmatter_ids=live_ids,
            live_aliases=live_aliases,
            operation_paths=operation_paths,
            operation_result_ids=result_ids,
            operation_result_aliases=result_aliases,
        )

    assert error.value.code == "identity_inventory_invalid"
    assert primary == before


def test_existing_empty_identity_sidecar_is_rejected_explicitly(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    (case.vault / ".datacron" / "ulids.json").write_bytes(b"")

    with pytest.raises(OrganizationManifestError, match="must not be empty") as error:
        _load_and_validate(case)

    assert error.value.code == "identity_inventory_invalid"


def test_sidecar_only_existing_source_is_explicitly_unsupported_in_v1(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    source = case.vault / "memory" / "move.md"
    source_bytes = b"# Sidecar-only identity\n\nBody.\n"
    source.write_bytes(source_bytes)
    operation = cast("dict[str, object]", cast("list[object]", case.manifest["operations"])[1])
    operation["expected_sha256"] = sha256_bytes(source_bytes)
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")
    (case.vault / ".datacron" / "ulids.json").write_text(
        json.dumps({"memory/move.md": _MOVE_ID}),
        encoding="utf-8",
    )

    with pytest.raises(OrganizationManifestError, match="no frontmatter id") as error:
        _load_and_validate(case)

    assert error.value.code == "source_identity_invalid"


def test_migrated_sidecar_mapped_source_remains_unsupported(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    (case.vault / ".datacron" / "ulids.json.migrated").write_text(
        json.dumps({"memory/move.md": _MOVE_ID}),
        encoding="utf-8",
    )

    with pytest.raises(OrganizationManifestError, match="migrated ULID sidecar") as error:
        _load_and_validate(case)

    assert error.value.code == "sidecar_migrated_move_unsupported"


def test_new_title_cannot_steal_existing_resolvable_alias(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    operations = cast("list[object]", case.manifest["operations"])
    create = cast("dict[str, object]", operations[2])
    original_digest = cast("str", create["payload_sha256"])
    payload_root = case.manifest_path.parent / "payloads"
    original_path = payload_root / f"{original_digest}.md"
    stolen = original_path.read_bytes().replace(b"created-title", b"replace-old")
    stolen_digest = sha256_bytes(stolen)
    original_path.unlink()
    (payload_root / f"{stolen_digest}.md").write_bytes(stolen)
    create["payload_sha256"] = stolen_digest
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")

    with pytest.raises(OrganizationManifestError, match="replace-old") as error:
        _load_and_validate(case)

    assert error.value.code == "result_alias_unresolved"


def test_config_validation_uses_read_scope_but_rejects_non_organization_change(
    tmp_path: Path,
) -> None:
    target = b"datacron_version: changed\n" + _config(max_kb=121)
    case = _build_case(tmp_path, target_config=target)
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match="only the top-level organization") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "config_scope_violation"


def test_config_scope_comparison_is_type_sensitive(tmp_path: Path) -> None:
    target = b"excluded_folders:\n  - true\n" + _config(max_kb=121)
    case = _build_case(tmp_path, target_config=target)
    live = b"excluded_folders:\n  - 1\n" + _config(max_kb=120)
    (case.vault / ".datacron" / "VAULT.yaml").write_bytes(live)
    config = cast("dict[str, object]", case.manifest["config"])
    config["expected_sha256"] = sha256_bytes(live)
    case.manifest_path.write_text(json.dumps(case.manifest), encoding="utf-8")
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match="only the top-level organization") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "config_scope_violation"


def test_config_payload_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    target = b"organization: {}\n" + _config(max_kb=121)
    case = _build_case(tmp_path, target_config=target)

    with pytest.raises(OrganizationManifestError, match="duplicate mapping key") as error:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)

    assert error.value.code == "config_payload_invalid"


def test_config_payload_rejects_non_string_nested_yaml_keys() -> None:
    with pytest.raises(OrganizationManifestError, match="mapping keys must be strings") as error:
        parse_organization_config_document(
            "query_expansion:\n  1: [one]\n  true: [two]\n",
            label="ambiguous VAULT.yaml",
        )

    assert error.value.code == "config_payload_invalid"


@pytest.mark.parametrize("existing_id", [_GLOBAL_ID, _GLOBAL_ID.lower()])
def test_result_id_must_not_collide_with_unchanged_admitted_note(
    tmp_path: Path,
    existing_id: str,
) -> None:
    case = _build_case(tmp_path, create_id=_GLOBAL_ID)
    (case.vault / "other" / "global.md").write_bytes(
        _note(existing_id, "global-title", (), "outside organization scope")
    )
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match="collides") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "result_id_collision"


def test_result_alias_must_resolve_to_its_id_after_projection(tmp_path: Path) -> None:
    case = _build_case(tmp_path, create_alias="global-title")
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match="does not resolve") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "result_alias_unresolved"


def test_operations_must_remain_inside_organization_scope(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    document = json.loads(case.manifest_path.read_text(encoding="utf-8"))
    document["operations"][2]["target"] = "other/created.md"
    case.manifest_path.write_text(json.dumps(document), encoding="utf-8")
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match=r"outside organization\.scope") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "operation_outside_organization_scope"


def test_existing_operation_source_must_remain_inside_live_organization_scope(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)
    document = json.loads(case.manifest_path.read_text(encoding="utf-8"))
    operation = document["operations"][0]
    payload_root = case.manifest_path.parent / "payloads"
    (payload_root / f"{operation['payload_sha256']}.md").unlink()
    outside_before = (case.vault / "other" / "global.md").read_bytes()
    outside_after = _note(_GLOBAL_ID, "global-title", (), "outside after")
    outside_digest = sha256_bytes(outside_after)
    (payload_root / f"{outside_digest}.md").write_bytes(outside_after)
    operation.update(
        {
            "target": "other/global.md",
            "expected_sha256": sha256_bytes(outside_before),
            "expected": {"id": _GLOBAL_ID, "aliases": []},
            "payload_sha256": outside_digest,
            "result": {"id": _GLOBAL_ID, "aliases": []},
        }
    )
    case.manifest_path.write_text(json.dumps(document), encoding="utf-8")
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match=r"outside organization\.scope") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "operation_outside_organization_scope"


def test_v1_rejects_organization_scope_changes(tmp_path: Path) -> None:
    target_config = (
        b"organization:\n"
        b"  scope: other\n"
        b"  rules:\n"
        b"    - tag: memory/fact\n"
        b"      folder: other\n"
        b"      naming: '{slug}'\n"
        b"      max_kb: 121\n"
    )
    case = _build_case(tmp_path, target_config=target_config)
    bundle = load_organization_bundle(case.manifest_path, vault_root=case.vault)

    with pytest.raises(OrganizationManifestError, match="scope changes") as error:
        validate_organization_bundle(bundle, vault_root=case.vault, scope=_scope(case.vault))

    assert error.value.code == "organization_scope_change_unsupported"


def test_scope_membership_keeps_posix_case_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")

    assert manifest_module._path_belongs_to_organization_scope("memory/a.md", "memory")
    assert not manifest_module._path_belongs_to_organization_scope("Memory/a.md", "memory")


@pytest.mark.parametrize(
    "target",
    [
        "../escape.md",
        "/absolute.md",
        "C:/absolute.md",
        "//server/share/note.md",
        "memory/note.md:stream",
        "memory\\note.md",
    ],
)
def test_manifest_rejects_noncanonical_or_dangerous_note_paths(target: str) -> None:
    with pytest.raises(ValidationError):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "create_exact",
                        "target": target,
                        "payload_sha256": "0" * 64,
                        "result": {"id": _CREATE_ID, "aliases": []},
                    }
                ],
            }
        )


def test_manifest_rejects_case_only_moves_and_operation_chains() -> None:
    common = {
        "expected_sha256": "1" * 64,
        "expected": {"id": _MOVE_ID, "aliases": []},
        "payload_sha256": "2" * 64,
        "result": {"id": _MOVE_ID, "aliases": []},
    }
    with pytest.raises(ValidationError, match="case-only"):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "move_replace_exact",
                        "source": "memory/Note.md",
                        "target": "memory/note.md",
                        **common,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="chains and cycles"):
        _validate_manifest_document(
            {
                "schema": "organization-apply-v1",
                "operations": [
                    {
                        "kind": "move_replace_exact",
                        "source": "memory/a.md",
                        "target": "memory/b.md",
                        **common,
                    },
                    {
                        "kind": "create_exact",
                        "target": "memory/a.md",
                        "payload_sha256": "3" * 64,
                        "result": {"id": _CREATE_ID, "aliases": []},
                    },
                ],
            }
        )


def test_bundle_rejects_unreferenced_payload_and_hash_mismatch(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    payload_root = case.manifest_path.parent / "payloads"
    (payload_root / f"{'f' * 64}.md").write_text("orphan", encoding="utf-8")

    with pytest.raises(OrganizationManifestError) as unreferenced:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)
    assert unreferenced.value.code == "payload_set_mismatch"

    (payload_root / f"{'f' * 64}.md").unlink()
    payload_path = next(payload_root.glob("*.md"))
    payload_path.write_bytes(payload_path.read_bytes() + b"tampered")
    with pytest.raises(OrganizationManifestError) as mismatch:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)
    assert mismatch.value.code == "payload_hash_mismatch"


def test_bundle_rejects_invalid_utf8_payload(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    payload_root = case.manifest_path.parent / "payloads"
    payload_path = next(payload_root.glob("*.md"))
    invalid = b"\xff"
    invalid_digest = sha256_bytes(invalid)
    original_digest = payload_path.stem
    payload_path.unlink()
    (payload_root / f"{invalid_digest}.md").write_bytes(invalid)
    manifest_text = case.manifest_path.read_text(encoding="utf-8").replace(
        original_digest,
        invalid_digest,
        1,
    )
    case.manifest_path.write_text(manifest_text, encoding="utf-8")

    with pytest.raises(OrganizationManifestError) as error:
        load_organization_bundle(case.manifest_path, vault_root=case.vault)
    assert error.value.code == "invalid_utf8"


def test_path_chain_guard_rejects_relative_and_linked_paths(tmp_path: Path) -> None:
    with pytest.raises(LinkedPathError, match="absolute"):
        assert_path_chain_without_links(Path("relative"))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(LinkedPathError, match="Linked path component"):
        assert_path_chain_without_links(link)


def test_path_chain_guard_rejects_windows_reparse_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "junction"
    candidate.mkdir()
    real_lstat = os.lstat

    def fake_lstat(path: os.PathLike[str] | str) -> os.stat_result:
        result = real_lstat(path)
        if Path(path) == candidate:
            return cast(
                "os.stat_result",
                SimpleNamespace(
                    st_mode=result.st_mode,
                    st_file_attributes=0x0400,
                ),
            )
        return result

    monkeypatch.setattr(os, "lstat", fake_lstat)

    with pytest.raises(LinkedPathError, match="Linked path component"):
        assert_path_chain_without_links(candidate)
