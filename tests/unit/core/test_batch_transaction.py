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
"""Tests for crash-consistent organization batch transactions."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from datacron.core import batch_transaction
from datacron.core.batch_transaction import (
    BATCH_FAULT_POINTS,
    BatchApplyResult,
    BatchConflictError,
    OrganizationBatchTransaction,
)
from datacron.core.config import Settings, VaultConfig
from datacron.core.durability import RecoveryRequiredError
from datacron.core.hashing import sha256_bytes
from datacron.core.operation_log import (
    OperationContext,
    OperationJournal,
    OperationLogError,
    OperationRecord,
)
from datacron.core.paths import PathConfinementError
from datacron.core.scope import assert_path_chain_without_links
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.organization.manifest import (
    MAX_MANIFEST_BYTES,
    MAX_PAYLOAD_BYTES,
    CreateExactOperation,
    ExistingNoteIdentity,
    IdentitySidecarCaseCanonicalization,
    MoveReplaceExactOperation,
    NoteIdentity,
    OrganizationManifest,
    OrganizationPayload,
    OrganizationScopeNotePrecondition,
    ReplaceExactOperation,
    ResolvedOrganizationOperation,
    ValidatedOrganizationBundle,
    VaultConfigReplaceExact,
    hash_identity_sidecar_case_canonicalizations,
)

_FIRST_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
_SECOND_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX6"
_THIRD_ID = "01HQXR7K9YZ8M2N3PQRSTV4WX7"
_CONFIRMATION_TOKEN = "c" * 64
_PROJECTED_REPORT_SHA256 = "d" * 64
_LEGACY_EXISTING_ID = "01KTBQ9F7M0N1HEIMDRELEAS01"
_ACTIVE_CONFIG = """organization:
  scope: notes
  rules:
    - tag: memory/fact
      folder: notes
      naming: '{slug}.md'
