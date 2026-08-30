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
"""Fail-closed validation for content-addressed organization bundles.

The module performs no mutation. Loading authenticates an external bundle and
its payload set. Validation then binds that immutable bundle to one exact live
vault state so a transaction layer can re-run the same preflight under its
writer lock before committing any bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Annotated, Final, Literal, TypeAlias, final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ulid import ULID
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from datacron.core.config import VaultConfig
from datacron.core.frontmatter import (
    FrontmatterError,
    build_tiered_alias_index,
    coerce_string_list,
    extract_tags,
    parse,
    resolve_note_title,
)
from datacron.core.paths import PathConfinementError, assert_within_paths
from datacron.core.scope import (
    LinkedPathError,
    NoteAdmissionError,
    NoteAdmissionPolicy,
    VaultScope,
    assert_path_chain_without_links,
)
from datacron.core.vault import MIGRATED_ULID_SIDECAR_FILENAME, ULID_SIDECAR_FILENAME
from datacron.organization.planner import OrganizationNoteSnapshot

__all__ = [
    "MAX_MANIFEST_BYTES",
    "MAX_OPERATION_COUNT",
    "MAX_PAYLOAD_BYTES",
    "MAX_TOTAL_PAYLOAD_BYTES",
    "ORGANIZATION_MANIFEST_SCHEMA",
    "CreateExactOperation",
    "ExistingNoteIdentity",
    "IdentitySidecarCaseCanonicalization",
    "MoveReplaceExactOperation",
    "NoteIdentity",
    "OrganizationBundle",
    "OrganizationManifest",
    "OrganizationManifestError",
    "OrganizationOperation",
    "OrganizationPayload",
    "OrganizationScopeNotePrecondition",
    "ReplaceExactOperation",
    "ResolvedOrganizationOperation",
    "ValidatedOrganizationBundle",
    "VaultConfigReplaceExact",
    "canonicalize_identity_sidecar_case_collisions",
    "hash_identity_sidecar_case_canonicalizations",
    "load_and_validate_organization_bundle",
    "load_organization_bundle",
    "normalize_vault_rel_path",
    "parse_organization_config_document",
    "parse_organization_note_strict",
    "sha256_bytes",
    "validate_organization_bundle",
]

ORGANIZATION_MANIFEST_SCHEMA: Final[str] = "organization-apply-v1"
MAX_MANIFEST_BYTES: Final[int] = 1024 * 1024
MAX_OPERATION_COUNT: Final[int] = 512
MAX_PAYLOAD_BYTES: Final[int] = 2 * 1024 * 1024
MAX_TOTAL_PAYLOAD_BYTES: Final[int] = 16 * 1024 * 1024

_MANIFEST_SUFFIX: Final[str] = ".json"
_MARKDOWN_SUFFIX: Final[Literal[".md"]] = ".md"
_YAML_SUFFIX: Final[Literal[".yaml"]] = ".yaml"
_PAYLOAD_DIRECTORY_NAME: Final[str] = "payloads"
_VAULT_CONFIG_REL_PATH: Final[str] = ".datacron/VAULT.yaml"
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_LEGACY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Z]{26}$")
_WINDOWS_INVALID_PATH_CHARACTERS: Final[frozenset[str]] = frozenset('<>"|?*')
_WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"com{number}" for number in "¹²³"),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in "¹²³"),
    }
)
_JSON_SEPARATORS: Final[tuple[str, str]] = (",", ":")
_H1_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?m)^#\s+(.+?)\s*$")
_ORGANIZATION_PATH_SENTINEL: Final[str] = "__datacron_organization_path__.md"
_IDENTITY_CASE_CANONICALIZATION_SCHEMA: Final[str] = "identity-sidecar-case-canonicalizations-v1"

PayloadSuffix = Literal[".md", ".yaml"]
OperationKind = Literal["create_exact", "replace_exact", "move_replace_exact"]


@final
class OrganizationManifestError(ValueError):
    """Raised when a bundle cannot be authenticated against the live vault."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("value must be a lowercase 64-character SHA-256 digest")
    return value


