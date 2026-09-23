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
"""A batch refusal the manifest validator did not foresee must not wedge the vault.

The classifier's stage checks ran only after the pending receipt was published,
and the rollback refused on the same check. A refusal there left the vault bytes
unchanged while every later write was refused and startup recovery stayed
blocked, with no online way out. Two inputs reached it: a note over 2 MB outside
the organization scope, which the validator admits up to 64 MB, and a stale
sidecar entry for a note the manifest does not touch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from datacron.core.config import Settings
from datacron.core.operation_log import OperationContext
from datacron.core.scope import SingleTenantVaultScope
from datacron.core.vault_writer import FilesystemVaultWriter
from datacron.organization.manifest import (
    MAX_PAYLOAD_BYTES,
    ValidatedOrganizationBundle,
    load_organization_bundle,
    sha256_bytes,
    validate_organization_bundle,
)

_RENAMED_ID: Final[str] = "01J00000000000000000000131"
_OTHER_ID: Final[str] = "01J00000000000000000000132"
_CONFIG: Final[bytes] = (
    b"organization:\n  scope: memory\n  rules:\n    - tag: memory/fact\n"
    b"      folder: memory\n      naming: '{slug}'\n"
)
_TOKEN: Final[str] = "c" * 64
_REPORT: Final[str] = "d" * 64


def _note(note_id: str, title: str, body: str) -> bytes:
    return (
        f"---\nid: {note_id}\ntitle: {title}\naliases: []\ntags:\n  - memory/fact\n---\n"
        f"# {title}\n\n{body}\n"
    ).encode()


def _vault(tmp_path: Path) -> tuple[Path, bytes]:
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / ".datacron").mkdir()
    (vault / ".datacron" / "VAULT.yaml").write_bytes(_CONFIG)
    (vault / "memory" / "renamed.md").write_bytes(_note(_RENAMED_ID, "Renamed", "r"))
    other_before = _note(_OTHER_ID, "Other", "before")
    (vault / "memory" / "other.md").write_bytes(other_before)
    return vault, other_before


def _validated(tmp_path: Path, vault: Path, other_before: bytes) -> ValidatedOrganizationBundle:
    other_after = _note(_OTHER_ID, "Other", "after")
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "payloads").mkdir(parents=True)
    (bundle_dir / "payloads" / f"{sha256_bytes(other_after)}.md").write_bytes(other_after)
    manifest = {
        "schema": "organization-apply-v1",
        "operations": [
            {
                "kind": "replace_exact",
                "target": "memory/other.md",
                "expected_sha256": sha256_bytes(other_before),
                "expected": {"id": _OTHER_ID, "aliases": []},
                "payload_sha256": sha256_bytes(other_after),
                "result": {"id": _OTHER_ID, "aliases": []},
            }
        ],
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    settings = Settings(vault_root=vault, read_paths=[vault], write_paths=[vault / "memory"])
    bundle = load_organization_bundle(bundle_dir / "manifest.json", vault_root=vault)
    return validate_organization_bundle(
        bundle, vault_root=vault, scope=SingleTenantVaultScope(vault, settings)
    )


async def _apply(vault: Path, validated: ValidatedOrganizationBundle) -> None:
    writer = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "memory"]))
    await writer.apply_organization_manifest(
        validated,
        confirmation_token=_TOKEN,
        projected_report_sha256=_REPORT,
        precommit_validator=lambda: None,
        operation=OperationContext(
            op="apply_organization_manifest",
            tool="apply_organization_manifest",
            actor="test",
            parameters={},
        ),
    )


def _pending_receipts(vault: Path) -> list[Path]:
    return list((vault / ".datacron" / "oplog" / "batches" / "pending").glob("*.json"))


async def test_a_note_over_the_payload_limit_outside_the_scope_does_not_block_an_apply(
    tmp_path: Path,
) -> None:
    vault, other_before = _vault(tmp_path)
    (vault / "journal").mkdir()
    big = b"---\ntitle: Big log\n---\n# Big log\n\n" + b"pasted log output\n" * (
        MAX_PAYLOAD_BYTES // 16
    )
    assert len(big) > MAX_PAYLOAD_BYTES
    (vault / "journal" / "big-log.md").write_bytes(big)

    await _apply(vault, _validated(tmp_path, vault, other_before))

    assert (vault / "memory" / "other.md").read_bytes() == _note(_OTHER_ID, "Other", "after")
    assert _pending_receipts(vault) == []


async def test_a_refusal_after_validation_publishes_nothing_and_leaves_writes_open(
    tmp_path: Path,
) -> None:
    vault, other_before = _vault(tmp_path)
    (vault / ".datacron" / "ulids.json").write_text(
        json.dumps({"memory/original.md": _RENAMED_ID}, indent=2) + "\n", encoding="utf-8"
    )
    validated = _validated(tmp_path, vault, other_before)

    with pytest.raises(Exception, match="refused before publication"):
        await _apply(vault, validated)

    assert _pending_receipts(vault) == []
    assert (vault / "memory" / "other.md").read_bytes() == other_before
    fresh = FilesystemVaultWriter(vault, Settings(write_paths=[vault / "memory"]))
    await fresh.recover_operations()
    assert fresh.recovery_blocked == ()
    await fresh.write_note_atomic("memory/after.md", "---\ntitle: x\n---\nhello\n", overwrite=False)
    assert (vault / "memory" / "after.md").is_file()