"""


def _ordinary_record(
    operation_id: str,
    timestamp: datetime,
    before_hash: str,
    after_hash: str,
) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        timestamp=timestamp.isoformat(timespec="microseconds"),
        op="patch_section",
        tool="patch_note_section",
        note_id="01J00000000000000000000042",
        rel_path="note.md",
        before_hash=before_hash,
        after_hash=after_hash,
        actor="unit-test",
        parameters={"new_content_chars": 3},
        history_stored=True,
    )


class _SimulatedProcessCrash(BaseException):
    """Bypass synchronous exception rollback to model process termination."""


def _context(bundle: ValidatedOrganizationBundle) -> OperationContext:
    operation_count = len(bundle.operations) + int(bundle.config_payload is not None)
    return OperationContext(
        op="apply_organization_manifest",
        tool="apply_organization_manifest",
        actor="batch-test",
        parameters={
            "manifest_sha256": bundle.manifest_sha256,
            "payload_set_sha256": bundle.payload_set_sha256,
            "scope_digest": bundle.scope_digest,
            "projected_report_sha256": _PROJECTED_REPORT_SHA256,
            "operation_count": operation_count,
            "total_payload_bytes": bundle.total_payload_bytes,
            "config_replaced": bundle.config_payload is not None,
        },
    )


def _note(
    note_id: str,
    title: str,
    *,
    aliases: tuple[str, ...] = (),
    bom: bool = False,
    eol: str = "\n",
) -> bytes:
    aliases_json = json.dumps(list(aliases), ensure_ascii=False)
    text = f"---\nid: {note_id}\ntitle: {title}\naliases: {aliases_json}\n---\n# {title}\n"
    rendered = text.replace("\n", eol).encode("utf-8")
    return (b"\xef\xbb\xbf" + rendered) if bom else rendered


def _note_without_id(title: str, *, aliases: tuple[str, ...] = ()) -> bytes:
    aliases_json = json.dumps(list(aliases), ensure_ascii=False)
    return (f"---\ntitle: {title}\naliases: {aliases_json}\n---\n# {title}\n").encode()


def _payload(root: Path, index: int, raw_bytes: bytes, suffix: str = ".md") -> OrganizationPayload:
    payload_hash = sha256_bytes(raw_bytes)
    payload_path = root / "bundle" / "payloads" / f"{payload_hash}{suffix}"
    return OrganizationPayload(
        sha256=payload_hash,
        path=payload_path,
        raw_bytes=raw_bytes,
        text=raw_bytes.decode("utf-8-sig"),
        suffix=suffix,  # type: ignore[arg-type]
    )


def _validated_bundle(
    root: Path,
    operations: tuple[ResolvedOrganizationOperation, ...],
    *,
    config: VaultConfigReplaceExact | None = None,
    config_payload: OrganizationPayload | None = None,
    identity_sidecar_before_bytes: bytes | None = None,
    identity_sidecar_after_bytes: bytes | None = None,
    identity_sidecar_case_canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...] = (),
) -> ValidatedOrganizationBundle:
    config_path = root / ".datacron" / "VAULT.yaml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_ACTIVE_CONFIG, encoding="utf-8", newline="\n")
    (root / "notes").mkdir(exist_ok=True)
    manifest = OrganizationManifest(
        schema="organization-apply-v1",
        operations=tuple(item.operation for item in operations),
        config=config,
    )
    manifest_bytes = manifest.model_dump_json().encode("utf-8")
    payloads = {(item.payload.sha256, item.payload.suffix): item.payload for item in operations}
    if config_payload is not None:
        payloads[(config_payload.sha256, config_payload.suffix)] = config_payload
    identity_sidecar_path = root / ".datacron" / "ulids.json"
    if identity_sidecar_before_bytes is not None:
        identity_sidecar_path.write_bytes(identity_sidecar_before_bytes)
    if identity_sidecar_after_bytes is not None and not identity_sidecar_path.is_file():
        raise ValueError("identity sidecar test update requires a baseline file")
    target_config = VaultConfig.model_validate(
        {
            "organization": {
                "scope": "notes",
                "rules": [
                    {
                        "tag": "memory/fact",
                        "folder": "notes",
                        "naming": "{slug}.md",
                    }
                ],
            }
        }
    )
    scope_note_preconditions = tuple(
        OrganizationScopeNotePrecondition(
            rel_path=path.relative_to(root).as_posix(),
            sha256=sha256_bytes(path.read_bytes()),
        )
        for path in sorted((root / "notes").rglob("*.md"))
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )
    migrated_identity_sidecar_path = root / ".datacron" / "ulids.json.migrated"
    return ValidatedOrganizationBundle(
        manifest_path=root / "bundle" / "manifest.json",
        bundle_root=root / "bundle",
        manifest_bytes=manifest_bytes,
        manifest_sha256=sha256_bytes(manifest_bytes),
        payload_set_sha256="e" * 64,
        manifest=manifest,
        payloads=MappingProxyType(payloads),
        total_payload_bytes=sum(len(payload.raw_bytes) for payload in payloads.values()),
        operations=operations,
        config_path=config_path if config else None,
        config_payload=config_payload,
        config_before_sha256=sha256_bytes(config_path.read_bytes()),
        target_config=target_config,
        projected_notes=(),
        identity_sidecar_path=identity_sidecar_path,
        identity_sidecar_before_sha256=(
            sha256_bytes(identity_sidecar_path.read_bytes())
            if identity_sidecar_path.is_file()
            else None
        ),
        identity_sidecar_after_bytes=identity_sidecar_after_bytes,
        identity_sidecar_after_sha256=(
            sha256_bytes(identity_sidecar_after_bytes)
            if identity_sidecar_after_bytes is not None
            else None
        ),
        migrated_identity_sidecar_path=migrated_identity_sidecar_path,
        migrated_identity_sidecar_before_sha256=(
            sha256_bytes(migrated_identity_sidecar_path.read_bytes())
            if migrated_identity_sidecar_path.is_file()
            else None
        ),
        scope_note_preconditions=scope_note_preconditions,
        scope_note_count=len(scope_note_preconditions),
        scope_digest="f" * 64,
        identity_sidecar_case_canonicalizations=(identity_sidecar_case_canonicalizations),
    )


def _create_bundle(root: Path, raw_bytes: bytes) -> ValidatedOrganizationBundle:
    payload = _payload(root, 0, raw_bytes)
    operation = CreateExactOperation(
        kind="create_exact",
        target="notes/new.md",
        payload_sha256=payload.sha256,
        result=NoteIdentity(id=_FIRST_ID),
    )
    return _validated_bundle(
        root,
        (
            ResolvedOrganizationOperation(
                operation=operation,
                source_path=None,
                target_path=root / operation.target,
                payload=payload,
            ),
        ),
    )


def _sidecar_bytes(mappings: dict[str, str]) -> bytes:
    return (json.dumps(mappings, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _move_bundle_with_sidecar(
    root: Path,
    *,
    aliases: tuple[str, ...] = (),
    result_aliases: tuple[str, ...] | None = None,
) -> ValidatedOrganizationBundle:
    resolved_result_aliases = aliases if result_aliases is None else result_aliases
    source = root / "notes" / "old.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    before = _note(_FIRST_ID, "Before", aliases=aliases)
    after = _note(_FIRST_ID, "After", aliases=resolved_result_aliases)
    source.write_bytes(before)
    payload = _payload(root, 0, after)
    expected_identity = ExistingNoteIdentity(id=_FIRST_ID, aliases=aliases)
    result_identity = ExistingNoteIdentity(
        id=_FIRST_ID,
        aliases=resolved_result_aliases,
    )
    operation = MoveReplaceExactOperation(
        kind="move_replace_exact",
        source="notes/old.md",
        target="notes/new.md",
        expected_sha256=sha256_bytes(before),
        expected=expected_identity,
        payload_sha256=payload.sha256,
        result=result_identity,
    )
    return _validated_bundle(
        root,
        (
            ResolvedOrganizationOperation(
                operation=operation,
                source_path=source,
                target_path=root / operation.target,
                payload=payload,
            ),
        ),
        identity_sidecar_before_bytes=_sidecar_bytes({operation.source: _FIRST_ID}),
        identity_sidecar_after_bytes=_sidecar_bytes({operation.target: _FIRST_ID}),
    )


def _case_canonicalization_bundle(
    root: Path,
) -> tuple[
    ValidatedOrganizationBundle,
    IdentitySidecarCaseCanonicalization,
    bytes,
    bytes,
]:
    live_path = root / "notes" / "live.md"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_bytes(_note_without_id("Live"))
    create_bundle = _create_bundle(root, _note(_FIRST_ID, "After"))
    canonicalization = IdentitySidecarCaseCanonicalization(
        stale_path="Notes/Live.md",
        stale_id=_SECOND_ID,
        live_path="notes/live.md",
        live_id=_LEGACY_EXISTING_ID,
    )
    before = _sidecar_bytes(
        {
            canonicalization.stale_path: canonicalization.stale_id,
            canonicalization.live_path: canonicalization.live_id,
        }
    )
    after = _sidecar_bytes({canonicalization.live_path: canonicalization.live_id})
    bundle = _validated_bundle(
        root,
        (create_bundle.operations[0],),
        identity_sidecar_before_bytes=before,
        identity_sidecar_after_bytes=after,
        identity_sidecar_case_canonicalizations=(canonicalization,),
    )
    return bundle, canonicalization, before, after


async def _apply(
    writer: FilesystemVaultWriter,
    bundle: ValidatedOrganizationBundle,
    *,
    validator: Callable[[], None] = lambda: None,
    fault_injector: Callable[[str], None] | None = None,
) -> BatchApplyResult:
    return await writer.apply_organization_manifest(
        bundle,
        confirmation_token=_CONFIRMATION_TOKEN,
        projected_report_sha256=_PROJECTED_REPORT_SHA256,
        precommit_validator=validator,
        operation=_context(bundle),
        fault_injector=fault_injector,
    )


def _transaction(writer: FilesystemVaultWriter) -> OrganizationBatchTransaction:
    return writer._batch_transaction


def _pending_path(root: Path, bundle: ValidatedOrganizationBundle) -> Path:
    return root / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"


def _stage_path(
    root: Path,
    bundle: ValidatedOrganizationBundle,
    stage_name: str,
) -> Path:
    return root / ".datacron" / "oplog" / "batches" / "stage" / bundle.manifest_sha256 / stage_name


async def _leave_pending(
    writer: FilesystemVaultWriter,
    bundle: ValidatedOrganizationBundle,
) -> None:
    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)


async def test_fresh_vault_creates_batch_directories_and_preserves_exact_bytes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    exact = _note(_FIRST_ID, "Exact", bom=True, eol="\r\n")
    bundle = _create_bundle(vault, exact)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))
    callback_observations: list[bool] = []

    def validate_before_stage() -> None:
        callback_observations.append((vault / ".datacron" / "oplog" / "batches").exists())

    result = await _apply(writer, bundle, validator=validate_before_stage)

    assert callback_observations == [False]
    assert (vault / "notes" / "new.md").read_bytes() == exact
    assert result.manifest_sha256 == bundle.manifest_sha256
    assert result.confirmation_token == _CONFIRMATION_TOKEN
    assert result.projected_report_sha256 == _PROJECTED_REPORT_SHA256
    assert result.already_committed is False
    batches = vault / ".datacron" / "oplog" / "batches"
    assert (batches / "committed" / f"{bundle.manifest_sha256}.json").is_file()
    assert not list((batches / "pending").glob("*.json"))
    assert not list((batches / "stage").glob(bundle.manifest_sha256))

    replay = await writer.get_organization_batch_result(bundle.manifest_sha256)

    assert replay is not None
    assert replay.already_committed is True
    assert replay.confirmation_token == _CONFIRMATION_TOKEN
    assert replay.projected_report_sha256 == _PROJECTED_REPORT_SHA256
    assert replay.payload_set_sha256 == result.payload_set_sha256
    assert replay.scope_digest == result.scope_digest
    assert replay.config_before_sha256 == result.config_before_sha256


async def test_committed_result_rejects_target_returned_to_before_state(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))
    await _apply(writer, bundle)
    (vault / "notes" / "new.md").unlink()

    with pytest.raises(RecoveryRequiredError, match="target differs from receipt"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_committed_result_rejects_reappeared_move_source(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))
    await _apply(writer, bundle)
    (vault / "notes" / "old.md").write_bytes(_note(_FIRST_ID, "Before"))

    with pytest.raises(RecoveryRequiredError, match="move source reappeared"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_move_applies_derived_identity_sidecar_as_internal_last_member(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(
        vault,
        aliases=("Old alias",),
        result_aliases=("New alias",),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    result = await _apply(writer, bundle)

    expected_sidecar = _sidecar_bytes({"notes/new.md": _FIRST_ID})
    assert [member.kind for member in result.members] == [
        "move_replace_exact",
        "identity_sidecar_replace_exact",
    ]
    assert result.payload_set_sha256 == bundle.payload_set_sha256
    assert result.scope_digest == bundle.scope_digest
    assert result.config_before_sha256 == bundle.config_before_sha256
    assert (vault / "notes" / "new.md").read_bytes() == _note(
        _FIRST_ID,
        "After",
        aliases=("New alias",),
    )
    assert not (vault / "notes" / "old.md").exists()
    assert (vault / ".datacron" / "ulids.json").read_bytes() == expected_sidecar
    records = await writer.list_operations()
    assert len(records) == 2
    for record in records:
        assert record.parameters["operation_count"] == 1
        assert record.parameters["member_count"] == 2
        assert record.parameters["identity_sidecar_replaced"] is True
        assert record.parameters["total_payload_bytes"] == (
            len(bundle.operations[0].payload.raw_bytes) + len(expected_sidecar)
        )
    assert records[-1].note_id is None
    assert records[-1].parameters["batch_member_kind"] == ("identity_sidecar_replace_exact")


async def test_committed_sidecar_diff_exposes_only_ids_removed_without_remap(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle, canonicalization, before, _after = _case_canonicalization_bundle(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    result = await _apply(writer, bundle)

    history_path = vault / ".datacron" / "history" / sha256_bytes(before)
    history_path.unlink()

    assert await writer.get_organization_removed_identity_ids(result) == (
        canonicalization.stale_id,
    )


async def test_sidecar_path_move_does_not_expose_retained_identity_for_index_deletion(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    result = await _apply(writer, bundle)

    assert await writer.get_organization_removed_identity_ids(result) == ()


@pytest.mark.parametrize(
    "field",
    ["payload_set_sha256", "scope_digest", "config_before_sha256"],
)
async def test_committed_result_metadata_roundtrips_and_tampering_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Exact"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    result = await _apply(writer, bundle)
    committed_path = (
        vault / ".datacron" / "oplog" / "batches" / "committed" / f"{bundle.manifest_sha256}.json"
    )
    committed = json.loads(committed_path.read_text(encoding="ascii"))

    assert committed["payload_set_sha256"] == result.payload_set_sha256
    assert committed["scope_digest"] == result.scope_digest
    assert committed["config_before_sha256"] == result.config_before_sha256

    committed[field] = "a" * 64
    committed_path.write_text(json.dumps(committed), encoding="ascii")

    with pytest.raises(RecoveryRequiredError, match="operation log"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_committed_case_canonicalization_tampering_is_rejected_against_log(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle, _canonicalization, _before, _after = _case_canonicalization_bundle(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _apply(writer, bundle)
    committed_path = (
        vault / ".datacron" / "oplog" / "batches" / "committed" / f"{bundle.manifest_sha256}.json"
    )
    committed = json.loads(committed_path.read_text(encoding="ascii"))
    committed["identity_sidecar_case_canonicalizations"][0]["stale_id"] = _THIRD_ID
    committed_path.write_text(json.dumps(committed), encoding="ascii")

    with pytest.raises(RecoveryRequiredError, match="operation log"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_committed_result_copied_under_another_hash_is_rejected(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Committed"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _apply(writer, bundle)
    committed_root = vault / ".datacron" / "oplog" / "batches" / "committed"
    original = committed_root / f"{bundle.manifest_sha256}.json"
    requested_hash = "a" * 64
    copied = committed_root / f"{requested_hash}.json"
    copied.write_bytes(original.read_bytes())

    with pytest.raises(RecoveryRequiredError, match="identity differs from filename"):
        await writer.get_organization_batch_result(requested_hash)


async def test_committed_result_member_prefix_cannot_hide_later_batch_records(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first_payload = _payload(vault, 0, _note(_FIRST_ID, "First"))
    second_payload = _payload(vault, 1, _note(_SECOND_ID, "Second"))
    first = CreateExactOperation(
        kind="create_exact",
        target="notes/first.md",
        payload_sha256=first_payload.sha256,
        result=NoteIdentity(id=_FIRST_ID),
    )
    second = CreateExactOperation(
        kind="create_exact",
        target="notes/second.md",
        payload_sha256=second_payload.sha256,
        result=NoteIdentity(id=_SECOND_ID),
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                first,
                None,
                vault / first.target,
                first_payload,
            ),
            ResolvedOrganizationOperation(
                second,
                None,
                vault / second.target,
                second_payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _apply(writer, bundle)
    result_path = (
        vault / ".datacron" / "oplog" / "batches" / "committed" / f"{bundle.manifest_sha256}.json"
    )
    receipt = json.loads(result_path.read_text(encoding="ascii"))
    receipt["members"] = receipt["members"][:1]
    result_path.write_text(json.dumps(receipt), encoding="ascii")

    with pytest.raises(RecoveryRequiredError, match="complete operation-record set"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_move_sidecar_recovery_authenticates_before_and_result_aliases(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(
        vault,
        aliases=("Old alias",),
        result_aliases=("New alias",),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))
    writes = 0

    def crash_after_sidecar_write(point: str) -> None:
        nonlocal writes
        if point != "after_member_write":
            return
        writes += 1
        if writes == 2:
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_sidecar_write)

    pending = json.loads(_pending_path(vault, bundle).read_text(encoding="ascii"))
    assert pending["members"][0]["before_aliases"] == ["Old alias"]
    assert pending["members"][0]["aliases"] == ["New alias"]
    assert pending["members"][1]["before_aliases"] is None
    assert pending["members"][1]["aliases"] is None
    assert writes == 2
    assert (vault / "notes" / "old.md").is_file()
    assert (vault / "notes" / "new.md").is_file()
    assert (vault / ".datacron" / "ulids.json").read_bytes() == _sidecar_bytes(
        {"notes/new.md": _FIRST_ID}
    )
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    assert await restarted.recover_operations() == 1
    assert restarted.recovery_blocked == ()
    assert not (vault / "notes" / "old.md").exists()
    assert (vault / "notes" / "new.md").read_bytes() == _note(
        _FIRST_ID,
        "After",
        aliases=("New alias",),
    )
    assert (vault / ".datacron" / "ulids.json").read_bytes() == _sidecar_bytes(
        {"notes/new.md": _FIRST_ID}
    )


async def test_case_canonicalization_recovers_after_pending_publish(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle, canonicalization, _before, after = _case_canonicalization_bundle(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    await _leave_pending(writer, bundle)

    pending = json.loads(_pending_path(vault, bundle).read_text(encoding="ascii"))
    expected_records = [
        {
            "stale_path": canonicalization.stale_path,
            "stale_id": canonicalization.stale_id,
            "live_path": canonicalization.live_path,
            "live_id": canonicalization.live_id,
        }
    ]
    assert pending["identity_sidecar_case_canonicalizations"] == expected_records
    assert pending["parameters"]["identity_sidecar_case_canonicalization_count"] == 1
    assert pending["parameters"]["identity_sidecar_case_canonicalization_sha256"] == (
        hash_identity_sidecar_case_canonicalizations((canonicalization,))
    )
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 1
    assert restarted.recovery_blocked == ()
    assert (vault / ".datacron" / "ulids.json").read_bytes() == after
    result = await restarted.get_organization_batch_result(bundle.manifest_sha256)
    assert result is not None
    assert result.identity_sidecar_case_canonicalizations == (canonicalization,)
    assert result.identity_sidecar_case_canonicalization_count == 1
    assert result.identity_sidecar_case_canonicalization_sha256 == (
        hash_identity_sidecar_case_canonicalizations((canonicalization,))
    )
    committed_path = (
        vault / ".datacron" / "oplog" / "batches" / "committed" / f"{bundle.manifest_sha256}.json"
    )
    committed = json.loads(committed_path.read_text(encoding="ascii"))
    assert committed["identity_sidecar_case_canonicalizations"] == expected_records
    for record in await restarted.list_operations():
        assert record.parameters["identity_sidecar_case_canonicalization_count"] == 1
        assert record.parameters["identity_sidecar_case_canonicalization_sha256"] == (
            result.identity_sidecar_case_canonicalization_sha256
        )


@pytest.mark.parametrize("tamper", ["missing", "forged"])
async def test_case_canonicalization_pending_tampering_blocks_recovery(
    tmp_path: Path,
    tamper: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle, canonicalization, before, _after = _case_canonicalization_bundle(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    if tamper == "missing":
        forged_records: tuple[IdentitySidecarCaseCanonicalization, ...] = ()
    else:
        forged_records = (replace(canonicalization, stale_id=_THIRD_ID),)
    pending["identity_sidecar_case_canonicalizations"] = [
        {
            "stale_path": item.stale_path,
            "stale_id": item.stale_id,
            "live_path": item.live_path,
            "live_id": item.live_id,
        }
        for item in forged_records
    ]
    pending["parameters"]["identity_sidecar_case_canonicalization_count"] = len(forged_records)
    pending["parameters"]["identity_sidecar_case_canonicalization_sha256"] = (
        hash_identity_sidecar_case_canonicalizations(forged_records)
    )
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert (vault / ".datacron" / "ulids.json").read_bytes() == before
    assert not (vault / "notes" / "new.md").exists()


async def test_tampered_derived_sidecar_stage_blocks_recovery(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    sidecar_member = pending["members"][-1]
    sidecar_stage = _stage_path(vault, bundle, sidecar_member["stage_name"])
    original = sidecar_stage.read_bytes()
    tampered = _sidecar_bytes(
        {
            "notes/new.md": _FIRST_ID,
            "notes/unrelated.md": _SECOND_ID,
        }
    )
    sidecar_stage.write_bytes(tampered)
    sidecar_member["after_hash"] = sha256_bytes(tampered)
    pending["parameters"]["total_payload_bytes"] += len(tampered) - len(original)
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert (vault / "notes" / "old.md").is_file()
    assert not (vault / "notes" / "new.md").exists()
    assert (vault / ".datacron" / "ulids.json").read_bytes() == _sidecar_bytes(
        {"notes/old.md": _FIRST_ID}
    )


async def test_recovery_derives_required_sidecar_member_even_if_receipt_omits_it(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    sidecar_member = pending["members"].pop()
    sidecar_stage = _stage_path(vault, bundle, sidecar_member["stage_name"])
    sidecar_size = len(sidecar_stage.read_bytes())
    sidecar_stage.unlink()
    pending["parameters"]["member_count"] = 1
    pending["parameters"]["identity_sidecar_replaced"] = False
    pending["parameters"]["total_payload_bytes"] -= sidecar_size
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert (vault / "notes" / "old.md").is_file()
    assert not (vault / "notes" / "new.md").exists()
    assert (vault / ".datacron" / "ulids.json").read_bytes() == _sidecar_bytes(
        {"notes/old.md": _FIRST_ID}
    )


async def test_sidecar_recovery_rejects_migrated_target_collision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    migrated_path = vault / ".datacron" / "ulids.json.migrated"
    migrated_path.parent.mkdir(parents=True)
    migrated_path.write_bytes(_sidecar_bytes({"notes/new.md": _SECOND_ID}))
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert (vault / "notes" / "old.md").is_file()
    assert not (vault / "notes" / "new.md").exists()


@pytest.mark.parametrize(
    ("tampered_note", "pending_field", "pending_value"),
    [
        (_note(_SECOND_ID, "After"), "note_id", _FIRST_ID),
        (
            _note(_FIRST_ID, "After", aliases=("Forged",)),
            "aliases",
            [],
        ),
        (
            (
                f"---\nid: {_FIRST_ID}\nid: {_FIRST_ID}\ntitle: After\naliases: []\n---\n# After\n"
            ).encode(),
            "aliases",
            [],
        ),
    ],
    ids=["id", "aliases", "duplicate-frontmatter-key"],
)
async def test_tampered_staged_note_identity_blocks_recovery(
    tmp_path: Path,
    tampered_note: bytes,
    pending_field: str,
    pending_value: object,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    member = pending["members"][0]
    stage_path = _stage_path(vault, bundle, member["stage_name"])
    original = stage_path.read_bytes()
    stage_path.write_bytes(tampered_note)
    member["after_hash"] = sha256_bytes(tampered_note)
    member[pending_field] = pending_value
    pending["parameters"]["total_payload_bytes"] += len(tampered_note) - len(original)
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


async def test_global_note_added_outside_scope_cannot_duplicate_result_id(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    outside_scope = vault / "outside" / "collision.md"
    outside_scope.parent.mkdir()
    outside_scope.write_bytes(_note(_FIRST_ID, "Collision"))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


async def test_forged_two_create_stages_cannot_share_one_result_id(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = _note(_FIRST_ID, "First")
    second = _note(_SECOND_ID, "Second")
    first_payload = _payload(vault, 0, first)
    second_payload = _payload(vault, 1, second)
    first_operation = CreateExactOperation(
        kind="create_exact",
        target="notes/first.md",
        payload_sha256=first_payload.sha256,
        result=NoteIdentity(id=_FIRST_ID),
    )
    second_operation = CreateExactOperation(
        kind="create_exact",
        target="notes/second.md",
        payload_sha256=second_payload.sha256,
        result=NoteIdentity(id=_SECOND_ID),
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                first_operation,
                None,
                vault / first_operation.target,
                first_payload,
            ),
            ResolvedOrganizationOperation(
                second_operation,
                None,
                vault / second_operation.target,
                second_payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    second_member = pending["members"][1]
    second_stage = _stage_path(vault, bundle, second_member["stage_name"])
    original = second_stage.read_bytes()
    forged = _note(_FIRST_ID, "Second")
    second_stage.write_bytes(forged)
    second_member["note_id"] = _FIRST_ID
    second_member["after_hash"] = sha256_bytes(forged)
    pending["parameters"]["total_payload_bytes"] += len(forged) - len(original)
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "first.md").exists()
    assert not (vault / "notes" / "second.md").exists()


async def test_result_alias_cannot_be_stolen_by_global_title_outside_scope(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    result_bytes = _note(_FIRST_ID, "After", aliases=("Owned",))
    payload = _payload(vault, 0, result_bytes)
    operation = CreateExactOperation(
        kind="create_exact",
        target="notes/new.md",
        payload_sha256=payload.sha256,
        result=NoteIdentity(id=_FIRST_ID, aliases=("Owned",)),
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation,
                None,
                vault / operation.target,
                payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    outside_scope = vault / "outside" / "owned.md"
    outside_scope.parent.mkdir()
    outside_scope.write_bytes(_note(_SECOND_ID, "Owned"))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


async def test_result_id_cannot_use_hidden_sidecar_reservation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    payload = _payload(vault, 0, _note(_FIRST_ID, "After"))
    operation = CreateExactOperation(
        kind="create_exact",
        target="notes/new.md",
        payload_sha256=payload.sha256,
        result=NoteIdentity(id=_FIRST_ID),
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation,
                None,
                vault / operation.target,
                payload,
            ),
        ),
        identity_sidecar_before_bytes=_sidecar_bytes({".hidden/reserved.md": _FIRST_ID}),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


async def test_precommit_failure_leaves_zero_batch_or_vault_artifacts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Rejected"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def reject() -> None:
        raise BatchConflictError("projection changed")

    with pytest.raises(BatchConflictError, match="projection changed"):
        await _apply(writer, bundle, validator=reject)

    assert not (vault / "notes" / "new.md").exists()
    assert not (vault / ".datacron" / "oplog" / "batches").exists()


def test_validate_capacity_is_read_only_and_bounds_actor(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Capacity"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(writer)

    transaction.validate_capacity(
        bundle,
        confirmation_token=_CONFIRMATION_TOKEN,
        projected_report_sha256=_PROJECTED_REPORT_SHA256,
        operation=_context(bundle),
    )

    assert not (vault / "notes" / "new.md").exists()
    assert not (vault / ".datacron" / "oplog").exists()
    oversized_actor = OperationContext(
        op="apply_organization_manifest",
        tool="apply_organization_manifest",
        actor="x" * 257,
        parameters=_context(bundle).parameters,
    )
    with pytest.raises(BatchConflictError, match="actor exceeds 256"):
        transaction.validate_capacity(
            bundle,
            confirmation_token=_CONFIRMATION_TOKEN,
            projected_report_sha256=_PROJECTED_REPORT_SHA256,
            operation=oversized_actor,
        )
    assert not (vault / ".datacron" / "oplog").exists()


async def test_pending_detection_blocks_capacity_until_recovery(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Pending"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(writer)
    assert transaction.has_pending_batches() is False

    await _leave_pending(writer, bundle)

    assert transaction.has_pending_batches() is True
    pending_before = _pending_path(vault, bundle).read_bytes()
    stage_before = _stage_path(vault, bundle, "0000.after").read_bytes()
    with pytest.raises(BatchConflictError, match="requires recovery before preview"):
        transaction.validate_capacity(
            bundle,
            confirmation_token=_CONFIRMATION_TOKEN,
            projected_report_sha256=_PROJECTED_REPORT_SHA256,
            operation=_context(bundle),
        )
    assert _pending_path(vault, bundle).read_bytes() == pending_before
    assert _stage_path(vault, bundle, "0000.after").read_bytes() == stage_before

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    assert _transaction(restarted).has_pending_batches() is True
    assert await restarted.recover_operations() == 1
    assert _transaction(restarted).has_pending_batches() is False


async def test_apply_never_publishes_a_pending_receipt_recovery_cannot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Rejected"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    oversized = b"x" * (MAX_MANIFEST_BYTES + 1)
    monkeypatch.setattr(batch_transaction, "_pending_bytes", lambda _pending: oversized)

    with pytest.raises(BatchConflictError, match="pending organization batch exceeds"):
        await _apply(writer, bundle)

    assert not (vault / "notes" / "new.md").exists()
    assert not (vault / ".datacron" / "oplog" / "batches").exists()


async def test_apply_rolls_back_when_pending_publish_succeeds_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Published"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(writer)
    write_pending = transaction._write_pending

    def publish_then_raise(pending: batch_transaction._PendingBatch) -> None:
        write_pending(pending)
        raise RuntimeError("pending publish acknowledgement failed")

    monkeypatch.setattr(transaction, "_write_pending", publish_then_raise)

    with pytest.raises(RuntimeError, match="publish acknowledgement failed"):
        await _apply(writer, bundle)

    assert not (vault / "notes" / "new.md").exists()
    assert not _pending_path(vault, bundle).exists()
    assert not _stage_path(vault, bundle, "0000.after").exists()


async def test_recovery_stops_if_pending_receipt_changes_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Snapshot"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(restarted)
    read_records = transaction._read_records
    pending_path = _pending_path(vault, bundle)
    swapped = False

    def read_records_then_swap() -> list[OperationRecord]:
        nonlocal swapped
        records = read_records()
        if not swapped:
            pending_path.write_bytes(pending_path.read_bytes() + b" ")
            swapped = True
        return records

    monkeypatch.setattr(transaction, "_read_records", read_records_then_swap)

    with pytest.raises(RecoveryRequiredError, match="changed during recovery"):
        await restarted.recover_operations()

    assert pending_path.exists()
    assert _stage_path(vault, bundle, "0000.after").exists()
    assert not (vault / "notes" / "new.md").exists()


async def test_recovery_revalidates_pending_snapshot_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Cleanup guard"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(restarted)
    write_result = transaction._write_result
    pending_path = _pending_path(vault, bundle)

    def write_result_then_swap(result: BatchApplyResult) -> None:
        write_result(result)
        pending_path.write_bytes(pending_path.read_bytes() + b" ")

    monkeypatch.setattr(transaction, "_write_result", write_result_then_swap)

    with pytest.raises(RecoveryRequiredError, match="changed during recovery"):
        await restarted.recover_operations()

    assert pending_path.exists()
    assert _stage_path(vault, bundle, "0000.after").exists()
    assert (vault / "notes" / "new.md").read_bytes() == _note(
        _FIRST_ID,
        "Cleanup guard",
    )


async def test_atomic_receipt_temps_are_ignored_and_cleaned_with_orphan_stage(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Temp"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    batches = vault / ".datacron" / "oplog" / "batches"
    pending_root = batches / "pending"
    committed_root = batches / "committed"
    stage_dir = batches / "stage" / bundle.manifest_sha256
    pending_root.mkdir(parents=True)
    committed_root.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    pending_temp = pending_root / f".{bundle.manifest_sha256}.json.{'1' * 32}.tmp"
    committed_temp = committed_root / f".{bundle.manifest_sha256}.json.{'2' * 32}.tmp"
    pending_temp.write_bytes(b"pending temp")
    committed_temp.write_bytes(b"committed temp")
    (stage_dir / "0000.after").write_bytes(b"orphan stage")

    assert _transaction(writer).has_pending_batches() is False
    assert await writer.recover_operations() == 0

    assert not pending_temp.exists()
    assert not committed_temp.exists()
    assert not stage_dir.exists()


async def test_oversized_pending_receipt_is_read_with_a_hard_bound(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    pending_path = (
        vault / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"
    )
    original = pending_path.read_bytes()
    pending_path.write_bytes(original + b" " * (MAX_MANIFEST_BYTES + 1 - len(original)))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="corrupt pending"):
        await restarted.recover_operations()

    assert not (vault / "notes" / "new.md").exists()


async def test_oversized_committed_receipt_is_read_with_a_hard_bound(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _apply(writer, bundle)
    result_path = (
        vault / ".datacron" / "oplog" / "batches" / "committed" / f"{bundle.manifest_sha256}.json"
    )
    original = result_path.read_bytes()
    result_path.write_bytes(original + b" " * (MAX_MANIFEST_BYTES + 1 - len(original)))

    with pytest.raises(RecoveryRequiredError, match="corrupt organization batch receipt"):
        await writer.get_organization_batch_result(bundle.manifest_sha256)


async def test_runtime_failure_rolls_back_exact_bytes_synchronously(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "notes" / "existing.md"
    target.parent.mkdir()
    before = _note(_FIRST_ID, "Before", bom=True, eol="\r\n")
    after = _note(_FIRST_ID, "After")
    target.write_bytes(before)
    payload = _payload(vault, 0, after)
    identity = NoteIdentity(id=_FIRST_ID)
    operation = ReplaceExactOperation(
        kind="replace_exact",
        target="notes/existing.md",
        expected_sha256=sha256_bytes(before),
        expected=identity,
        payload_sha256=payload.sha256,
        result=identity,
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation=operation,
                source_path=None,
                target_path=target,
                payload=payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def fail_after_write(point: str) -> None:
        if point == "after_member_write":
            raise RuntimeError("ordinary write failure")

    with pytest.raises(RuntimeError, match="ordinary write failure"):
        await _apply(writer, bundle, fault_injector=fail_after_write)

    assert target.read_bytes() == before
    batches = vault / ".datacron" / "oplog" / "batches"
    assert not list((batches / "pending").glob("*.json"))
    assert not list((batches / "stage").glob(bundle.manifest_sha256))
    assert await writer.get_organization_batch_result(bundle.manifest_sha256) is None


async def test_restart_rolls_partial_move_forward_and_deletes_source_last(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "notes" / "old" / "note.md"
    source.parent.mkdir(parents=True)
    before = _note(_FIRST_ID, "Before")
    after = _note(_FIRST_ID, "After", bom=True, eol="\r\n")
    source.write_bytes(before)
    payload = _payload(vault, 0, after)
    identity = NoteIdentity(id=_FIRST_ID)
    operation = MoveReplaceExactOperation(
        kind="move_replace_exact",
        source="notes/old/note.md",
        target="notes/new/note.md",
        expected_sha256=sha256_bytes(before),
        expected=identity,
        payload_sha256=payload.sha256,
        result=identity,
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation=operation,
                source_path=source,
                target_path=vault / "notes" / "new" / "note.md",
                payload=payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    source_seen_after_target: list[bool] = []

    def crash_after_target(point: str) -> None:
        if point == "after_member_write":
            source_seen_after_target.append(source.is_file())
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_target)

    assert source_seen_after_target == [True]
    assert source.read_bytes() == before
    assert (vault / "notes" / "new" / "note.md").read_bytes() == after

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    recovered = await restarted.recover_operations()

    assert recovered == 1
    assert not source.exists()
    assert (vault / "notes" / "new" / "note.md").read_bytes() == after
    result = await restarted.get_organization_batch_result(bundle.manifest_sha256)
    assert result is not None
    assert result.already_committed is True
    records = await restarted.list_operations()
    assert len(records) == 1
    assert records[0].parameters["source_rel_path"] == "notes/old/note.md"


async def test_committed_marker_recovery_rolls_before_state_forward_before_cleanup(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_commit_marker(point: str) -> None:
        if point == "after_commit_marker":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_commit_marker)

    source = vault / "notes" / "old.md"
    target = vault / "notes" / "new.md"
    source.write_bytes(_note(_FIRST_ID, "Before"))
    target.unlink()
    sidecar = vault / ".datacron" / "ulids.json"
    sidecar.write_bytes(_sidecar_bytes({"notes/old.md": _FIRST_ID}))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 1
    assert not source.exists()
    assert target.read_bytes() == _note(_FIRST_ID, "After")
    assert sidecar.read_bytes() == _sidecar_bytes({"notes/new.md": _FIRST_ID})
    assert not _pending_path(vault, bundle).exists()


async def test_resolve_batch_result_recovers_commit_marker_on_same_writer(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_commit_marker(point: str) -> None:
        if point == "after_commit_marker":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_commit_marker)

    pending_path = _pending_path(vault, bundle)
    stage_dir = pending_path.parents[1] / "stage" / bundle.manifest_sha256
    assert pending_path.is_file()
    assert stage_dir.is_dir()
    read_only = await writer.get_organization_batch_result(bundle.manifest_sha256)
    assert read_only is not None
    assert pending_path.is_file()
    assert stage_dir.is_dir()

    resolved = await writer.resolve_organization_batch_result(bundle.manifest_sha256)

    assert resolved is not None
    assert resolved.already_committed is True
    assert not pending_path.exists()
    assert not stage_dir.exists()
    assert (vault / "notes" / "new.md").read_bytes() == _note(_FIRST_ID, "After")


async def test_resolve_batch_result_never_masks_an_unrelated_blocked_pending(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    committed_bundle = _create_bundle(vault, _note(_FIRST_ID, "Committed"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    committed = await _apply(writer, committed_bundle)
    blocked_payload = _payload(vault, 1, _note(_SECOND_ID, "Blocked"))
    blocked_operation = CreateExactOperation(
        kind="create_exact",
        target="notes/blocked.md",
        payload_sha256=blocked_payload.sha256,
        result=NoteIdentity(id=_SECOND_ID),
    )
    blocked_bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation=blocked_operation,
                source_path=None,
                target_path=vault / blocked_operation.target,
                payload=blocked_payload,
            ),
        ),
    )
    await _leave_pending(writer, blocked_bundle)
    blocked_target = vault / "notes" / "blocked.md"
    blocked_target.write_bytes(b"external-third-state\n")

    with pytest.raises(RecoveryRequiredError, match="blocked operation"):
        await writer.resolve_organization_batch_result(committed.manifest_sha256)

    assert (vault / "notes" / "new.md").read_bytes() == _note(_FIRST_ID, "Committed")
    assert blocked_target.read_bytes() == b"external-third-state\n"
    assert _pending_path(vault, blocked_bundle).is_file()
    assert (
        _pending_path(vault, blocked_bundle).parents[1] / "stage" / blocked_bundle.manifest_sha256
    ).is_dir()


async def test_restart_with_third_hash_blocks_every_writer_without_mutation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    target = vault / "notes" / "new.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"external-third-state\n")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert len(restarted.recovery_blocked) == 1
    blocked = restarted.recovery_blocked[0]
    assert blocked.reason == "pending_batch_disk_hash_mismatch"
    assert blocked.restore_before_available is False
    assert blocked.adopt_disk_available is False
    with pytest.raises(FileNotFoundError, match="pending operation not found"):
        await restarted.repair_recovery(
            blocked.operation_id,
            "restore-before",
            expected_disk_hash=sha256_bytes(b"external-third-state\n"),
            actor="batch-test",
        )
    with pytest.raises(RecoveryRequiredError, match="organization"):
        await restarted.write_note_atomic("other.md", "blocked\n", overwrite=False)

    assert target.read_bytes() == b"external-third-state\n"
    assert not (vault / "other.md").exists()


async def test_crash_after_first_record_recovery_does_not_duplicate_records(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = _note(_FIRST_ID, "First")
    second = _note(_SECOND_ID, "Second")
    first_payload = _payload(vault, 0, first)
    second_payload = _payload(vault, 1, second)
    first_operation = CreateExactOperation(
        kind="create_exact",
        target="notes/first.md",
        payload_sha256=first_payload.sha256,
        result=NoteIdentity(id=_FIRST_ID),
    )
    second_operation = CreateExactOperation(
        kind="create_exact",
        target="notes/second.md",
        payload_sha256=second_payload.sha256,
        result=NoteIdentity(id=_SECOND_ID),
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                first_operation,
                None,
                vault / "notes" / "first.md",
                first_payload,
            ),
            ResolvedOrganizationOperation(
                second_operation,
                None,
                vault / "notes" / "second.md",
                second_payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    record_count = 0

    def crash_after_first_record(point: str) -> None:
        nonlocal record_count
        if point != "after_operation_record":
            return
        record_count += 1
        if record_count == 1:
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_first_record)

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    assert await restarted.recover_operations() == 1
    records = await restarted.list_operations()
    assert len(records) == 2
    assert len({record.operation_id for record in records}) == 2


async def test_committed_manifest_rejects_different_confirmation_evidence(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "Committed"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _apply(writer, bundle)

    with pytest.raises(BatchConflictError, match="different confirmation token"):
        await writer.apply_organization_manifest(
            bundle,
            confirmation_token="a" * 64,
            projected_report_sha256=_PROJECTED_REPORT_SHA256,
            precommit_validator=lambda: None,
            operation=_context(bundle),
        )

    with pytest.raises(BatchConflictError, match="different projected report"):
        await writer.apply_organization_manifest(
            bundle,
            confirmation_token=_CONFIRMATION_TOKEN,
            projected_report_sha256="b" * 64,
            precommit_validator=lambda: None,
            operation=_context(bundle),
        )


async def test_config_replace_uses_exact_cas_and_preserves_payload_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True)
    (vault / "notes").mkdir(parents=True)
    before = _ACTIVE_CONFIG.encode("utf-8")
    after = b"\xef\xbb\xbf" + _ACTIVE_CONFIG.replace("\n", "\r\n").encode("utf-8")
    config_path.write_bytes(before)
    payload = _payload(vault, 0, after, suffix=".yaml")
    config = VaultConfigReplaceExact(
        kind="replace_exact",
        target=".datacron/VAULT.yaml",
        expected_sha256=sha256_bytes(before),
        payload_sha256=payload.sha256,
    )
    bundle = _validated_bundle(
        vault,
        (),
        config=config,
        config_payload=payload,
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    result = await _apply(writer, bundle)

    assert config_path.read_bytes() == after
    assert len(result.members) == 1
    assert result.members[0].kind == "config_replace_exact"
    assert result.members[0].before_hash == sha256_bytes(before)
    assert result.members[0].after_hash == sha256_bytes(after)
    assert (vault / ".datacron" / "history" / sha256_bytes(before)).read_bytes() == before


async def test_replace_preserves_bounded_legacy_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = vault / "notes" / "legacy.md"
    source.parent.mkdir(parents=True)
    before = _note(_LEGACY_EXISTING_ID, "Legacy before")
    after = _note(_LEGACY_EXISTING_ID, "Legacy after")
    source.write_bytes(before)
    payload = _payload(vault, 0, after)
    identity = ExistingNoteIdentity(id=_LEGACY_EXISTING_ID)
    operation = ReplaceExactOperation(
        kind="replace_exact",
        target="notes/legacy.md",
        expected_sha256=sha256_bytes(before),
        expected=identity,
        payload_sha256=payload.sha256,
        result=identity,
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation,
                source,
                source,
                payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    result = await _apply(writer, bundle)

    assert source.read_bytes() == after
    assert result.members[0].note_id == _LEGACY_EXISTING_ID


@pytest.mark.parametrize("fault_point", BATCH_FAULT_POINTS)
async def test_each_batch_fault_boundary_converges_to_exact_before_or_after(
    tmp_path: Path,
    fault_point: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "notes" / "old" / "note.md"
    source.parent.mkdir(parents=True)
    before = _note(_FIRST_ID, "Before", bom=True, eol="\r\n")
    after = _note(_FIRST_ID, "After")
    source.write_bytes(before)
    payload = _payload(vault, 0, after)
    identity = NoteIdentity(id=_FIRST_ID)
    operation = MoveReplaceExactOperation(
        kind="move_replace_exact",
        source="notes/old/note.md",
        target="notes/new/note.md",
        expected_sha256=sha256_bytes(before),
        expected=identity,
        payload_sha256=payload.sha256,
        result=identity,
    )
    bundle = _validated_bundle(
        vault,
        (
            ResolvedOrganizationOperation(
                operation,
                source,
                vault / "notes" / "new" / "note.md",
                payload,
            ),
        ),
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"fault at {point}")

    with pytest.raises(RuntimeError, match=f"fault at {fault_point}"):
        await _apply(writer, bundle, fault_injector=inject)

    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await restarted.recover_operations()
    target = vault / "notes" / "new" / "note.md"
    before_state = source.is_file() and source.read_bytes() == before and not target.exists()
    after_state = not source.exists() and target.is_file() and target.read_bytes() == after

    assert before_state or after_state
    assert restarted.recovery_blocked == ()


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("target_rel_path", "../../outside.md"),
        ("stage_name", "../../outside.after"),
        ("operation_id", "forged-operation"),
        ("note_id", None),
    ],
)
async def test_corrupt_pending_member_blocks_recovery_without_path_escape(
    tmp_path: Path,
    field: str,
    forged_value: object,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    pending_path = (
        vault / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"
    )
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["members"][0][field] = forged_value
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="corrupt pending"):
        await restarted.recover_operations()

    assert not (tmp_path / "outside.md").exists()
    assert not (tmp_path / "outside.after").exists()


async def test_pending_parameters_cannot_inject_content_into_journal(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    pending_path = (
        vault / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"
    )
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["parameters"]["note_body"] = "must never reach the journal"
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="corrupt pending"):
        await restarted.recover_operations()

    assert await restarted.list_operations() == []


async def test_pending_receipt_cannot_exceed_manifest_operation_limit(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    pending_path = (
        vault / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"
    )
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["members"] = payload["members"] * 514
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="corrupt pending"):
        await restarted.recover_operations()


@pytest.mark.parametrize(
    ("forged_target", "write_root", "expected_reason"),
    [
        (
            "outside/new.md",
            None,
            "pending_batch_scope_violation",
        ),
        (
            "notes/forbidden/new.md",
            "notes/allowed",
            "pending_batch_scope_violation",
        ),
    ],
)
async def test_forged_pending_path_outside_live_policy_blocks_before_write(
    tmp_path: Path,
    forged_target: str,
    write_root: str | None,
    expected_reason: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    batches = vault / ".datacron" / "oplog" / "batches"
    pending_path = batches / "pending" / f"{bundle.manifest_sha256}.json"
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["members"][0]["target_rel_path"] = forged_target
    payload["members"][0]["created_parent_dirs"] = []
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    configured_root = vault / write_root if write_root is not None else vault
    configured_root.mkdir(parents=True, exist_ok=True)
    restarted = FilesystemVaultWriter(
        vault,
        Settings(write_paths=[configured_root]),
    )

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {expected_reason}
    assert not (vault / Path(forged_target)).exists()
    assert not (vault / "notes" / "new.md").exists()


async def test_oversized_staged_payload_blocks_before_write(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    batches = vault / ".datacron" / "oplog" / "batches"
    pending_path = batches / "pending" / f"{bundle.manifest_sha256}.json"
    stage_path = batches / "stage" / bundle.manifest_sha256 / "0000.after"
    oversized = b"x" * (MAX_PAYLOAD_BYTES + 1)
    oversized_hash = sha256_bytes(oversized)
    stage_path.write_bytes(oversized)
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["members"][0]["after_hash"] = oversized_hash
    payload["parameters"]["total_payload_bytes"] = len(oversized)
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


async def test_existing_operation_id_mismatch_blocks_before_roll_forward(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_record(point: str) -> None:
        if point == "after_operation_record":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_record)

    batches = vault / ".datacron" / "oplog" / "batches"
    pending_path = batches / "pending" / f"{bundle.manifest_sha256}.json"
    payload = json.loads(pending_path.read_text(encoding="ascii"))
    payload["actor"] = "forged-actor"
    pending_path.write_text(json.dumps(payload), encoding="ascii")
    target = vault / "notes" / "new.md"
    target.unlink()
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_operation_record_mismatch"
    }
    assert not target.exists()


async def test_committed_receipt_mismatch_blocks_before_roll_forward(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_receipt(point: str) -> None:
        if point == "after_commit_marker":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_receipt)

    batches = vault / ".datacron" / "oplog" / "batches"
    result_path = batches / "committed" / f"{bundle.manifest_sha256}.json"
    receipt = json.loads(result_path.read_text(encoding="ascii"))
    receipt["projected_report_sha256"] = "a" * 64
    result_path.write_text(json.dumps(receipt), encoding="ascii")
    target = vault / "notes" / "new.md"
    target.unlink()
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_receipt_mismatch"
    }
    assert not target.exists()


async def test_cross_pending_path_effects_block_all_batches_before_write(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    batches = vault / ".datacron" / "oplog" / "batches"
    first_pending = batches / "pending" / f"{bundle.manifest_sha256}.json"
    first_stage = batches / "stage" / bundle.manifest_sha256 / "0000.after"
    second_id = "a" * 64
    payload = json.loads(first_pending.read_text(encoding="ascii"))
    payload["batch_id"] = second_id
    payload["manifest_sha256"] = second_id
    payload["parameters"]["batch_id"] = second_id
    payload["parameters"]["manifest_sha256"] = second_id
    payload["members"][0]["operation_id"] = f"organization-{second_id}-0000"
    second_pending = batches / "pending" / f"{second_id}.json"
    second_pending.write_text(json.dumps(payload), encoding="ascii")
    second_stage = batches / "stage" / second_id / "0000.after"
    second_stage.parent.mkdir(parents=True)
    second_stage.write_bytes(first_stage.read_bytes())
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert (
        sum(item.reason == "pending_batch_cross_effect" for item in restarted.recovery_blocked) == 2
    )
    assert not (vault / "notes" / "new.md").exists()


async def test_disjoint_pending_scope_snapshots_block_before_any_mutation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    first_path = vault / "notes" / "first.md"
    second_path = vault / "notes" / "second.md"
    first_path.parent.mkdir(parents=True)
    first_before = _note(_FIRST_ID, "First before")
    second_before = _note(_SECOND_ID, "Second before")
    first_path.write_bytes(first_before)
    second_path.write_bytes(second_before)

    def replace_bundle(
        target: str,
        note_id: str,
        before: bytes,
        after: bytes,
    ) -> ValidatedOrganizationBundle:
        payload = _payload(vault, 0, after)
        identity = ExistingNoteIdentity(id=note_id)
        operation = ReplaceExactOperation(
            kind="replace_exact",
            target=target,
            expected_sha256=sha256_bytes(before),
            expected=identity,
            payload_sha256=payload.sha256,
            result=identity,
        )
        path = vault / Path(target)
        return _validated_bundle(
            vault,
            (
                ResolvedOrganizationOperation(
                    operation=operation,
                    source_path=path,
                    target_path=path,
                    payload=payload,
                ),
            ),
        )

    first_bundle = replace_bundle(
        "notes/first.md",
        _FIRST_ID,
        first_before,
        _note(_FIRST_ID, "First after"),
    )
    second_bundle = replace_bundle(
        "notes/second.md",
        _SECOND_ID,
        second_before,
        _note(_SECOND_ID, "Second after"),
    )
    assert first_bundle.scope_note_preconditions == second_bundle.scope_note_preconditions
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    transaction = _transaction(writer)
    for bundle in (first_bundle, second_bundle):
        pending, payloads = transaction._prepare_pending(
            bundle,
            _CONFIRMATION_TOKEN,
            _PROJECTED_REPORT_SHA256,
            _context(bundle),
        )
        transaction._stage_payloads(pending, payloads, None)
        transaction._store_before_history(pending, None)
        transaction._write_pending(pending)
    config_before = (vault / ".datacron" / "VAULT.yaml").read_bytes()
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    inspected = await restarted.inspect_recovery()
    assert len(inspected) == 2
    assert {item.reason for item in inspected} == {"pending_batch_cross_effect"}
    assert first_path.read_bytes() == first_before
    assert second_path.read_bytes() == second_before
    assert (vault / ".datacron" / "VAULT.yaml").read_bytes() == config_before
    assert await restarted.recover_operations() == 0
    assert len(restarted.recovery_blocked) == 2
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_cross_effect"}
    assert first_path.read_bytes() == first_before
    assert second_path.read_bytes() == second_before
    assert (vault / ".datacron" / "VAULT.yaml").read_bytes() == config_before
    assert await restarted.list_operations() == []


async def test_duplicate_pending_json_key_is_not_accepted_last_wins(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    pending_path = (
        vault / ".datacron" / "oplog" / "batches" / "pending" / f"{bundle.manifest_sha256}.json"
    )
    original = pending_path.read_bytes()
    forged = original.replace(
        b'"actor":"batch-test"',
        b'"actor":"batch-test","actor":"forged-actor"',
        1,
    )
    assert forged != original
    pending_path.write_bytes(forged)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="corrupt pending"):
        await restarted.recover_operations()

    assert not (vault / "notes" / "new.md").exists()


def test_batch_json_parser_rejects_non_finite_numbers() -> None:
    parse_object = batch_transaction._json_object

    with pytest.raises(OperationLogError, match="non-finite JSON number"):
        parse_object(b'{"value":NaN}', "test object")


@pytest.mark.parametrize(
    "tampered_config",
    [
        b"history_mode: redacted\n" + _ACTIVE_CONFIG.encode("utf-8"),
        b"""organization:
  scope: notes
  scope: notes
  rules:
    - tag: memory/fact
      folder: notes
      naming: '{slug}.md'