def _validate_aliases(value: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    for alias in value:
        if not alias or alias != alias.strip():
            raise ValueError("aliases must be non-empty and must not have surrounding whitespace")
        if any(ord(character) < 32 for character in alias):
            raise ValueError("aliases must not contain control characters")
        normalized = alias.casefold()
        if normalized in seen:
            raise ValueError(f"duplicate alias under case-insensitive comparison: {alias!r}")
        seen.add(normalized)
    return value


def _validate_note_rel_path(value: str) -> str:
    if not value:
        raise ValueError("vault-relative note path must not be empty")
    if any(ord(character) < 32 for character in value):
        raise ValueError("vault-relative note path must not contain control characters")
    if "\\" in value:
        raise ValueError("vault-relative note path must use POSIX separators")
    if ":" in value:
        raise ValueError("vault-relative note path must not contain an alternate data stream")
    if any(character in _WINDOWS_INVALID_PATH_CHARACTERS for character in value):
        raise ValueError("vault-relative note path contains a Windows-invalid character")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.drive or windows_path.is_absolute() or posix_path.is_absolute():
        raise ValueError("vault-relative note path must not be absolute or UNC")
    if not posix_path.parts or any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError("vault-relative note path must not traverse directories")
    if posix_path.as_posix() != value:
        raise ValueError("vault-relative note path must be canonical POSIX")
    for part in posix_path.parts:
        if part.endswith((" ", ".")):
            raise ValueError("vault-relative path components must not end with a dot or space")
        if part.split(".", maxsplit=1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"vault-relative path contains reserved Windows name: {part!r}")
    if not value.endswith(_MARKDOWN_SUFFIX):
        raise ValueError("organization note paths must end with '.md'")
    return value


def normalize_vault_rel_path(rel_path: str) -> str:
    """Return the deterministic collision key for one validated vault path."""
    return PurePosixPath(_validate_note_rel_path(rel_path)).as_posix().casefold()


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""
    return hashlib.sha256(content).hexdigest()


def _filesystem_path_key(value: str) -> str:
    """Normalize case only on filesystems whose path contract is case-insensitive."""
    return value.casefold() if os.name == "nt" else value


class ExistingNoteIdentity(_StrictModel):
    """Identity preserved from an existing note, including bounded legacy IDs."""

    id: str
    aliases: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if _LEGACY_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("existing id must be 26 uppercase alphanumeric characters")
        return value

    @field_validator("aliases")
    @classmethod
    def _validate_identity_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_aliases(value)


@final
class NoteIdentity(ExistingNoteIdentity):
    """Canonical Crockford identity required for every newly created note."""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if _ULID_PATTERN.fullmatch(value) is None:
            raise ValueError("id must be a canonical 26-character Crockford ULID")
        return value


@final
class CreateExactOperation(_StrictModel):
    """Create one absent Markdown target from authenticated payload bytes."""

    kind: Literal["create_exact"]
    target: str
    payload_sha256: str
    result: NoteIdentity

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_note_rel_path(value)

    @field_validator("payload_sha256")
    @classmethod
    def _validate_payload_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


@final
class ReplaceExactOperation(_StrictModel):
    """Replace one existing Markdown target under byte and identity CAS."""

    kind: Literal["replace_exact"]
    target: str
    expected_sha256: str
    expected: ExistingNoteIdentity
    payload_sha256: str
    result: ExistingNoteIdentity

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_note_rel_path(value)

    @field_validator("expected_sha256", "payload_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def _preserve_id(self) -> ReplaceExactOperation:
        if self.expected.id != self.result.id:
            raise ValueError("replace_exact must preserve the source note id")
        return self


@final
class MoveReplaceExactOperation(_StrictModel):
    """Delete one exact source and create one absent target from payload bytes."""

    kind: Literal["move_replace_exact"]
    source: str
    target: str
    expected_sha256: str
    expected: ExistingNoteIdentity
    payload_sha256: str
    result: ExistingNoteIdentity

    @field_validator("source", "target")
    @classmethod
    def _validate_paths(cls, value: str) -> str:
        return _validate_note_rel_path(value)

    @field_validator("expected_sha256", "payload_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def _preserve_id_and_reject_case_only(self) -> MoveReplaceExactOperation:
        if self.expected.id != self.result.id:
            raise ValueError("move_replace_exact must preserve the source note id")
        if normalize_vault_rel_path(self.source) == normalize_vault_rel_path(self.target):
            raise ValueError("move_replace_exact must not be a case-only rename")
        return self


OrganizationOperation: TypeAlias = Annotated[
    CreateExactOperation | ReplaceExactOperation | MoveReplaceExactOperation,
    Field(discriminator="kind"),
]


@final
class VaultConfigReplaceExact(_StrictModel):
    """Replace the sole supported vault configuration file under byte CAS."""

    kind: Literal["replace_exact"]
    target: Literal[".datacron/VAULT.yaml"]
    expected_sha256: str
    payload_sha256: str

    @field_validator("expected_sha256", "payload_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return _validate_sha256(value)


@final
class OrganizationManifest(_StrictModel):
    """Schema ``organization-apply-v1`` with bounded exact operations."""

    schema_: Literal["organization-apply-v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    operations: tuple[OrganizationOperation, ...] = Field(max_length=MAX_OPERATION_COUNT)
    config: VaultConfigReplaceExact | None = None

    @model_validator(mode="after")
    def _validate_graph(self) -> OrganizationManifest:
        operation_count = len(self.operations) + int(self.config is not None)
        if operation_count == 0:
            raise ValueError("manifest must contain at least one operation")
        if operation_count > MAX_OPERATION_COUNT:
            raise ValueError(f"manifest exceeds the {MAX_OPERATION_COUNT} operation limit")

        write_targets: dict[str, str] = {}
        move_sources: dict[str, str] = {}
        result_ids: dict[str, str] = {}
        result_aliases: dict[str, str] = {}
        for operation in self.operations:
            normalized_target = normalize_vault_rel_path(operation.target)
            previous_target = write_targets.get(normalized_target)
            if previous_target is not None:
                raise ValueError(
                    f"duplicate write target under normcase: {previous_target!r}, "
                    f"{operation.target!r}"
                )
            write_targets[normalized_target] = operation.target

            if isinstance(operation, MoveReplaceExactOperation):
                normalized_source = normalize_vault_rel_path(operation.source)
                previous_source = move_sources.get(normalized_source)
                if previous_source is not None:
                    raise ValueError(
                        f"duplicate move source under normcase: {previous_source!r}, "
                        f"{operation.source!r}"
                    )
                move_sources[normalized_source] = operation.source

            normalized_id = operation.result.id.casefold()
            previous_id_path = result_ids.get(normalized_id)
            if previous_id_path is not None:
                raise ValueError(
                    f"duplicate result note id for {previous_id_path!r} and {operation.target!r}"
                )
            result_ids[normalized_id] = operation.target
            for alias in operation.result.aliases:
                normalized_alias = alias.casefold()
                previous_alias_path = result_aliases.get(normalized_alias)
                if previous_alias_path is not None:
                    raise ValueError(
                        f"duplicate result alias {alias!r} for {previous_alias_path!r} "
                        f"and {operation.target!r}"
                    )
                result_aliases[normalized_alias] = operation.target

        overlap = sorted(set(write_targets).intersection(move_sources))
        if overlap:
            source = move_sources[overlap[0]]
            target = write_targets[overlap[0]]
            raise ValueError(
                f"operation chains and cycles are forbidden; {source!r} is also target {target!r}"
            )
        return self


@final
@dataclass(frozen=True, slots=True)
class OrganizationPayload:
    """One authenticated UTF-8 payload from the external bundle."""

    sha256: str
    path: Path
    raw_bytes: bytes
    text: str
    suffix: PayloadSuffix


@final
@dataclass(frozen=True, slots=True)
class OrganizationBundle:
    """Authenticated manifest and payload set, independent of live vault state."""

    manifest_path: Path
    bundle_root: Path
    manifest_bytes: bytes
    manifest_sha256: str
    payload_set_sha256: str
    manifest: OrganizationManifest
    payloads: Mapping[tuple[str, PayloadSuffix], OrganizationPayload]
    total_payload_bytes: int

    def get_payload(self, sha256: str, suffix: PayloadSuffix) -> OrganizationPayload:
        """Return one referenced payload or fail closed if the key is absent."""
        try:
            return self.payloads[(sha256, suffix)]
        except KeyError as exc:
            raise OrganizationManifestError(
                "payload_missing",
                f"authenticated payload is absent: {sha256}{suffix}",
            ) from exc


@final
@dataclass(frozen=True, slots=True)
class ResolvedOrganizationOperation:
    """One exact operation bound to absolute paths and authenticated bytes."""

    operation: OrganizationOperation
    source_path: Path | None
    target_path: Path
    payload: OrganizationPayload

    @property
    def kind(self) -> OperationKind:
        """Return the discriminated operation kind."""
        return self.operation.kind

    @property
    def before_sha256(self) -> str | None:
        """Return the live source hash required by CAS, if any."""
        if isinstance(self.operation, CreateExactOperation):
            return None
        return self.operation.expected_sha256

    @property
    def after_sha256(self) -> str:
        """Return the exact hash of bytes that the transaction must write."""
        return self.payload.sha256

    @property
    def expected_identity(self) -> ExistingNoteIdentity | None:
        """Return the required live identity, if the operation has a source."""
        if isinstance(self.operation, CreateExactOperation):
            return None
        return self.operation.expected

    @property
    def result_identity(self) -> ExistingNoteIdentity:
        """Return the identity authenticated in the result payload."""
        return self.operation.result


@final
@dataclass(frozen=True, slots=True)
class ValidatedOrganizationBundle:
    """Bundle bound to one exact, non-mutated live vault state."""

    manifest_path: Path
    bundle_root: Path
    manifest_bytes: bytes
    manifest_sha256: str
    payload_set_sha256: str
    manifest: OrganizationManifest
    payloads: Mapping[tuple[str, PayloadSuffix], OrganizationPayload]
    total_payload_bytes: int
    operations: tuple[ResolvedOrganizationOperation, ...]
    config_path: Path | None
    config_payload: OrganizationPayload | None
    config_before_sha256: str
    target_config: VaultConfig
    projected_notes: tuple[OrganizationNoteSnapshot, ...]
    identity_sidecar_path: Path
    identity_sidecar_before_sha256: str | None
    identity_sidecar_after_bytes: bytes | None
    identity_sidecar_after_sha256: str | None
    migrated_identity_sidecar_path: Path
    migrated_identity_sidecar_before_sha256: str | None
    scope_note_preconditions: tuple[OrganizationScopeNotePrecondition, ...]
    scope_note_count: int
    scope_digest: str
    identity_sidecar_case_canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...] = ()

    @property
    def identity_sidecar_case_canonicalization_count(self) -> int:
        """Return the number of proven obsolete mappings in the after-image."""
        return len(self.identity_sidecar_case_canonicalizations)

    @property
    def identity_sidecar_case_canonicalization_sha256(self) -> str:
        """Return the stable content-free digest shown and signed at preview."""
        return hash_identity_sidecar_case_canonicalizations(
            self.identity_sidecar_case_canonicalizations
        )


@final
@dataclass(frozen=True, slots=True)
class OrganizationScopeNotePrecondition:
    """Content-free exact pre-state used to authenticate crash recovery."""

    rel_path: str
    sha256: str


@final
@dataclass(frozen=True, slots=True)
class IdentitySidecarCaseCanonicalization:
    """One mechanically proven obsolete case alias removed from ``ulids.json``."""

    stale_path: str
    stale_id: str
    live_path: str
    live_id: str


@final
@dataclass(frozen=True, slots=True)
class _ProjectedIdentity:
    rel_path: str
    note_id: str
    frontmatter_id: str | None
    title: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    calendar_date: str | None
    sha256: str
    size: int

    @property
    def stem(self) -> str:
        return PurePosixPath(self.rel_path).stem


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=_JSON_SEPARATORS,
    ).encode("utf-8")


def hash_identity_sidecar_case_canonicalizations(
    canonicalizations: tuple[IdentitySidecarCaseCanonicalization, ...],
) -> str:
    """Hash a stable, content-free description of derived case cleanup."""
    items = [
        {
            "live_id": item.live_id,
            "live_path": item.live_path,
            "stale_id": item.stale_id,
            "stale_path": item.stale_path,
        }
        for item in sorted(
            canonicalizations,
            key=lambda item: (item.stale_path.casefold(), item.stale_path),
        )
    ]
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "canonicalizations": items,
                "schema": _IDENTITY_CASE_CANONICALIZATION_SCHEMA,
            }
        )
    )


def _read_bounded_bytes(path: Path, *, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Cannot stat {label}: {exc}",
        ) from exc
    if size > limit:
        raise OrganizationManifestError(
            "bundle_limit_exceeded",
            f"{label} is {size} bytes; limit is {limit}",
        )
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except OSError as exc:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Cannot read {label}: {exc}",
        ) from exc
    if len(content) > limit:
        raise OrganizationManifestError(
            "bundle_limit_exceeded",
            f"{label} exceeds the {limit}-byte bounded-read limit",
        )
    return content


