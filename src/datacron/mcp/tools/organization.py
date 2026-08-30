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
"""Two-phase MCP boundary for exact organization-manifest application."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias, cast

from datacron.core.batch_transaction import BatchApplyResult, BatchConflictError
from datacron.core.config import VaultConfig
from datacron.core.durability import (
    DurabilityUnavailableError,
    ReadOnlyModeError,
    RecoveryRequiredError,
)
from datacron.core.operation_log import OperationContext
from datacron.core.paths import PathConfinementError, sidecar_vault_config
from datacron.core.scope import (
    LinkedPathError,
    OrganizationBatchWriter,
    SingleTenantVaultScope,
    assert_path_chain_without_links,
)
from datacron.core.vault_writer import VaultLockBusyError
from datacron.indexing.reconcile import ReconcileStats, reconcile
from datacron.mcp.tools.payloads import _audit, _error_response, _internal_error_response
from datacron.organization.manifest import (
    MAX_PAYLOAD_BYTES,
    OrganizationBundle,
    OrganizationManifestError,
    ValidatedOrganizationBundle,
    load_organization_bundle,
    parse_organization_config_document,
    validate_organization_bundle,
)
from datacron.organization.planner import (
    OrganizationConfigurationError,
    hash_organization_plan,
    plan_organization,
    plan_organization_snapshot,
)

if TYPE_CHECKING:
    from datacron.mcp.server import DatacronApp

OrganizationManifestMode: TypeAlias = Literal["validate", "apply"]
OrganizationCommittedStatus: TypeAlias = Literal[
    "committed_index_incomplete",
    "committed_report_mismatch",
]

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_COMPONENT_COUNT: Final[int] = 5


class OrganizationConfirmationError(ValueError):
    """Raised when an apply request is not bound to its exact preview."""

    code: Final[str] = "organization_confirmation_mismatch"


class OrganizationProjectionError(ValueError):
    """Raised when a safe deterministic planner projection cannot be produced."""

    code: Final[str] = "organization_projection_failed"


class OrganizationHistoryError(ValueError):
    """Raised before preview when exact rollback history is unavailable."""

    code: Final[str] = "organization_full_history_required"


class OrganizationScopeError(ValueError):
    """Raised when recovery cannot serialize an injected scope restriction."""

    code: Final[str] = "organization_scope_unsupported"


class OrganizationFinalReportError(RuntimeError):
    """Raised after commit when the live planner report differs from the preview."""

    code: Final[str] = "organization_final_report_mismatch"


@dataclass(frozen=True, slots=True)
class _OrganizationPreview:
    """Content-free evidence binding one bundle to one exact live pre-state."""

    validated: ValidatedOrganizationBundle
    config_before_sha256: str
    projected_report_sha256: str
    confirmation_token: str


def _require_sha256(value: str, field: str) -> str:
    """Return one strict lowercase SHA-256 or raise a stable validation error."""
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase 64-character SHA-256 digest")
    return value


def _validate_request(
    manifest_path: str,
    expected_manifest_sha256: str,
    mode: str,
    confirmation_token: str | None,
) -> tuple[Path, str, OrganizationManifestMode, str | None]:
    """Validate scalar MCP arguments before opening external bundle files."""
    path = Path(manifest_path).expanduser()
    if not path.is_absolute():
        raise ValueError("manifest_path must be absolute")
    expected = _require_sha256(expected_manifest_sha256, "expected_manifest_sha256")
    if mode not in {"validate", "apply"}:
        raise ValueError("mode must be 'validate' or 'apply'")
    cleaned_mode = cast("OrganizationManifestMode", mode)
    if cleaned_mode == "validate":
        if confirmation_token is not None:
            raise ValueError("confirmation_token must be null in validate mode")
        return path, expected, cleaned_mode, None
    if confirmation_token is None:
        raise OrganizationConfirmationError("confirmation_token is required in apply mode")
    token = _require_sha256(confirmation_token, "confirmation_token")
    return path, expected, cleaned_mode, token


def _load_expected_bundle(
    manifest_path: Path,
    expected_manifest_sha256: str,
    app: DatacronApp,
) -> OrganizationBundle:
    """Authenticate a bundle and bind the call to the caller-provided raw hash."""
    bundle = load_organization_bundle(manifest_path, vault_root=app.vault_root)
    if not hmac.compare_digest(bundle.manifest_sha256, expected_manifest_sha256):
        raise OrganizationManifestError(
            "manifest_hash_mismatch",
            "organization manifest bytes do not match expected_manifest_sha256",
        )
    return bundle


def _read_config_bytes(vault_root: Path) -> bytes:
    """Read exact current VAULT.yaml bytes without following linked paths."""
    config_path = sidecar_vault_config(vault_root)
    try:
        config_path.lstat()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise OrganizationProjectionError("VAULT.yaml cannot be inspected safely") from exc
    try:
        safe_path = assert_path_chain_without_links(config_path)
        if not safe_path.is_file():
            raise OrganizationProjectionError("VAULT.yaml is not a regular file")
        with safe_path.open("rb") as handle:
            raw_bytes = handle.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw_bytes) > MAX_PAYLOAD_BYTES:
            raise OrganizationProjectionError(
                f"VAULT.yaml exceeds the {MAX_PAYLOAD_BYTES}-byte validation limit"
            )
        return raw_bytes
    except OrganizationProjectionError:
        raise
    except (LinkedPathError, OSError) as exc:
        raise OrganizationProjectionError(
            "VAULT.yaml cannot be read without following a linked path"
        ) from exc


def _confirmation_token(
    *,
    manifest_sha256: str,
    payload_set_sha256: str,
    scope_digest: str,
    config_before_sha256: str,
    projected_report_sha256: str,
) -> str:
    """Hash the five fixed-width preview digests in their public contract order."""
    components = (
        manifest_sha256,
        payload_set_sha256,
        scope_digest,
        config_before_sha256,
        projected_report_sha256,
    )
    if len(components) != _TOKEN_COMPONENT_COUNT:
        raise AssertionError("organization confirmation token component count changed")
    for component in components:
        _require_sha256(component, "confirmation token component")
    return hashlib.sha256("".join(components).encode("ascii")).hexdigest()


def _project_report_hash(
    app: DatacronApp,
    validated: ValidatedOrganizationBundle,
) -> str:
    """Build and hash an in-memory, content-free post-batch projection."""
    try:
        projected_plan = plan_organization_snapshot(
            app.vault_root,
            validated.target_config,
            validated.projected_notes,
        )
    except (OrganizationConfigurationError, PathConfinementError) as exc:
        raise OrganizationProjectionError(
            "projected organization report cannot be computed"
        ) from exc
    return hash_organization_plan(projected_plan)


def _build_preview(app: DatacronApp, bundle: OrganizationBundle) -> _OrganizationPreview:
    """Bind one authenticated bundle to live state and compute its isolated preview."""
    validated = validate_organization_bundle(
        bundle,
        vault_root=app.vault_root,
        scope=app.scope,
    )
    if validated.target_config.history_mode != "full":
        raise OrganizationHistoryError(
            "organization batches require history_mode=full before validation"
        )
    config_before_sha256 = validated.config_before_sha256
    projected_report_sha256 = _project_report_hash(app, validated)
    token = _confirmation_token(
        manifest_sha256=validated.manifest_sha256,
        payload_set_sha256=validated.payload_set_sha256,
        scope_digest=validated.scope_digest,
        config_before_sha256=config_before_sha256,
        projected_report_sha256=projected_report_sha256,
    )
    return _OrganizationPreview(
        validated=validated,
        config_before_sha256=config_before_sha256,
        projected_report_sha256=projected_report_sha256,
        confirmation_token=token,
    )


def _assert_preview_token(preview: _OrganizationPreview, token: str) -> None:
    """Fail closed unless a preview exactly matches the caller confirmation."""
    if not hmac.compare_digest(preview.confirmation_token, token):
        raise OrganizationConfirmationError(
            "confirmation_token does not match the current manifest, payloads, scope, "
            "configuration, and projected report"
        )


def _current_report_hash(app: DatacronApp) -> str:
    """Hash the planner report for the live post-transaction vault."""
    try:
        raw_bytes = _read_config_bytes(app.vault_root)
        if raw_bytes:
            text = raw_bytes.decode("utf-8")
            _document, live_config = parse_organization_config_document(
                text,
                label="live VAULT.yaml",
            )
        else:
            live_config = VaultConfig()
        report = plan_organization(app.vault_root, live_config, settings=app.settings)
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        OrganizationConfigurationError,
        OrganizationManifestError,
    ) as exc:
        raise OrganizationFinalReportError(
            "the final organization report cannot be computed"
        ) from exc
    return hash_organization_plan(report)


async def _reconcile_batch_locked(
    app: DatacronApp,
    *,
    removed_identity_ids: tuple[str, ...],
) -> ReconcileStats:
    """Perform one full reconcile while the caller holds ``reconcile_lock``."""
    await app.vault_reader.invalidate_alias_cache()
    for note_id in removed_identity_ids:
        await app.store.delete_note(note_id)
    stats = await reconcile(
        app.store,
        app.vault_reader,
        app.chunker,
        mtime_gate=False,
    )
    app.repair_state.last_sweep_completed_at = time.monotonic()
    return stats


def _organization_operation_count(validated: ValidatedOrganizationBundle) -> int:
    return len(validated.operations) + int(validated.config_payload is not None)


def _identity_sidecar_replaced(validated: ValidatedOrganizationBundle) -> bool:
    return validated.identity_sidecar_after_bytes is not None


def _organization_operation_context(
    preview: _OrganizationPreview,
    *,
    actor: str,
) -> OperationContext:
    validated = preview.validated
    derived_payload_bytes = len(validated.identity_sidecar_after_bytes or b"")
    return OperationContext(
        op="apply_organization_manifest",
        tool="apply_organization_manifest",
        actor=actor,
        parameters={
            "manifest_sha256": validated.manifest_sha256,
            "payload_set_sha256": validated.payload_set_sha256,
            "scope_digest": validated.scope_digest,
            "projected_report_sha256": preview.projected_report_sha256,
            "operation_count": _organization_operation_count(validated),
            "total_payload_bytes": validated.total_payload_bytes + derived_payload_bytes,
            "config_replaced": validated.config_payload is not None,
            "identity_sidecar_replaced": _identity_sidecar_replaced(validated),
            "identity_sidecar_case_canonicalization_count": (
                validated.identity_sidecar_case_canonicalization_count
            ),
            "identity_sidecar_case_canonicalization_sha256": (
                validated.identity_sidecar_case_canonicalization_sha256
            ),
        },
    )


def _validation_payload(preview: _OrganizationPreview) -> dict[str, Any]:
    """Return a bounded content-free validate receipt."""
    validated = preview.validated
    return {
        "schema_version": 1,
        "mode": "validate",
        "status": "validated",
        "manifest_sha256": validated.manifest_sha256,
        "payload_set_sha256": validated.payload_set_sha256,
        "scope_digest": validated.scope_digest,
        "config_before_sha256": preview.config_before_sha256,
        "projected_report_sha256": preview.projected_report_sha256,
        "final_report_sha256": None,
        "operation_count": _organization_operation_count(validated),
        "derived_operation_count": int(_identity_sidecar_replaced(validated)),
        "identity_sidecar_replaced": _identity_sidecar_replaced(validated),
        "identity_sidecar_case_canonicalization_count": (
            validated.identity_sidecar_case_canonicalization_count
        ),
        "identity_sidecar_case_canonicalization_sha256": (
            validated.identity_sidecar_case_canonicalization_sha256
        ),
        "total_payload_bytes": validated.total_payload_bytes,
        "confirmation_token": preview.confirmation_token,
        "batch_id": None,
        "applied_operations": None,
        "already_committed": None,
        "indexed": None,
        "committed_error_code": None,
        "committed_error_message": None,
    }


def _apply_payload(
    bundle: OrganizationBundle,
    result: BatchApplyResult,
    final_report_sha256: str | None,
    preview: _OrganizationPreview | None,
    *,
    status: Literal["applied"] | OrganizationCommittedStatus = "applied",
    indexed: bool = True,
    committed_error_code: str | None = None,
    committed_error_message: str | None = None,
) -> dict[str, Any]:
    """Return a bounded content-free apply receipt, including replay state."""
    derived_operation_count = sum(
        member.kind == "identity_sidecar_replace_exact" for member in result.members
    )
    return {
        "schema_version": 1,
        "mode": "apply",
        "status": status,
        "manifest_sha256": result.manifest_sha256,
        "payload_set_sha256": result.payload_set_sha256,
        "scope_digest": result.scope_digest,
        "config_before_sha256": result.config_before_sha256,
        "projected_report_sha256": result.projected_report_sha256,
        "final_report_sha256": final_report_sha256,
        "operation_count": len(bundle.manifest.operations)
        + int(bundle.manifest.config is not None),
        "derived_operation_count": derived_operation_count,
        "identity_sidecar_replaced": derived_operation_count == 1,
        "identity_sidecar_case_canonicalization_count": (
            result.identity_sidecar_case_canonicalization_count
        ),
        "identity_sidecar_case_canonicalization_sha256": (
            result.identity_sidecar_case_canonicalization_sha256
        ),
        "total_payload_bytes": bundle.total_payload_bytes,
        "confirmation_token": result.confirmation_token,
        "batch_id": result.batch_id,
        "applied_operations": len(result.members),
        "already_committed": result.already_committed,
        "indexed": indexed,
        "committed_error_code": committed_error_code,
        "committed_error_message": committed_error_message,
    }


async def _finalize_committed_batch(
    app: DatacronApp,
    bundle: OrganizationBundle,
    result: BatchApplyResult,
    preview: _OrganizationPreview | None,
    *,
    started: float,
    mode: OrganizationManifestMode,
) -> tuple[dict[str, Any] | None, ReconcileStats | None, str | None]:
    try:
        batch_writer = cast("OrganizationBatchWriter", app.vault_writer)
        removed_identity_ids = await batch_writer.get_organization_removed_identity_ids(result)
        index_stats = await _reconcile_batch_locked(
            app,
            removed_identity_ids=removed_identity_ids,
        )
    except Exception:
        payload = _apply_payload(
            bundle,
            result,
            None,
            preview,
            status="committed_index_incomplete",
            indexed=False,
            committed_error_code="organization_committed_index_incomplete",
            committed_error_message=(
                "The batch is durably committed, but index reconciliation did not complete; "
                "retry apply with the same confirmation token."
            ),
        )
        _audit(
            "apply_organization_manifest",
            started,
            mode=mode,
            status="committed_index_incomplete",
            manifest_sha256=bundle.manifest_sha256,
            batch_id=result.batch_id,
            already_committed=result.already_committed,
        )
        return payload, None, None
    try:
        final_report_sha256 = _current_report_hash(app)
    except Exception:
        final_report_sha256 = None
    if final_report_sha256 is None or not hmac.compare_digest(
        final_report_sha256,
        result.projected_report_sha256,
    ):
        detail = (
            "could not be verified"
            if final_report_sha256 is None
            else "differs from the validated projection"
        )
        payload = _apply_payload(
            bundle,
            result,
            final_report_sha256,
            preview,
            status="committed_report_mismatch",
            indexed=True,
            committed_error_code="organization_committed_report_mismatch",
            committed_error_message=(
                f"The batch is durably committed and reconciled, but the final planner report "
                f"{detail}; retry apply with the same confirmation token."
            ),
        )
        _audit(
            "apply_organization_manifest",
            started,
            mode=mode,
            status="committed_report_mismatch",
            manifest_sha256=bundle.manifest_sha256,
            batch_id=result.batch_id,
            already_committed=result.already_committed,
            projected_report_sha256=result.projected_report_sha256,
            final_report_sha256=final_report_sha256,
        )
        return payload, index_stats, final_report_sha256
    return None, index_stats, final_report_sha256


async def _apply_organization_manifest_impl(
    app: DatacronApp,
    *,
    manifest_path: str,
    expected_manifest_sha256: str,
    mode: OrganizationManifestMode,
    confirmation_token: str | None = None,
    actor: str = "direct-call",
) -> dict[str, Any]:
    """Validate or crash-consistently apply one external organization manifest bundle."""
    started = time.perf_counter()
    audit_fields = {
        "mode": mode,
        "expected_manifest_sha256": expected_manifest_sha256,
    }
    try:
        app.write_policy.ensure_writable()
        if type(app.scope) is not SingleTenantVaultScope:
            raise OrganizationScopeError(
                "organization batches are unavailable with an injected scope restriction"
            )
        path, expected, cleaned_mode, token = _validate_request(
            manifest_path,
            expected_manifest_sha256,
            mode,
            confirmation_token,
        )
        bundle = _load_expected_bundle(path, expected, app)
        if cleaned_mode == "validate":
            validation_preview = _build_preview(app, bundle)
            batch_writer = cast("OrganizationBatchWriter", app.vault_writer)
            validation_operation = _organization_operation_context(
                validation_preview,
                actor=actor,
            )
            await batch_writer.validate_organization_manifest_capacity(
                validation_preview.validated,
                confirmation_token=validation_preview.confirmation_token,
                projected_report_sha256=validation_preview.projected_report_sha256,
                operation=validation_operation,
            )
            payload = _validation_payload(validation_preview)
            _audit(
                "apply_organization_manifest",
                started,
                mode=cleaned_mode,
                status="validated",
                manifest_sha256=bundle.manifest_sha256,
                payload_set_sha256=bundle.payload_set_sha256,
                scope_digest=validation_preview.validated.scope_digest,
                projected_report_sha256=validation_preview.projected_report_sha256,
                operation_count=payload["operation_count"],
                identity_sidecar_case_canonicalization_count=(
                    payload["identity_sidecar_case_canonicalization_count"]
                ),
                identity_sidecar_case_canonicalization_sha256=(
                    payload["identity_sidecar_case_canonicalization_sha256"]
                ),
                total_payload_bytes=bundle.total_payload_bytes,
            )
            return payload

        if token is None:
            raise AssertionError("apply token validation did not run")
        preview: _OrganizationPreview | None = None
        async with app.reconcile_lock:
            batch_writer = cast("OrganizationBatchWriter", app.vault_writer)
            existing = await batch_writer.resolve_organization_batch_result(bundle.manifest_sha256)
            if existing is not None:
                if not hmac.compare_digest(existing.confirmation_token, token):
                    raise OrganizationConfirmationError(
                        "manifest was committed with a different confirmation_token"
                    )
                result = existing
            else:
                preview = _build_preview(app, bundle)
                _assert_preview_token(preview, token)

                def precommit_validator() -> None:
                    locked_bundle = _load_expected_bundle(path, expected, app)
                    locked_preview = _build_preview(app, locked_bundle)
                    _assert_preview_token(locked_preview, token)
                    if locked_preview.projected_report_sha256 != preview.projected_report_sha256:
                        raise OrganizationConfirmationError(
                            "projected organization report changed before commit"
                        )

                result = await batch_writer.apply_organization_manifest(
                    preview.validated,
                    confirmation_token=token,
                    projected_report_sha256=preview.projected_report_sha256,
                    precommit_validator=precommit_validator,
                    operation=_organization_operation_context(preview, actor=actor),
                )

            incomplete, index_stats, final_report_sha256 = await _finalize_committed_batch(
                app,
                bundle,
                result,
                preview,
                started=started,
                mode=cleaned_mode,
            )
        if incomplete is not None:
            return incomplete
        if index_stats is None or final_report_sha256 is None:
            raise AssertionError("successful organization finalization lacks verified evidence")
        payload = _apply_payload(bundle, result, final_report_sha256, preview)
        _audit(
            "apply_organization_manifest",
            started,
            mode=cleaned_mode,
            status="applied",
            manifest_sha256=bundle.manifest_sha256,
            batch_id=result.batch_id,
            operation_count=len(result.members),
            identity_sidecar_case_canonicalization_count=(
                result.identity_sidecar_case_canonicalization_count
            ),
            identity_sidecar_case_canonicalization_sha256=(
                result.identity_sidecar_case_canonicalization_sha256
            ),
            already_committed=result.already_committed,
            projected_report_sha256=result.projected_report_sha256,
            final_report_sha256=final_report_sha256,
            reindexed_notes=index_stats["reindexed_notes"],
            deleted_notes=index_stats["deleted_notes"],
        )
        return payload
    except (
        BatchConflictError,
        DurabilityUnavailableError,
        OrganizationConfigurationError,
        OrganizationConfirmationError,
        OrganizationFinalReportError,
        OrganizationHistoryError,
        OrganizationManifestError,
        OrganizationProjectionError,
        OrganizationScopeError,
        PathConfinementError,
        ReadOnlyModeError,
        RecoveryRequiredError,
        ValueError,
        VaultLockBusyError,
    ) as exc:
        return _error_response(
            "apply_organization_manifest",
            exc,
            started,
            **audit_fields,
        )
    except Exception:
        return _internal_error_response(
            "apply_organization_manifest",
            started,
            **audit_fields,
        )