""",
    ],
    ids=["non-organization-change", "duplicate-yaml-key"],
)
async def test_tampered_config_stage_is_rejected_against_exact_history_baseline(
    tmp_path: Path,
    tampered_config: bytes,
) -> None:
    vault = tmp_path / "vault"
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True)
    (vault / "notes").mkdir(parents=True)
    before = _ACTIVE_CONFIG.encode("utf-8")
    config_path.write_bytes(before)
    payload = _payload(vault, 0, before, suffix=".yaml")
    config = VaultConfigReplaceExact(
        kind="replace_exact",
        target=".datacron/VAULT.yaml",
        expected_sha256=sha256_bytes(before),
        payload_sha256=payload.sha256,
    )
    bundle = _validated_bundle(vault, (), config=config, config_payload=payload)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    batches = vault / ".datacron" / "oplog" / "batches"
    pending_path = batches / "pending" / f"{bundle.manifest_sha256}.json"
    stage_path = batches / "stage" / bundle.manifest_sha256 / "0000.after"
    stage_path.write_bytes(tampered_config)
    forged_hash = sha256_bytes(tampered_config)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    pending["members"][0]["after_hash"] = forged_hash
    pending["parameters"]["total_payload_bytes"] = len(tampered_config)
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "notes"]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("config_prefix", "forged_target"),
    [
        ("", "notes/.hidden/new.md"),
        ("excluded_folders:\n  - excluded\n", "notes/excluded/new.md"),
        ("excluded_files:\n  - blocked.md\n", "notes/blocked.md"),
    ],
    ids=["hidden-parent", "excluded-folder", "excluded-file"],
)
async def test_live_admission_policy_blocks_forged_pending_note_target(
    tmp_path: Path,
    config_prefix: str,
    forged_target: str,
) -> None:
    vault = tmp_path / "vault"
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        config_prefix + _ACTIVE_CONFIG,
        encoding="utf-8",
        newline="\n",
    )
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    pending["members"][0]["target_rel_path"] = forged_target
    pending["members"][0]["created_parent_dirs"] = []
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_scope_violation"}
    assert not (vault / Path(forged_target)).exists()


@pytest.mark.parametrize("forged_field", ["before_aliases", "source_before_hash"])
async def test_forged_source_identity_is_rejected_against_history(
    tmp_path: Path,
    forged_field: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _move_bundle_with_sidecar(vault, aliases=("Original",))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    pending_path = _pending_path(vault, bundle)
    pending = json.loads(pending_path.read_text(encoding="ascii"))
    move_member = pending["members"][0]
    if forged_field == "before_aliases":
        move_member["before_aliases"] = ["Forged"]
    else:
        forged_before = _note(_SECOND_ID, "Other", aliases=("Original",))
        forged_hash = writer._operation_journal.store_history(forged_before)
        (vault / "notes" / "old.md").write_bytes(forged_before)
        move_member["source_before_hash"] = forged_hash
        for item in pending["scope_note_preconditions"]:
            if item["rel_path"] == "notes/old.md":
                item["sha256"] = forged_hash
        pending["parameters"]["scope_note_preconditions_sha256"] = sha256_bytes(
            batch_transaction._json_bytes(pending["scope_note_preconditions"])
        )
    pending_path.write_text(json.dumps(pending), encoding="ascii")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert not (vault / "notes" / "new.md").exists()


@pytest.mark.parametrize("mutation", ["change", "delete", "add"])
async def test_scope_note_preconditions_block_nonmember_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    vault = tmp_path / "vault"
    unrelated = vault / "notes" / "unrelated.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(_note(_SECOND_ID, "Unrelated"))
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    if mutation == "change":
        unrelated.write_bytes(_note(_SECOND_ID, "Changed"))
    elif mutation == "delete":
        unrelated.unlink()
    else:
        (vault / "notes" / "added.md").write_bytes(_note("01HQXR7K9YZ8M2N3PQRSTV4WX7", "Added"))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_scope_precondition_mismatch"
    }
    assert not (vault / "notes" / "new.md").exists()


async def test_scope_directory_symlink_blocks_recovery_before_traversal(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    outside = tmp_path / "outside-scope"
    outside.mkdir()
    (outside / "collision.md").write_bytes(_note(_FIRST_ID, "Outside"))
    linked = vault / "notes" / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_scope_precondition_mismatch"
    }
    assert not (vault / "notes" / "new.md").exists()
    assert (outside / "collision.md").is_file()


async def test_simulated_scope_reparse_blocks_before_directory_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    reparse = vault / "notes" / "simulated-reparse"
    reparse.mkdir()
    original_guard = assert_path_chain_without_links

    def simulate_reparse(
        path: Path,
        *,
        anchor: Path | None = None,
        allow_missing: bool = False,
    ) -> Path:
        if Path(path) == reparse:
            raise PathConfinementError("simulated reparse point")
        return original_guard(path, anchor=anchor, allow_missing=allow_missing)

    monkeypatch.setattr(
        batch_transaction,
        "assert_path_chain_without_links",
        simulate_reparse,
    )
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_scope_precondition_mismatch"
    }
    assert not (vault / "notes" / "new.md").exists()


async def test_config_baseline_change_blocks_recovery_without_config_member(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.write_text(
        "excluded_files:\n  - ignored.md\n" + _ACTIVE_CONFIG,
        encoding="utf-8",
        newline="\n",
    )
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_baseline_mismatch"
    }
    assert not (vault / "notes" / "new.md").exists()


async def test_partial_config_recovery_cannot_change_historical_scope(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    config_path = vault / ".datacron" / "VAULT.yaml"
    config_path.parent.mkdir(parents=True)
    (vault / "notes").mkdir(parents=True)
    (vault / "notes2").mkdir(parents=True)
    before = _ACTIVE_CONFIG.encode()
    after = (
        _ACTIVE_CONFIG.replace("scope: notes", "scope: notes2")
        .replace(
            "folder: notes",
            "folder: notes2",
        )
        .encode()
    )
    config_path.write_bytes(before)
    payload = _payload(vault, 0, after, suffix=".yaml")
    config = VaultConfigReplaceExact(
        kind="replace_exact",
        target=".datacron/VAULT.yaml",
        expected_sha256=sha256_bytes(before),
        payload_sha256=payload.sha256,
    )
    bundle = _validated_bundle(
        vault,
        (),
        config=config,
        config_payload=payload,
    )
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    config_path.write_bytes(after)
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {"pending_batch_stage_invalid"}
    assert config_path.read_bytes() == after


@pytest.mark.parametrize(
    "rel_path",
    [".datacron/ulids.json", ".datacron/ulids.json.migrated"],
    ids=["primary", "migrated"],
)
async def test_identity_sidecar_baseline_change_blocks_recovery_without_member(
    tmp_path: Path,
    rel_path: str,
) -> None:
    vault = tmp_path / "vault"
    sidecar_path = vault / Path(rel_path)
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_bytes(_sidecar_bytes({"notes/original.md": _SECOND_ID}))
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    await _leave_pending(writer, bundle)
    sidecar_path.write_bytes(_sidecar_bytes({"notes/changed.md": _SECOND_ID}))
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    assert await restarted.recover_operations() == 0
    assert {item.reason for item in restarted.recovery_blocked} == {
        "pending_batch_baseline_mismatch"
    }
    assert not (vault / "notes" / "new.md").exists()


@pytest.mark.parametrize("linked_root", ["history", "oplog"])
async def test_linked_organization_journal_root_is_rejected_before_stage(
    tmp_path: Path,
    linked_root: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    outside = tmp_path / f"outside-{linked_root}"
    outside.mkdir()
    root = vault / ".datacron" / linked_root
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(PermissionError, match="Linked path component is forbidden"):
        await writer.validate_organization_manifest_capacity(
            bundle,
            confirmation_token=_CONFIRMATION_TOKEN,
            projected_report_sha256=_PROJECTED_REPORT_SHA256,
            operation=_context(bundle),
        )

    with pytest.raises(PermissionError, match="Linked path component is forbidden"):
        await _apply(writer, bundle)

    assert not list(outside.iterdir())
    assert not (vault / "notes" / "new.md").exists()
    assert not (vault / ".datacron" / "oplog" / "batches").exists()


async def test_linked_stage_payload_blocks_recovery_without_reading_link_target(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bundle = _create_bundle(vault, _note(_FIRST_ID, "After"))
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    def crash_after_pending(point: str) -> None:
        if point == "after_pending_write":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        await _apply(writer, bundle, fault_injector=crash_after_pending)

    outside = tmp_path / "outside.after"
    outside.write_bytes(b"outside must remain untouched")
    stage_path = (
        vault / ".datacron" / "oplog" / "batches" / "stage" / bundle.manifest_sha256 / "0000.after"
    )
    stage_path.unlink()
    try:
        stage_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    restarted = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(RecoveryRequiredError, match="unsafe organization batch sidecar"):
        await restarted.recover_operations()

    assert outside.read_bytes() == b"outside must remain untouched"


async def test_linked_global_lock_directory_is_rejected_before_open(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    sidecar = vault / ".datacron"
    outside = tmp_path / "outside-locks"
    sidecar.mkdir(parents=True)
    outside.mkdir()
    lock_dir = sidecar / "locks"
    try:
        lock_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    with pytest.raises(PermissionError, match="Linked path component is forbidden"):
        await writer.write_note_atomic(
            "notes/new.md",
            "blocked\n",
            overwrite=False,
        )

    assert not list(outside.iterdir())
    assert not (vault / "notes" / "new.md").exists()


def test_operation_journal_interrupted_append_preserves_last_complete_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    journal.append_record(
        _ordinary_record(
            "committed-operation",
            now,
            sha256_bytes(b"before-committed"),
            sha256_bytes(b"after-committed"),
        )
    )
    operations_path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    committed_bytes = operations_path.read_bytes()

    def fail_before_atomic_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated interruption before replace")

    monkeypatch.setattr(
        "datacron.core.durability._replace_with_windows_retry",
        fail_before_atomic_replace,
    )

    with pytest.raises(OperationLogError, match="failed to append"):
        journal.append_record(
            _ordinary_record(
                "interrupted-operation",
                now + timedelta(seconds=1),
                sha256_bytes(b"before-interrupted"),
                sha256_bytes(b"after-interrupted"),
            )
        )

    assert operations_path.read_bytes() == committed_bytes
    inspected = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    assert [record.operation_id for record in inspected.read_records()] == ["committed-operation"]
    assert not list(operations_path.parent.glob(f".{operations_path.name}.*.tmp"))


@pytest.mark.parametrize("read_mode", ["full", "tail"])
@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (b'"actor":"unit-test"', b'"actor":"unit-test","actor":"unit-test"'),
        (b'"new_content_chars":3', b'"new_content_chars":NaN'),
        (b'"new_content_chars":3', b'"new_content_chars":Infinity'),
        (b'"new_content_chars":3', b'"new_content_chars":-Infinity'),
    ],
    ids=["duplicate-key", "nan", "positive-infinity", "negative-infinity"],
)
def test_operation_journal_readers_reject_ambiguous_or_non_finite_json(
    tmp_path: Path,
    read_mode: str,
    needle: bytes,
    replacement: bytes,
) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    journal.append_record(
        _ordinary_record(
            "strict-json-operation",
            datetime(2026, 7, 10, tzinfo=UTC),
            sha256_bytes(b"before"),
            sha256_bytes(b"after"),
        )
    )
    path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    original = path.read_bytes()
    forged = original.replace(needle, replacement, 1)
    assert forged != original
    path.write_bytes(forged)

    if read_mode == "tail":
        inspected = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        error = "invalid operation log tail record"
        action: Callable[[], object] = inspected.next_timestamp
    else:
        error = "invalid JSONL at line 1"
        action = journal.read_records

    with pytest.raises(OperationLogError, match=error):
        action()


@pytest.mark.parametrize("read_mode", ["full", "tail"])
def test_operation_journal_readers_reject_fields_outside_exact_schema(
    tmp_path: Path,
    read_mode: str,
) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    journal.append_record(
        _ordinary_record(
            "extra-field-operation",
            datetime(2026, 7, 10, tzinfo=UTC),
            sha256_bytes(b"before"),
            sha256_bytes(b"after"),
        )
    )
    path = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["content"] = "must not be canonicalized away"
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="ascii",
    )

    if read_mode == "tail":
        inspected = OperationJournal(tmp_path, retention_days=30, history_mode="full")
        action: Callable[[], object] = inspected.next_timestamp
    else:
        action = journal.read_records

    with pytest.raises(OperationLogError, match="fields differ from schema"):
        action()


@pytest.mark.parametrize(
    ("linked_rel_path", "operation"),
    [
        (".datacron", "history"),
        (".datacron/history", "history"),
        (".datacron/oplog", "append"),
        (".datacron/oplog/pending", "pending"),
    ],
)
def test_operation_journal_linked_roots_fail_closed_without_outside_io(
    tmp_path: Path,
    linked_rel_path: str,
    operation: str,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}-{operation}"
    outside.mkdir()
    linked = tmp_path / linked_rel_path
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    record = _ordinary_record(
        "linked-root-operation",
        datetime(2026, 7, 10, tzinfo=UTC),
        sha256_bytes(b"before"),
        sha256_bytes(b"after"),
    )
    action: Callable[[], object]
    if operation == "history":
        action = partial(journal.store_history, b"must stay inside")
    elif operation == "pending":
        action = partial(journal.write_pending, record)
    else:
        action = partial(journal.append_record, record)

    with pytest.raises(OperationLogError, match="linked or reparse"):
        action()

    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "target_kind",
    [
        "history-read",
        "history-write",
        "pending-read",
        "pending-write",
        "operations-read",
        "operations-append",
    ],
)
def test_operation_journal_linked_file_targets_fail_closed(
    tmp_path: Path,
    target_kind: str,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}-{target_kind}.bin"
    sentinel = b"outside bytes must remain exact\n"
    outside.write_bytes(sentinel)
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    content = b"history bytes"
    record = _ordinary_record(
        "linked-write",
        datetime(2026, 7, 10, tzinfo=UTC),
        sha256_bytes(b"before"),
        sha256_bytes(b"after"),
    )
    if target_kind == "history-read":
        linked = tmp_path / ".datacron" / "history" / sha256_bytes(sentinel)
    elif target_kind == "history-write":
        linked = tmp_path / ".datacron" / "history" / sha256_bytes(content)
    elif target_kind.startswith("pending"):
        pending_name = "linked-write.json" if target_kind == "pending-write" else "linked.json"
        linked = tmp_path / ".datacron" / "oplog" / "pending" / pending_name
    else:
        linked = tmp_path / ".datacron" / "oplog" / "operations.jsonl"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable: {exc}")
    action: Callable[[], object]
    if target_kind == "history-read":
        action = partial(journal.read_history, sha256_bytes(sentinel))
    elif target_kind == "history-write":
        action = partial(journal.store_history, content)
    elif target_kind == "pending-read":
        action = partial(journal.read_pending, linked)
    elif target_kind == "pending-write":
        action = partial(journal.write_pending, record)
    elif target_kind == "operations-read":
        action = journal.read_records
    else:
        action = partial(journal.append_record, record)

    with pytest.raises(OperationLogError, match="linked or reparse"):
        action()

    assert linked.is_symlink()
    assert outside.read_bytes() == sentinel


@pytest.mark.parametrize("renamed_name", ["renamed.json", "renamed.txt"])
async def test_operation_recovery_rejects_pending_filename_id_mismatch(
    tmp_path: Path,
    renamed_name: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"before\n")
    journal = OperationJournal(vault, retention_days=30, history_mode="full")
    record = _ordinary_record(
        "original",
        datetime(2026, 7, 10, tzinfo=UTC),
        sha256_bytes(b"before\n"),
        sha256_bytes(b"after\n"),
    )
    journal.write_pending(record)
    renamed = journal.pending_path("original").with_name(renamed_name)
    journal.pending_path("original").replace(renamed)
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))

    expected_error = (
        "filename does not match operation_id" if renamed.suffix == ".json" else "unexpected entry"
    )
    with pytest.raises(OperationLogError, match=expected_error):
        await writer.recover_operations()
    with pytest.raises(OperationLogError, match=expected_error):
        await writer.write_note_atomic("other.md", "# Other\n", overwrite=False)

    assert target.read_bytes() == b"before\n"
    assert not (vault / "other.md").exists()
    assert renamed.is_file()


async def test_operation_recovery_rejects_pending_receipt_changed_between_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    target.write_bytes(b"before\n")
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault]))
    journal = cast("OperationJournal", cast("Any", writer)._operation_journal)
    original = _ordinary_record(
        "changing",
        datetime(2026, 7, 10, tzinfo=UTC),
        sha256_bytes(b"before\n"),
        sha256_bytes(b"after\n"),
    )
    changed = replace(original, rel_path="other.md")
    journal.write_pending(original)
    original_read = OperationJournal.read_pending_snapshot
    reads = 0

    def swap_after_initial_read(
        current: OperationJournal,
        path: Path,
    ) -> tuple[OperationRecord, bytes]:
        nonlocal reads
        result = original_read(current, path)
        if current is journal:
            reads += 1
            if reads == 2:
                journal.write_pending(changed)
        return result

    monkeypatch.setattr(OperationJournal, "read_pending_snapshot", swap_after_initial_read)

    with pytest.raises(OperationLogError, match="changed during recovery"):
        await writer.recover_operations()

    assert target.read_bytes() == b"before\n"
    assert not (vault / "other.md").exists()


def test_operation_pending_receipt_read_is_bounded(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    oversized = tmp_path / ".datacron" / "oplog" / "pending" / "oversized.json"
    oversized.parent.mkdir(parents=True)
    oversized.write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(OperationLogError, match="exceeds 65536 bytes"):
        journal.read_pending(oversized)


@pytest.mark.parametrize("target_kind", ["pending", "history"])
def test_operation_recovery_cannot_unlink_linked_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}-{target_kind}.bin"
    sentinel = b"outside bytes must remain exact\n"
    outside.write_bytes(sentinel)
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    if target_kind == "pending":
        linked = journal.pending_path("linked-pending")
        action: Callable[[], object] = partial(journal.remove_pending, "linked-pending")
    else:
        linked = tmp_path / ".datacron" / "history" / ("a" * 64)
        action = journal.purge_history
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable: {exc}")

    with pytest.raises(OperationLogError, match="linked or reparse"):
        action()

    assert linked.is_symlink()
    assert outside.read_bytes() == sentinel


def test_operation_journal_windows_reparse_attribute_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / ".datacron"
    sidecar.mkdir()
    journal = OperationJournal(tmp_path, retention_days=30, history_mode="full")
    original_lstat = os.lstat

    def reparse_lstat(path: Path | str) -> os.stat_result:
        result = original_lstat(path)
        if Path(path) == sidecar:
            return cast(
                "os.stat_result",
                SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x0400),
            )
        return result

    monkeypatch.setattr(os, "lstat", reparse_lstat)

    with pytest.raises(OperationLogError, match="linked or reparse"):
        journal.store_history(b"blocked")

    assert not (sidecar / "history").exists()