def _decode_utf8(content: bytes, *, label: str) -> str:
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OrganizationManifestError(
            "invalid_utf8",
            f"{label} is not strict UTF-8: {exc}",
        ) from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while refusing ambiguous duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> object:
    """Reject Python's non-standard NaN and infinity JSON extensions."""
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _assert_unique_yaml_mapping_keys(text: str) -> None:
    """Reject aliases, non-string keys, and duplicate keys in safe YAML."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    pending: list[Node] = [] if root is None else [root]
    visited: set[int] = set()
    while pending:
        node = pending.pop()
        node_identity = id(node)
        if node_identity in visited:
            raise yaml.YAMLError("YAML aliases are forbidden")
        visited.add(node_identity)
        if isinstance(node, MappingNode):
            seen: set[tuple[str, str]] = set()
            for key_node, value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.YAMLError("YAML merge keys are forbidden")
                if not isinstance(key_node, ScalarNode) or key_node.tag != (
                    "tag:yaml.org,2002:str"
                ):
                    raise yaml.YAMLError("YAML mapping keys must be strings")
                key = (key_node.tag, key_node.value)
                if key in seen:
                    raise yaml.YAMLError(f"found duplicate mapping key {key_node.value!r}")
                seen.add(key)
                pending.extend((key_node, value_node))
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)


def _assert_finite_yaml_values(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("YAML non-finite floats are forbidden")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _strict_frontmatter_block(text: str) -> str | None:
    """Return frontmatter YAML for duplicate-key validation, when present."""
    parseable = text[1:] if text.startswith("\ufeff") else text
    lines = parseable.lstrip().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return None


def parse_organization_note_strict(text: str) -> tuple[dict[str, object], str]:
    """Parse one note after rejecting duplicate YAML frontmatter keys."""
    block = _strict_frontmatter_block(text)
    if block is not None:
        try:
            _assert_unique_yaml_mapping_keys(block)
        except yaml.YAMLError as exc:
            raise FrontmatterError(str(exc)) from exc
    metadata, body = parse(text)
    return dict(metadata), body


def _reject_external_ads_or_unc(path: Path) -> None:
    raw = os.fspath(path)
    if raw.startswith(("\\\\", "//")):
        raise OrganizationManifestError("bundle_path_invalid", "UNC bundle paths are forbidden")
    if ".." in path.parts:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "Parent traversal is forbidden in bundle paths",
        )
    drive = path.drive
    remainder = raw[len(drive) :] if drive else raw
    if ":" in remainder:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Alternate data streams are forbidden in bundle path: {path}",
        )


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_plain_vault_root(vault_root: Path) -> Path:
    if not vault_root.is_absolute():
        raise OrganizationManifestError("vault_path_invalid", "vault_root must be absolute")
    try:
        resolved = assert_path_chain_without_links(vault_root).resolve()
    except (FileNotFoundError, LinkedPathError, OSError) as exc:
        raise OrganizationManifestError(
            "vault_path_invalid",
            f"Vault root is missing, linked, or unreadable: {exc}",
        ) from exc
    if not resolved.is_dir():
        raise OrganizationManifestError("vault_path_invalid", "vault_root is not a directory")
    return resolved


def _validate_payload_identity(
    payload: OrganizationPayload,
    expected: ExistingNoteIdentity,
) -> None:
    try:
        metadata, _body = parse_organization_note_strict(payload.text)
    except (FrontmatterError, ValueError) as exc:
        raise OrganizationManifestError(
            "payload_identity_invalid",
            f"Cannot parse note payload identity {payload.path.name}: {exc}",
        ) from exc
    note_id = metadata.get("id")
    aliases = tuple(coerce_string_list(metadata.get("aliases")))
    if note_id != expected.id or aliases != expected.aliases:
        raise OrganizationManifestError(
            "payload_identity_mismatch",
            f"Payload identity does not match manifest for {payload.path.name}",
        )


def _validate_organization_directory_rel_path(value: str, *, label: str) -> str:
    """Validate one organization directory with the note-path lexical policy."""
    if value != value.strip():
        raise ValueError(f"{label} must not have surrounding whitespace")
    try:
        _validate_note_rel_path(f"{value}/{_ORGANIZATION_PATH_SENTINEL}")
    except ValueError as exc:
        raise ValueError(f"{label} is not a canonical vault-relative path: {exc}") from exc
    return value


def _validate_raw_organization_paths(document: Mapping[str, object]) -> None:
    """Reject dangerous organization paths before config models normalize them."""
    organization = document.get("organization")
    if not isinstance(organization, dict):
        return
    raw_scope = organization.get("scope")
    if isinstance(raw_scope, str) and raw_scope:
        _validate_organization_directory_rel_path(
            raw_scope,
            label="organization.scope",
        )
    raw_rules = organization.get("rules")
    if not isinstance(raw_rules, list):
        return
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            continue
        raw_folder = raw_rule.get("folder")
        if isinstance(raw_folder, str):
            _validate_organization_directory_rel_path(
                raw_folder,
                label=f"organization.rules[{index}].folder",
            )


def parse_organization_config_document(
    text: str,
    *,
    label: str,
) -> tuple[dict[str, object], VaultConfig]:
    try:
        _assert_unique_yaml_mapping_keys(text)
        parsed = yaml.safe_load(text)
        _assert_finite_yaml_values(parsed)
        if not isinstance(parsed, dict):
            raise ValueError("vault configuration payload must be a YAML mapping")
        if any(not isinstance(key, str) for key in parsed):
            raise ValueError("vault configuration keys must be strings")
        document = {key: value for key, value in parsed.items() if isinstance(key, str)}
        _validate_raw_organization_paths(document)
        return document, VaultConfig.model_validate(document)
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        raise OrganizationManifestError(
            "config_payload_invalid",
            f"Invalid {label}: {exc}",
        ) from exc


def _validate_config_payload(payload: OrganizationPayload) -> None:
    parse_organization_config_document(
        payload.text,
        label=f"VAULT.yaml payload {payload.path.name}",
    )


def _referenced_payload_keys(
    manifest: OrganizationManifest,
) -> set[tuple[str, PayloadSuffix]]:
    keys: set[tuple[str, PayloadSuffix]] = {
        (operation.payload_sha256, _MARKDOWN_SUFFIX) for operation in manifest.operations
    }
    if manifest.config is not None:
        keys.add((manifest.config.payload_sha256, _YAML_SUFFIX))
    return keys


def _resolve_external_manifest(manifest_path: Path, vault_root: Path) -> tuple[Path, Path]:
    if not manifest_path.is_absolute():
        raise OrganizationManifestError("bundle_path_invalid", "manifest_path must be absolute")
    _reject_external_ads_or_unc(manifest_path)
    if manifest_path.suffix != _MANIFEST_SUFFIX:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "manifest_path must end with '.json'",
        )
    try:
        resolved_manifest = assert_path_chain_without_links(manifest_path).resolve()
    except (FileNotFoundError, LinkedPathError, OSError) as exc:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Manifest path is missing, linked, or unreadable: {exc}",
        ) from exc
    if not resolved_manifest.is_file():
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "manifest_path is not a regular file",
        )
    resolved_vault = _resolve_plain_vault_root(vault_root)
    bundle_root = resolved_manifest.parent
    if (
        _is_within(resolved_manifest, resolved_vault)
        or _is_within(bundle_root, resolved_vault)
        or _is_within(resolved_vault, bundle_root)
    ):
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "organization bundle must be outside vault",
        )
    return resolved_manifest, bundle_root


def _parse_manifest(manifest_bytes: bytes) -> OrganizationManifest:
    manifest_text = _decode_utf8(manifest_bytes, label="organization manifest")
    try:
        json.loads(
            manifest_text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
        return OrganizationManifest.model_validate_json(manifest_text)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise OrganizationManifestError(
            "manifest_invalid",
            f"Manifest does not satisfy {ORGANIZATION_MANIFEST_SCHEMA}: {exc}",
        ) from exc


def _resolve_payload_root(bundle_root: Path) -> Path:
    payload_root = bundle_root / _PAYLOAD_DIRECTORY_NAME
    try:
        payload_root = assert_path_chain_without_links(payload_root)
    except (FileNotFoundError, LinkedPathError, OSError) as exc:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Payload directory is missing, linked, or unreadable: {exc}",
        ) from exc
    if not payload_root.is_dir():
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "payloads sibling is not a directory",
        )
    return payload_root


def _assert_exact_payload_set(
    payload_root: Path,
    referenced: set[tuple[str, PayloadSuffix]],
) -> None:
    expected_names = {f"{digest}{suffix}" for digest, suffix in referenced}
    try:
        actual_names = {entry.name for entry in payload_root.iterdir()}
    except OSError as exc:
        raise OrganizationManifestError(
            "bundle_path_invalid",
            f"Cannot enumerate payload directory: {exc}",
        ) from exc
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unreferenced = sorted(actual_names - expected_names)
        raise OrganizationManifestError(
            "payload_set_mismatch",
            f"Payload set mismatch; missing={missing}, unreferenced={unreferenced}",
        )


def _load_payload_set(
    payload_root: Path,
    referenced: set[tuple[str, PayloadSuffix]],
) -> tuple[dict[tuple[str, PayloadSuffix], OrganizationPayload], int]:
    payloads: dict[tuple[str, PayloadSuffix], OrganizationPayload] = {}
    total_payload_bytes = 0
    for digest, suffix in sorted(referenced):
        payload_path = payload_root / f"{digest}{suffix}"
        try:
            payload_path = assert_path_chain_without_links(payload_path, anchor=payload_root)
        except (FileNotFoundError, LinkedPathError, OSError) as exc:
            raise OrganizationManifestError(
                "bundle_path_invalid",
                f"Payload path is missing, linked, or unreadable: {exc}",
            ) from exc
        if not payload_path.is_file():
            raise OrganizationManifestError(
                "bundle_path_invalid",
                f"Payload is not a regular file: {payload_path.name}",
            )
        raw_bytes = _read_bounded_bytes(
            payload_path,
            limit=MAX_PAYLOAD_BYTES,
            label=f"payload {payload_path.name}",
        )
        total_payload_bytes += len(raw_bytes)
        if total_payload_bytes > MAX_TOTAL_PAYLOAD_BYTES:
            raise OrganizationManifestError(
                "bundle_limit_exceeded",
                f"Payload set exceeds {MAX_TOTAL_PAYLOAD_BYTES} bytes",
            )
        actual_digest = sha256_bytes(raw_bytes)
        if actual_digest != digest:
            raise OrganizationManifestError(
                "payload_hash_mismatch",
                f"Payload hash mismatch for {payload_path.name}; got {actual_digest}",
            )
        payloads[(digest, suffix)] = OrganizationPayload(
            sha256=digest,
            path=payload_path,
            raw_bytes=raw_bytes,
            text=_decode_utf8(raw_bytes, label=f"payload {payload_path.name}"),
            suffix=suffix,
        )
    return payloads, total_payload_bytes


def load_organization_bundle(manifest_path: Path, *, vault_root: Path) -> OrganizationBundle:
    """Authenticate one external manifest and every referenced payload.

    This phase deliberately does not inspect operation source or target state.
    A transaction layer can therefore check its committed marker by
    ``manifest_sha256`` before deciding whether live preconditions still need
    validation on an idempotent retry.

    Args:
        manifest_path: Absolute path to the JSON manifest outside the vault.
        vault_root: Vault that must not contain the bundle.

    Returns:
        The immutable authenticated bundle.

    Raises:
        OrganizationManifestError: If any path, size, schema, encoding, hash,
            reference, payload identity, or configuration constraint fails.
    """
    resolved_manifest, bundle_root = _resolve_external_manifest(manifest_path, vault_root)
    manifest_bytes = _read_bounded_bytes(
        resolved_manifest,
        limit=MAX_MANIFEST_BYTES,
        label="organization manifest",
    )
    manifest = _parse_manifest(manifest_bytes)
    payload_root = _resolve_payload_root(bundle_root)
    referenced = _referenced_payload_keys(manifest)
    _assert_exact_payload_set(payload_root, referenced)
    payloads, total_payload_bytes = _load_payload_set(payload_root, referenced)

    for operation in manifest.operations:
        _validate_payload_identity(
            payloads[(operation.payload_sha256, _MARKDOWN_SUFFIX)],
            operation.result,
        )
    if manifest.config is not None:
        _validate_config_payload(payloads[(manifest.config.payload_sha256, _YAML_SUFFIX)])

    payload_signature = [
        {"sha256": digest, "size": len(payloads[(digest, suffix)].raw_bytes), "suffix": suffix}
        for digest, suffix in sorted(payloads)
    ]
    return OrganizationBundle(
        manifest_path=resolved_manifest,
        bundle_root=bundle_root,
        manifest_bytes=manifest_bytes,
        manifest_sha256=sha256_bytes(manifest_bytes),
        payload_set_sha256=sha256_bytes(_canonical_json_bytes(payload_signature)),
        manifest=manifest,
        payloads=MappingProxyType(payloads),
        total_payload_bytes=total_payload_bytes,
    )


def _resolve_scoped_path(
    vault_root: Path,
    rel_path: str,
    scope: VaultScope,
    *,
    access: Literal["read", "write"],
    allow_missing: bool,
) -> Path:
    lexical = vault_root / PurePosixPath(rel_path)
    try:
        unlinked = assert_path_chain_without_links(
            lexical,
            anchor=vault_root,
            allow_missing=allow_missing,
        )
        scoped = scope.authorize_rel_path(rel_path, access)
        confined = assert_within_paths(scoped, [vault_root], kind=access)
    except (FileNotFoundError, LinkedPathError, OSError, PathConfinementError, RuntimeError) as exc:
        raise OrganizationManifestError(
            "vault_path_invalid",
            f"Vault path is linked, inaccessible, or outside scope: {rel_path!r}: {exc}",
        ) from exc
    resolved_lexical = unlinked.resolve(strict=False)
    if confined != resolved_lexical:
        raise OrganizationManifestError(
            "vault_path_invalid",
            f"Scope redirected vault path {rel_path!r}",
        )
    return confined


def _assert_absent_case_insensitive(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise OrganizationManifestError(
            "target_not_absent",
            f"Exact-create target already exists: {path}",
        )
    parent = path.parent
    if not parent.is_dir():
        return
    try:
        collision = next(
            (entry for entry in parent.iterdir() if entry.name.casefold() == path.name.casefold()),
            None,
        )
    except OSError as exc:
        raise OrganizationManifestError(
            "vault_path_invalid",
            f"Cannot inspect target parent {parent}: {exc}",
        ) from exc
    if collision is not None:
        raise OrganizationManifestError(
            "target_not_absent",
            f"Case-insensitive target collision: {collision}",
        )


def _read_expected_note(
    path: Path,
    *,
    expected_sha256: str,
    expected_identity: ExistingNoteIdentity,
) -> None:
    if not path.is_file():
        raise OrganizationManifestError("source_missing", f"Exact source is not a file: {path}")
    raw_bytes = _read_bounded_bytes(
        path,
        limit=MAX_PAYLOAD_BYTES,
        label=f"source note {path}",
    )
    actual_sha256 = sha256_bytes(raw_bytes)
    if actual_sha256 != expected_sha256:
        raise OrganizationManifestError(
            "source_hash_mismatch",
            f"Source hash mismatch for {path}; expected {expected_sha256}, got {actual_sha256}",
        )
    text = _decode_utf8(raw_bytes, label=f"source {path}")
    try:
        metadata, _body = parse_organization_note_strict(text)
    except (FrontmatterError, ValueError) as exc:
        raise OrganizationManifestError(
            "source_identity_invalid",
            f"Cannot parse source identity {path}: {exc}",
        ) from exc
    source_id = metadata.get("id")
    if not isinstance(source_id, str):
        raise OrganizationManifestError(
            "source_identity_invalid",
            f"Source note has no frontmatter id: {path}",
        )
    actual_identity = ExistingNoteIdentity(
        id=source_id,
        aliases=tuple(coerce_string_list(metadata.get("aliases"))),
    )
    if actual_identity != expected_identity:
        raise OrganizationManifestError(
            "source_identity_mismatch",
            f"Source identity mismatch for {path}",
        )


def _read_expected_file(path: Path, *, expected_sha256: str, label: str) -> None:
    if not path.is_file():
        raise OrganizationManifestError("source_missing", f"{label} is not a file: {path}")
    actual_sha256 = sha256_bytes(_read_bounded_bytes(path, limit=MAX_PAYLOAD_BYTES, label=label))
    if actual_sha256 != expected_sha256:
        raise OrganizationManifestError(
            "source_hash_mismatch",
            f"{label} hash mismatch; expected {expected_sha256}, got {actual_sha256}",
        )


def _read_live_config(
    path: Path,
) -> tuple[bytes, dict[str, object], VaultConfig]:
    if not path.is_file():
        raise OrganizationManifestError("source_missing", f"VAULT.yaml is not a file: {path}")
    raw_bytes = _read_bounded_bytes(
        path,
        limit=MAX_PAYLOAD_BYTES,
        label="live VAULT.yaml",
    )
    text = _decode_utf8(raw_bytes, label="live VAULT.yaml")
    document, config = parse_organization_config_document(text, label="live VAULT.yaml")
    return raw_bytes, document, config


def _active_organization_scope(config: VaultConfig, *, label: str) -> str:
    organization = config.organization
    if organization is None or not organization.rules or organization.scope is None:
        raise OrganizationManifestError(
            "organization_scope_missing",
            f"{label} must declare active organization rules and a scope",
        )
    scope = organization.scope
    try:
        _validate_organization_directory_rel_path(
            scope,
            label=f"{label} organization.scope",
        )
    except ValueError as exc:
        raise OrganizationManifestError(
            "organization_scope_invalid",
            str(exc),
        ) from exc
    return scope


def _compare_config_scope(
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> None:
    before_without_organization = {
        key: value for key, value in before.items() if key != "organization"
    }
    after_without_organization = {
        key: value for key, value in after.items() if key != "organization"
    }
    if not _yaml_values_equal_exact(
        before_without_organization,
        after_without_organization,
    ):
        raise OrganizationManifestError(
            "config_scope_violation",
            "VAULT.yaml payload may change only the top-level organization field",
        )


def _yaml_values_equal_exact(before: object, after: object) -> bool:
    """Compare parsed YAML without Python's cross-type scalar equality."""
    if type(before) is not type(after):
        return False
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(
            _yaml_values_equal_exact(value, after[key]) for key, value in before.items()
        )
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _yaml_values_equal_exact(left, right) for left, right in zip(before, after, strict=True)
        )
    return bool(before == after)


def _read_vault_id_mapping(path: Path, *, vault_root: Path) -> tuple[bytes, dict[str, str]]:
    if not path.exists() and not path.is_symlink():
        return b"", {}
    safe_path = assert_path_chain_without_links(path, anchor=vault_root)
    if not safe_path.is_file():
        raise ValueError(f"ULID sidecar is not a regular file: {path.name}")
    raw_bytes = _read_bounded_bytes(
        safe_path,
        limit=MAX_PAYLOAD_BYTES,
        label=f"ULID sidecar {path.name}",
    )
    if not raw_bytes:
        raise ValueError(f"ULID sidecar must not be empty: {path.name}")
    parsed = json.loads(
        _decode_utf8(raw_bytes, label=f"ULID sidecar {path.name}"),
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json_constant,
    )
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValueError(f"ULID sidecar must contain only string pairs: {path.name}")
    return raw_bytes, {
        key: value
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _load_vault_id_state(
    vault_root: Path,
) -> tuple[bytes, dict[str, str], bytes, dict[str, str], dict[str, str]]:
    sidecar = vault_root / ".datacron" / ULID_SIDECAR_FILENAME
    migrated = vault_root / ".datacron" / MIGRATED_ULID_SIDECAR_FILENAME
    try:
        primary_bytes, primary = _read_vault_id_mapping(sidecar, vault_root=vault_root)
        migrated_bytes, migrated_values = _read_vault_id_mapping(
            migrated,
            vault_root=vault_root,
        )
    except (LinkedPathError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise OrganizationManifestError(
            "identity_inventory_invalid",
            f"Cannot read vault ULID identity mappings: {exc}",
        ) from exc
    merged = dict(primary)
    merged.update(migrated_values)
    return primary_bytes, primary, migrated_bytes, migrated_values, merged


def _scope_excluded_folders(scope: VaultScope) -> frozenset[str]:
    policy = getattr(scope, "admission_policy", None)
    if isinstance(policy, NoteAdmissionPolicy):
        return policy.excluded_folders
    return frozenset()


def _assert_target_note_admitted(scope: VaultScope, rel_path: str) -> None:
    """Reject exact-create targets excluded by the canonical note policy."""
    policy = getattr(scope, "admission_policy", None)
    if not isinstance(policy, NoteAdmissionPolicy):
        return
    parts = PurePosixPath(rel_path).parts
    if (
        any(
            part.startswith(".") or part.casefold() in policy.excluded_folders
            for part in parts[:-1]
        )
        or parts[-1].casefold() in policy.excluded_files
    ):
        raise OrganizationManifestError(
            "target_not_admitted",
            f"Operation target is not admitted by note admission policy: {rel_path!r}",
        )


def _collect_admitted_note_paths(vault_root: Path, scope: VaultScope) -> tuple[Path, ...]:
    pending = [vault_root]
    discovered: list[Path] = []
    excluded_folders = _scope_excluded_folders(scope)
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise OrganizationManifestError(
                "source_unreadable",
                f"Cannot enumerate vault directory {directory}: {exc}",
            ) from exc
        for entry in entries:
            try:
                unlinked = assert_path_chain_without_links(entry, anchor=vault_root)
            except (FileNotFoundError, LinkedPathError, OSError) as exc:
                raise OrganizationManifestError(
                    "vault_path_invalid",
                    f"Linked or unreadable vault entry: {exc}",
                ) from exc
            if unlinked.is_dir():
                if entry.name.startswith(".") or entry.name.casefold() in excluded_folders:
                    continue
                pending.append(unlinked)
                continue
            if not unlinked.is_file() or unlinked.suffix.casefold() != _MARKDOWN_SUFFIX:
                continue
            rel_path = unlinked.relative_to(vault_root).as_posix()
            try:
                admitted = scope.authorize_note_rel_path(rel_path)
            except NoteAdmissionError:
                continue
            except (PathConfinementError, RuntimeError) as exc:
                raise OrganizationManifestError(
                    "source_not_admitted",
                    f"Cannot authorize admitted note {rel_path!r}: {exc}",
                ) from exc
            if admitted != unlinked.resolve():
                raise OrganizationManifestError(
                    "vault_path_invalid",
                    f"Scope redirected admitted note {rel_path!r}",
                )
            discovered.append(unlinked)
    return tuple(sorted(discovered, key=lambda path: path.relative_to(vault_root).as_posix()))


def _fallback_note_id(rel_path: str) -> str:
    digest = hashlib.sha256(f"datacron-rel-path-id\x00{rel_path}".encode()).digest()
    return str(ULID.from_bytes(digest[:16]))


def _calendar_date(metadata: Mapping[str, object]) -> str | None:
    """Return the planner's created-then-updated local calendar date."""
    for key in ("created", "updated"):
        value = metadata.get(key)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        try:
            return datetime.fromisoformat(candidate).date().isoformat()
        except ValueError:
            try:
                return date.fromisoformat(candidate).isoformat()
            except ValueError:
                continue
    return None


def _read_projected_identity(
    path: Path,
    *,
    vault_root: Path,
    id_mappings: Mapping[str, str],
) -> _ProjectedIdentity:
    rel_path = path.relative_to(vault_root).as_posix()
    try:
        raw_bytes = _read_bounded_bytes(
            path,
            limit=MAX_PAYLOAD_BYTES,
            label=f"admitted note {rel_path}",
        )
        text = raw_bytes.decode("utf-8", errors="strict")
        metadata, body = parse_organization_note_strict(text)
    except (OSError, UnicodeDecodeError, FrontmatterError, ValueError) as exc:
        raise OrganizationManifestError(
            "identity_inventory_invalid",
            f"Cannot read projected identity for {rel_path!r}: {exc}",
        ) from exc
    frontmatter_id = metadata.get("id")
    note_id = (
        frontmatter_id
        if isinstance(frontmatter_id, str) and len(frontmatter_id) == 26
        else id_mappings.get(rel_path, _fallback_note_id(rel_path))
    )
    return _ProjectedIdentity(
        rel_path=rel_path,
        note_id=note_id,
        frontmatter_id=frontmatter_id if isinstance(frontmatter_id, str) else None,
        title=resolve_note_title(
            metadata,
            body,
            path,
            h1_pattern=_H1_PATTERN,
            empty_h1_falls_back=True,
        ),
        aliases=tuple(coerce_string_list(metadata.get("aliases"), keep_empty_scalar=True)),
        tags=tuple(extract_tags(metadata, body)),
        calendar_date=_calendar_date(metadata),
        sha256=sha256_bytes(raw_bytes),
        size=len(raw_bytes),
    )


def _inventory_admitted_identities(
    vault_root: Path,
    scope: VaultScope,
    id_mappings: Mapping[str, str],
) -> dict[str, _ProjectedIdentity]:
    identities: dict[str, _ProjectedIdentity] = {}
    for path in _collect_admitted_note_paths(vault_root, scope):
        identity = _read_projected_identity(path, vault_root=vault_root, id_mappings=id_mappings)
        normalized = PurePosixPath(identity.rel_path).as_posix().casefold()
        existing = identities.get(normalized)
        if existing is not None:
            raise OrganizationManifestError(
                "vault_path_collision",
                f"Case-insensitive admitted note collision: "
                f"{existing.rel_path!r}, {identity.rel_path!r}",
            )
        identities[normalized] = identity
    return identities


def _identity_from_payload(
    operation: OrganizationOperation,
    payload: OrganizationPayload,
) -> _ProjectedIdentity:
    try:
        metadata, body = parse_organization_note_strict(payload.text)
    except (FrontmatterError, ValueError) as exc:
        raise OrganizationManifestError(
            "payload_identity_invalid",
            f"Cannot parse result identity for {operation.target!r}: {exc}",
        ) from exc
    raw_frontmatter_id = metadata.get("id")
    frontmatter_id = raw_frontmatter_id if isinstance(raw_frontmatter_id, str) else None
    return _ProjectedIdentity(
        rel_path=operation.target,
        note_id=operation.result.id,
        frontmatter_id=frontmatter_id,
        title=resolve_note_title(
            metadata,
            body,
            Path(operation.target),
            h1_pattern=_H1_PATTERN,
            empty_h1_falls_back=True,
        ),
        aliases=operation.result.aliases,
        tags=tuple(extract_tags(metadata, body)),
        calendar_date=_calendar_date(metadata),
        sha256=payload.sha256,
        size=len(payload.raw_bytes),
    )


def canonicalize_identity_sidecar_case_collisions(
    primary: Mapping[str, str],
    migrated: Mapping[str, str],
    *,
    live_frontmatter_ids: Mapping[str, str | None],
    live_aliases: Mapping[str, tuple[str, ...]],
    operation_paths: tuple[str, ...],
    operation_result_ids: tuple[str, ...],
    operation_result_aliases: tuple[str, ...],
) -> tuple[dict[str, str], tuple[IdentitySidecarCaseCanonicalization, ...]]:
    """Derive only mechanically provable obsolete case aliases.

    This helper is pure so crash recovery can re-derive and authenticate the
    exact same primary-sidecar after-image. Any ambiguous topology fails
    closed; callers must journal the returned mapping before publishing it.
    """
    if live_frontmatter_ids.keys() != live_aliases.keys():
        raise OrganizationManifestError(
            "identity_inventory_invalid",
            "Live identity evidence paths differ between frontmatter ids and aliases",
        )

    primary_groups: dict[str, list[tuple[str, str]]] = {}
    primary_id_counts: dict[str, int] = {}
    for rel_path, note_id in primary.items():
        primary_groups.setdefault(rel_path.casefold(), []).append((rel_path, note_id))
        normalized_id = note_id.casefold()
        primary_id_counts[normalized_id] = primary_id_counts.get(normalized_id, 0) + 1

    live_groups: dict[str, list[str]] = {}
    for rel_path in live_frontmatter_ids:
        live_groups.setdefault(rel_path.casefold(), []).append(rel_path)
    migrated_path_keys = {rel_path.casefold() for rel_path in migrated}
    migrated_ids = {note_id.casefold() for note_id in migrated.values()}
    frontmatter_ids = {
        note_id.casefold() for note_id in live_frontmatter_ids.values() if isinstance(note_id, str)
    }
    aliases = {
        alias.strip().casefold()
        for values in live_aliases.values()
        for alias in values
        if alias.strip()
    }
    aliases.update(alias.strip().casefold() for alias in operation_result_aliases if alias.strip())
    operation_path_keys = {rel_path.casefold() for rel_path in operation_paths}
    result_ids = {note_id.casefold() for note_id in operation_result_ids}

    updated = dict(primary)
    repairs: list[IdentitySidecarCaseCanonicalization] = []
    for path_key, entries in sorted(primary_groups.items()):
        if len(entries) == 1:
            continue
        if len(entries) != 2:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision does not contain exactly two keys: {path_key!r}",
            )
        if path_key in migrated_path_keys:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision is also claimed by migrated mappings: {path_key!r}",
            )
        physical_paths = live_groups.get(path_key, [])
        if len(physical_paths) != 1:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision does not resolve to one admitted note: {path_key!r}",
            )
        live_path = physical_paths[0]
        exact_entries = [entry for entry in entries if entry[0] == live_path]
        if len(exact_entries) != 1:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision has no unique exact-cased live key: {live_path!r}",
            )
        if live_frontmatter_ids[live_path] is not None:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision note has a frontmatter id: {live_path!r}",
            )
        if path_key in operation_path_keys:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar case collision intersects a manifest operation: {live_path!r}",
            )

        live_entry = exact_entries[0]
        stale_entry = next(entry for entry in entries if entry != live_entry)
        stale_path, stale_id = stale_entry
        normalized_stale_id = stale_id.casefold()
        if (
            primary_id_counts.get(normalized_stale_id) != 1
            or normalized_stale_id in migrated_ids
            or normalized_stale_id in frontmatter_ids
            or normalized_stale_id in aliases
            or stale_path.casefold() in aliases
            or normalized_stale_id in result_ids
        ):
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"Obsolete case-alias id is still claimed elsewhere: {stale_id!r}",
            )
        del updated[stale_path]
        repairs.append(
            IdentitySidecarCaseCanonicalization(
                stale_path=stale_path,
                stale_id=stale_id,
                live_path=live_entry[0],
                live_id=live_entry[1],
            )
        )

    return updated, tuple(repairs)


def _normalized_sidecar_mappings(
    mappings: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    normalized: dict[str, tuple[str, str]] = {}
    for path, note_id in mappings.items():
        key = _filesystem_path_key(PurePosixPath(path).as_posix())
        if key in normalized:
            raise OrganizationManifestError(
                "identity_inventory_invalid",
                f"ULID sidecar has a case-colliding path: {path!r}",
            )
        normalized[key] = (path, note_id)
    return normalized


def _derive_identity_sidecar_update(
    *,
    vault_root: Path,
    operations: tuple[ResolvedOrganizationOperation, ...],
    primary_bytes: bytes,
    primary: Mapping[str, str],
    migrated: Mapping[str, str],
    external_payload_bytes: int,
    primary_was_canonicalized: bool = False,
) -> tuple[Path, str | None, bytes | None, str | None]:
    """Derive one exact journaled after-image for canonicalization and moves."""
    updated = dict(primary)
    normalized_primary = _normalized_sidecar_mappings(primary)
    normalized_migrated = _normalized_sidecar_mappings(migrated)
    changed = primary_was_canonicalized
    for resolved in operations:
        operation = resolved.operation
        if not isinstance(operation, MoveReplaceExactOperation):
            continue
        source_key = _filesystem_path_key(PurePosixPath(operation.source).as_posix())
        target_key = _filesystem_path_key(PurePosixPath(operation.target).as_posix())
        if source_key in normalized_migrated:
            raise OrganizationManifestError(
                "sidecar_migrated_move_unsupported",
                f"move source is reserved by migrated ULID sidecar: {operation.source!r}",
            )
        source_mapping = normalized_primary.get(source_key)
        if source_mapping is None:
            continue
        source_path, mapped_id = source_mapping
        if mapped_id != operation.expected.id:
            raise OrganizationManifestError(
                "sidecar_identity_mismatch",
                f"move source sidecar identity differs from manifest: {operation.source!r}",
            )
        if target_key in normalized_primary or target_key in normalized_migrated:
            raise OrganizationManifestError(
                "sidecar_identity_conflict",
                f"move target already has a sidecar identity: {operation.target!r}",
            )
        del updated[source_path]
        updated[operation.target] = mapped_id
        normalized_primary.pop(source_key)
        normalized_primary[target_key] = (operation.target, mapped_id)
        changed = True
    sidecar_path = vault_root / ".datacron" / ULID_SIDECAR_FILENAME
    before_sha256 = sha256_bytes(primary_bytes) if primary_bytes else None
    if not changed:
        return sidecar_path, before_sha256, None, None
    if before_sha256 is None:
        raise OrganizationManifestError(
            "identity_inventory_invalid",
            "derived ULID sidecar migration has no exact baseline",
        )
    after_bytes = (json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    if len(after_bytes) > MAX_PAYLOAD_BYTES:
        raise OrganizationManifestError(
            "bundle_limit_exceeded",
            "derived ULID sidecar payload exceeds the per-payload limit",
        )
    if external_payload_bytes + len(after_bytes) > MAX_TOTAL_PAYLOAD_BYTES:
        raise OrganizationManifestError(
            "bundle_limit_exceeded",
            "external and derived payloads exceed the total payload limit",
        )
    return (
        sidecar_path,
        before_sha256,
        after_bytes,
        sha256_bytes(after_bytes),
    )


def _validate_sidecar_operation_identity(
    operation: OrganizationOperation,
    sidecar_ids: Mapping[str, str],
) -> None:
    """Prevent stale sidecar moves and vault-wide sidecar ID reuse."""
    normalized = _normalized_sidecar_mappings(sidecar_ids)
    target_key = _filesystem_path_key(PurePosixPath(operation.target).as_posix())
    if isinstance(operation, (CreateExactOperation, MoveReplaceExactOperation)) and (
        target_key in normalized
    ):
        raise OrganizationManifestError(
            "sidecar_identity_conflict",
            f"Exact-create target has a reserved sidecar identity: {operation.target!r}",
        )
    source_key = (
        _filesystem_path_key(PurePosixPath(operation.source).as_posix())
        if isinstance(operation, MoveReplaceExactOperation)
        else None
    )
    for mapped_key, (mapped_path, mapped_id) in normalized.items():
        if mapped_id.casefold() != operation.result.id.casefold():
            continue
        if isinstance(operation, ReplaceExactOperation) and mapped_key == target_key:
            continue
        if isinstance(operation, MoveReplaceExactOperation) and mapped_key == source_key:
            continue
        raise OrganizationManifestError(
            "result_id_collision",
            f"Projected note id {operation.result.id!r} is reserved by sidecar path "
            f"{mapped_path!r}",
        )


def _validate_projected_identities(
    live: Mapping[str, _ProjectedIdentity],
    operations: tuple[ResolvedOrganizationOperation, ...],
    sidecar_ids: Mapping[str, str],
) -> dict[str, _ProjectedIdentity]:
    projected = dict(live)
    result_items: list[_ProjectedIdentity] = []
    for resolved in operations:
        operation = resolved.operation
        _validate_sidecar_operation_identity(operation, sidecar_ids)
        normalized_target = normalize_vault_rel_path(operation.target)
        if isinstance(operation, (CreateExactOperation, MoveReplaceExactOperation)) and (
            normalized_target in projected
        ):
            raise OrganizationManifestError(
                "target_not_absent",
                f"Projected exact-create target collides with admitted note: {operation.target!r}",
            )
        if isinstance(operation, MoveReplaceExactOperation):
            projected.pop(normalize_vault_rel_path(operation.source), None)
        result = _identity_from_payload(operation, resolved.payload)
        projected[normalized_target] = result
        result_items.append(result)

    ids: dict[str, str] = {}
    for item in projected.values():
        normalized_id = item.note_id.casefold()
        previous_path = ids.get(normalized_id)
        if previous_path is not None and previous_path != item.rel_path:
            raise OrganizationManifestError(
                "result_id_collision",
                f"Projected note id {item.note_id!r} collides between "
                f"{previous_path!r} and {item.rel_path!r}",
            )
        ids[normalized_id] = item.rel_path

    live_items = tuple(live.values())
    live_alias_index = build_tiered_alias_index(
        live_items,
        identity=lambda item: item.note_id,
        title=lambda item: (item.title,),
        stem=lambda item: (item.stem,),
        aliases=lambda item: item.aliases,
        normalize=lambda value: value.strip().lower(),
    )
    projected_items = tuple(projected.values())
    alias_index = build_tiered_alias_index(
        projected_items,
        identity=lambda item: item.note_id,
        title=lambda item: (item.title,),
        stem=lambda item: (item.stem,),
        aliases=lambda item: item.aliases,
        normalize=lambda value: value.strip().lower(),
    )
    result_paths = {item.rel_path.casefold() for item in result_items}
    for item in projected_items:
        for alias in item.aliases:
            normalized_alias = alias.strip().lower()
            is_result = item.rel_path.casefold() in result_paths
            owned_before = live_alias_index.get(normalized_alias) == item.note_id
            if not is_result and not owned_before:
                continue
            if alias_index.get(normalized_alias) != item.note_id:
                raise OrganizationManifestError(
                    "result_alias_unresolved",
                    f"Projected alias {alias!r} does not resolve to {item.note_id} "
                    f"for {item.rel_path!r}",
                )
    return projected


def _scope_note_rows(
    identities: Mapping[str, _ProjectedIdentity],
    scope_rel_paths: set[str],
) -> list[dict[str, object]]:
    prefixes = tuple(
        f"{_filesystem_path_key(scope_path.rstrip('/'))}/" for scope_path in scope_rel_paths
    )
    selected = [
        item
        for item in identities.values()
        if _filesystem_path_key(item.rel_path).startswith(prefixes)
    ]
    return [
        {
            "aliases": list(item.aliases),
            "note_id": item.note_id,
            "rel_path": item.rel_path,
            "sha256": item.sha256,
            "size": item.size,
            "title": item.title,
        }
        for item in sorted(selected, key=lambda item: item.rel_path.casefold())
    ]


def _validate_config_precondition(
    bundle: OrganizationBundle,
    *,
    vault_root: Path,
    scope: VaultScope,
) -> tuple[
    Path | None,
    OrganizationPayload | None,
    str,
    set[str],
    dict[str, object] | None,
    VaultConfig,
]:
    live_path = _resolve_scoped_path(
        vault_root,
        _VAULT_CONFIG_REL_PATH,
        scope,
        access="read",
        allow_missing=False,
    )
    live_bytes, live_document, live_config = _read_live_config(live_path)
    before_sha256 = sha256_bytes(live_bytes)
    organization_scope = _active_organization_scope(live_config, label="live VAULT.yaml")
    config = bundle.manifest.config
    if config is None:
        _validate_organization_topology(
            vault_root,
            scope,
            live_config,
            organization_scope,
        )
        return None, None, before_sha256, {organization_scope}, None, live_config
    if config.expected_sha256 != before_sha256:
        raise OrganizationManifestError(
            "source_hash_mismatch",
            "VAULT.yaml hash does not match config expected_sha256",
        )
    payload = bundle.get_payload(config.payload_sha256, _YAML_SUFFIX)
    target_document, target_config = parse_organization_config_document(
        payload.text,
        label=f"VAULT.yaml payload {payload.path.name}",
    )
    _compare_config_scope(live_document, target_document)
    target_scope = _active_organization_scope(target_config, label="target VAULT.yaml")
    if _filesystem_path_key(target_scope) != _filesystem_path_key(organization_scope):
        raise OrganizationManifestError(
            "organization_scope_change_unsupported",
            "organization-apply-v1 does not permit organization.scope changes",
        )
    _validate_organization_topology(vault_root, scope, target_config, target_scope)
    precondition: dict[str, object] = {
        "after_sha256": payload.sha256,
        "before_sha256": config.expected_sha256,
        "kind": "config_replace_exact",
        "target": config.target,
    }
    return live_path, payload, before_sha256, {organization_scope}, precondition, target_config


def _validate_organization_topology(
    vault_root: Path,
    scope: VaultScope,
    config: VaultConfig,
    organization_scope: str,
) -> None:
    scope_path = _resolve_scoped_path(
        vault_root,
        organization_scope,
        scope,
        access="read",
        allow_missing=False,
    )
    if not scope_path.is_dir():
        raise OrganizationManifestError(
            "organization_scope_path_invalid",
            f"organization.scope is not an existing directory: {organization_scope!r}",
        )
    scope_prefix = f"{_filesystem_path_key(organization_scope.rstrip('/'))}/"
    organization = config.organization
    for rule in () if organization is None else organization.rules:
        try:
            _validate_organization_directory_rel_path(
                rule.folder,
                label=f"organization rule folder {rule.folder!r}",
            )
        except ValueError as exc:
            raise OrganizationManifestError(
                "organization_rule_path_invalid",
                str(exc),
            ) from exc
        folder_key = _filesystem_path_key(PurePosixPath(rule.folder).as_posix())
        if folder_key != _filesystem_path_key(organization_scope) and not folder_key.startswith(
            scope_prefix
        ):
            raise OrganizationManifestError(
                "organization_rule_outside_scope",
                f"Organization rule folder is outside organization.scope: {rule.folder!r}",
            )
        folder_path = _resolve_scoped_path(
            vault_root,
            rule.folder,
            scope,
            access="read",
            allow_missing=True,
        )
        if folder_path.exists() and not folder_path.is_dir():
            raise OrganizationManifestError(
                "organization_rule_path_invalid",
                f"Organization rule folder is an existing non-directory: {rule.folder!r}",
            )


def _path_belongs_to_organization_scope(rel_path: str, organization_scope: str) -> bool:
    normalized_path = _filesystem_path_key(PurePosixPath(rel_path).as_posix())
    normalized_scope = _filesystem_path_key(
        PurePosixPath(organization_scope).as_posix().rstrip("/")
    )
    return normalized_path.startswith(f"{normalized_scope}/")


def _assert_operation_organization_scope(
    operation: OrganizationOperation,
    organization_scope: str,
) -> None:
    if not _path_belongs_to_organization_scope(operation.target, organization_scope):
        raise OrganizationManifestError(
            "operation_outside_organization_scope",
            f"Operation target is outside organization.scope: {operation.target!r}",
        )
    if isinstance(operation, MoveReplaceExactOperation) and not _path_belongs_to_organization_scope(
        operation.source,
        organization_scope,
    ):
        raise OrganizationManifestError(
            "operation_outside_organization_scope",
            f"Move source is outside organization.scope: {operation.source!r}",
        )


def _canonicalize_and_project_live_identities(
    live_identities: Mapping[str, _ProjectedIdentity],
    operations: tuple[ResolvedOrganizationOperation, ...],
    primary: Mapping[str, str],
    migrated: Mapping[str, str],
) -> tuple[
    dict[str, str],
    tuple[IdentitySidecarCaseCanonicalization, ...],
    dict[str, _ProjectedIdentity],
]:
    operation_paths = tuple(
        rel_path
        for resolved in operations
        for rel_path in (
            resolved.operation.target,
            *(
                (resolved.operation.source,)
                if isinstance(resolved.operation, MoveReplaceExactOperation)
                else ()
            ),
        )
    )
    canonical_primary, canonicalizations = canonicalize_identity_sidecar_case_collisions(
        primary,
        migrated,
        live_frontmatter_ids={
            item.rel_path: item.frontmatter_id for item in live_identities.values()
        },
        live_aliases={item.rel_path: item.aliases for item in live_identities.values()},
        operation_paths=operation_paths,
        operation_result_ids=tuple(resolved.operation.result.id for resolved in operations),
        operation_result_aliases=tuple(
            alias for resolved in operations for alias in resolved.operation.result.aliases
        ),
    )
    canonical_merged = dict(canonical_primary)
    canonical_merged.update(migrated)
    projected = _validate_projected_identities(live_identities, operations, canonical_merged)
    return canonical_primary, canonicalizations, projected


def validate_organization_bundle(
    bundle: OrganizationBundle,
    *,
    vault_root: Path,
    scope: VaultScope,
) -> ValidatedOrganizationBundle:
    """Bind an authenticated bundle to one exact live vault state without writes.

    Args:
        bundle: Result of :func:`load_organization_bundle`.
        vault_root: Live vault root.
        scope: Canonical served scope used to authorize every read and write.

    Returns:
        Resolved operations and a deterministic digest of all live
        preconditions.

    Raises:
        OrganizationManifestError: If any source, target, hash, identity,
            scope, link, or configuration precondition differs.
    """
    resolved_vault = _resolve_plain_vault_root(vault_root)
    if _is_within(bundle.bundle_root, resolved_vault) or _is_within(
        resolved_vault,
        bundle.bundle_root,
    ):
        raise OrganizationManifestError(
            "bundle_path_invalid",
            "organization bundle and validation vault must not overlap",
        )
    resolved_operations: list[ResolvedOrganizationOperation] = []
    preconditions: list[dict[str, object]] = []
    (
        config_path,
        config_payload,
        config_before_sha256,
        scope_rel_paths,
        config_precondition,
        target_config,
    ) = _validate_config_precondition(bundle, vault_root=resolved_vault, scope=scope)
    if config_precondition is not None:
        preconditions.append(config_precondition)
    organization_scope = next(iter(scope_rel_paths))

    for operation in bundle.manifest.operations:
        _assert_operation_organization_scope(operation, organization_scope)
        payload = bundle.get_payload(operation.payload_sha256, _MARKDOWN_SUFFIX)
        target_path = _resolve_scoped_path(
            resolved_vault,
            operation.target,
            scope,
            access="write",
            allow_missing=isinstance(operation, (CreateExactOperation, MoveReplaceExactOperation)),
        )
        _assert_target_note_admitted(scope, operation.target)
        if isinstance(operation, CreateExactOperation):
            _assert_absent_case_insensitive(target_path)
            source_path = None
            preconditions.append(
                {
                    "after_sha256": payload.sha256,
                    "kind": operation.kind,
                    "result": operation.result.model_dump(mode="json"),
                    "target": operation.target,
                    "target_state": "absent",
                }
            )
        elif isinstance(operation, ReplaceExactOperation):
            try:
                admitted = scope.authorize_note_rel_path(operation.target)
            except (NoteAdmissionError, PathConfinementError, RuntimeError) as exc:
                raise OrganizationManifestError(
                    "source_not_admitted",
                    f"Replace source is not an admitted note: {operation.target!r}: {exc}",
                ) from exc
            if admitted != target_path:
                raise OrganizationManifestError(
                    "vault_path_invalid",
                    f"Scope resolved two paths for replace target {operation.target!r}",
                )
            _read_expected_note(
                target_path,
                expected_sha256=operation.expected_sha256,
                expected_identity=operation.expected,
            )
            source_path = target_path
            preconditions.append(
                {
                    "after_sha256": payload.sha256,
                    "before_sha256": operation.expected_sha256,
                    "expected": operation.expected.model_dump(mode="json"),
                    "kind": operation.kind,
                    "result": operation.result.model_dump(mode="json"),
                    "target": operation.target,
                }
            )
        else:
            source_path = _resolve_scoped_path(
                resolved_vault,
                operation.source,
                scope,
                access="write",
                allow_missing=False,
            )
            try:
                admitted = scope.authorize_note_rel_path(operation.source)
            except (NoteAdmissionError, PathConfinementError, RuntimeError) as exc:
                raise OrganizationManifestError(
                    "source_not_admitted",
                    f"Move source is not an admitted note: {operation.source!r}: {exc}",
                ) from exc
            if admitted != source_path:
                raise OrganizationManifestError(
                    "vault_path_invalid",
                    f"Scope resolved two paths for move source {operation.source!r}",
                )
            _read_expected_note(
                source_path,
                expected_sha256=operation.expected_sha256,
                expected_identity=operation.expected,
            )
            _assert_absent_case_insensitive(target_path)
            preconditions.append(
                {
                    "after_sha256": payload.sha256,
                    "before_sha256": operation.expected_sha256,
                    "expected": operation.expected.model_dump(mode="json"),
                    "kind": operation.kind,
                    "result": operation.result.model_dump(mode="json"),
                    "source": operation.source,
                    "target": operation.target,
                    "target_state": "absent",
                }
            )
        resolved_operations.append(
            ResolvedOrganizationOperation(
                operation=operation,
                source_path=source_path,
                target_path=target_path,
                payload=payload,
            )
        )

    (
        primary_bytes,
        primary_ids,
        migrated_bytes,
        migrated_ids,
        sidecar_ids,
    ) = _load_vault_id_state(resolved_vault)
    live_identities = _inventory_admitted_identities(
        resolved_vault,
        scope,
        sidecar_ids,
    )
    canonical_primary_ids, case_canonicalizations, projected_identities = (
        _canonicalize_and_project_live_identities(
            live_identities,
            tuple(resolved_operations),
            primary_ids,
            migrated_ids,
        )
    )
    (
        identity_sidecar_path,
        identity_sidecar_before_sha256,
        identity_sidecar_after_bytes,
        identity_sidecar_after_sha256,
    ) = _derive_identity_sidecar_update(
        vault_root=resolved_vault,
        operations=tuple(resolved_operations),
        primary_bytes=primary_bytes,
        primary=canonical_primary_ids,
        migrated=migrated_ids,
        external_payload_bytes=bundle.total_payload_bytes,
        primary_was_canonicalized=bool(case_canonicalizations),
    )
    if identity_sidecar_after_sha256 is not None:
        preconditions.append(
            {
                "after_sha256": identity_sidecar_after_sha256,
                "before_sha256": identity_sidecar_before_sha256,
                "kind": "identity_sidecar_replace_exact",
                "target": f".datacron/{ULID_SIDECAR_FILENAME}",
            }
        )
    scope_notes = _scope_note_rows(live_identities, scope_rel_paths)
    scope_note_preconditions = tuple(
        OrganizationScopeNotePrecondition(
            rel_path=str(row["rel_path"]),
            sha256=str(row["sha256"]),
        )
        for row in scope_notes
    )
    projected_notes = tuple(
        OrganizationNoteSnapshot(
            rel_path=item.rel_path,
            size_bytes=item.size,
            tags=item.tags,
            calendar_date=item.calendar_date,
        )
        for item in sorted(projected_identities.values(), key=lambda value: value.rel_path)
        if _path_belongs_to_organization_scope(item.rel_path, organization_scope)
    )
    scope_document = {
        "config_before_sha256": config_before_sha256,
        "identity_sidecars": {
            "case_canonicalization_count": len(case_canonicalizations),
            "case_canonicalization_sha256": (
                hash_identity_sidecar_case_canonicalizations(case_canonicalizations)
            ),
            "primary_before_sha256": identity_sidecar_before_sha256,
            "migrated_before_sha256": (sha256_bytes(migrated_bytes) if migrated_bytes else None),
        },
        "manifest_sha256": bundle.manifest_sha256,
        "notes": scope_notes,
        "preconditions": preconditions,
        "schema": "organization-scope-v1",
        "scopes": sorted(scope_rel_paths, key=str.casefold),
    }
    return ValidatedOrganizationBundle(
        manifest_path=bundle.manifest_path,
        bundle_root=bundle.bundle_root,
        manifest_bytes=bundle.manifest_bytes,
        manifest_sha256=bundle.manifest_sha256,
        payload_set_sha256=bundle.payload_set_sha256,
        manifest=bundle.manifest,
        payloads=bundle.payloads,
        total_payload_bytes=bundle.total_payload_bytes,
        operations=tuple(resolved_operations),
        config_path=config_path,
        config_payload=config_payload,
        config_before_sha256=config_before_sha256,
        target_config=target_config,
        projected_notes=projected_notes,
        identity_sidecar_path=identity_sidecar_path,
        identity_sidecar_before_sha256=identity_sidecar_before_sha256,
        identity_sidecar_after_bytes=identity_sidecar_after_bytes,
        identity_sidecar_after_sha256=identity_sidecar_after_sha256,
        migrated_identity_sidecar_path=(
            resolved_vault / ".datacron" / MIGRATED_ULID_SIDECAR_FILENAME
        ),
        migrated_identity_sidecar_before_sha256=(
            sha256_bytes(migrated_bytes) if migrated_bytes else None
        ),
        scope_note_preconditions=scope_note_preconditions,
        scope_note_count=len(scope_notes),
        scope_digest=sha256_bytes(_canonical_json_bytes(scope_document)),
        identity_sidecar_case_canonicalizations=case_canonicalizations,
    )


def load_and_validate_organization_bundle(
    manifest_path: Path,
    *,
    vault_root: Path,
    scope: VaultScope,
) -> ValidatedOrganizationBundle:
    """Load an external bundle and bind it to the exact live vault state."""
    bundle = load_organization_bundle(manifest_path, vault_root=vault_root)
    return validate_organization_bundle(bundle, vault_root=vault_root, scope=scope)
