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
"""Runtime configuration loaded from environment variables and ``.env``.

Reserved runtime keys must use the ``DATACRON_`` prefix. The
:func:`get_settings` accessor returns a cached singleton; tests may override it
via :func:`reset_settings_cache`.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from datacron.core.memory_protocol import (
    SESSION_DEFAULT_PATHS,
    SESSION_MAX_NOTES,
    SESSION_NOTE_CHARS,
)
from datacron.core.query_expansion import default_query_expansion, normalize_term_map

DEFAULT_LOG_LEVEL: Final[str] = "INFO"
DEFAULT_LOG_DIR: Final[Path] = Path.home() / ".datacron" / "logs"
DEFAULT_MAX_RESULT_TOKENS: Final[int] = 8000
DEFAULT_MAX_RESULT_COUNT: Final[int] = 20
DEFAULT_REPAIR_MIN_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_EVAL_REGRESSION_TOLERANCE: Final[float] = 0.02
DEFAULT_CONTRADICTION_MAX_PAIRS: Final[int] = 256
DEFAULT_CONTRADICTION_MAX_CANDIDATES: Final[int] = 20
DEFAULT_CONTRADICTION_MAX_PER_NOTE_PAIR: Final[int] = 2
DEFAULT_CONTRADICTION_SUMMARY_EVIDENCE_CHARS: Final[int] = 160
DEFAULT_CONTRADICTION_PROVENANCE_LABELS: Final[dict[str, str]] = {
    "contradiction": "CORRECTION",
    "refinement": "MISE A JOUR",
    "open_question": "QUESTION OUVERTE",
}
DEFAULT_CONTRADICTION_SOURCE_CONNECTOR: Final[str] = "Voir"
TOKEN_ESTIMATE_CHARS_PER_TOKEN: Final[int] = 4
TEMPORAL_OVERFETCH_FACTOR: Final[int] = 3
# BM25 column weights: the chunk body and its context (note title plus heading trail).
SEARCH_CONTENT_WEIGHT: Final[float] = 1.0
SEARCH_CONTEXT_WEIGHT: Final[float] = 3.0
CHUNK_CONTEXT_SEPARATOR: Final[str] = " / "
SUPERSEDED_DEMOTION_FACTOR: Final[float] = 0.1
CONFIDENCE_PENALTY: Final[dict[str, float]] = {"low": 0.7, "needs_verification": 0.5}
DEFAULT_RIPGREP_PATH: Final[str] = "rg"
DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH: Final[int] = 512
# Budget for one complete indexed fallback scan. The scan streams the index and
# stops at the first `limit` matches, so this ceiling is only reached by a query
# that matches nothing: measured at 1.7s over 94589 chunks, warm cache. The
# headroom covers a cold cache, a narrower scope, and vault growth, while still
# bounding a pathological pattern.
DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS: Final[float] = 10.0
# Chunks handed to one worker-thread regex batch. The deadline is only observed
# between batches, so this trades deadline precision against thread hand-off
# cost. Internal tuning: deliberately not a Settings field.
REGEX_FALLBACK_SCAN_BATCH_CHUNKS: Final[int] = 512
DEFAULT_REGEX_MAX_FRAME_BYTES: Final[int] = 8 * 1024 * 1024
REGEX_STREAM_READ_BYTES: Final[int] = 64 * 1024
# Bounded wait for a contended vault advisory lock (and the sidecar index
# busy-wait) before giving up. Mirrors the historical 5s SQLite busy timeout so
# a single source of truth governs "how long to wait on a busy vault resource".
DEFAULT_VAULT_LOCK_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_CHUNK_MAX_TOKENS: Final[int] = 1024
# get_note(full) budget, decoupled from the search budget (max_result_tokens).
# Search returns many snippets and must stay bounded; reading one note can be
# generous, so a single get_note returns most notes whole while pagination
# (offset/limit/next_offset) remains the safety valve for pathologically large notes.
DEFAULT_GET_NOTE_MAX_TOKENS: Final[int] = 25000
DEFAULT_HISTORY_RETENTION_DAYS: Final[int] = 30
DEFAULT_HISTORY_MODE: Final[str] = "full"
# Canonical text encoding and line-ending convention for the sidecar and the
# ``VAULT.yaml`` ``encoding``/``line_endings`` fields. Single source of truth so
# the writer (bootstrap) and the model default cannot drift apart.
DEFAULT_ENCODING: Final[str] = "utf-8"
DEFAULT_LINE_ENDINGS: Final[str] = "lf"
DEFAULT_REDACT_SECRETS: Final[str] = "all"
DEFAULT_DURABILITY_MODE: Final[str] = "best-effort"
DEFAULT_TOOL_DESCRIPTION_PROFILE: Final[str] = "standard"
DEFAULT_SCRUB_NOTES_PER_SECOND: Final[float] = 50.0
DEFAULT_SCRUB_MEBIBYTES_PER_SECOND: Final[float] = 16.0
DEFAULT_SCRUB_MAX_DURATION_SECONDS: Final[float] = 30.0
DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES: Final[int] = 25
DEFAULT_SCRUB_CHECKPOINT_PATH: Final[Path] = Path(".datacron/scrubber/checkpoint.json")
DEFAULT_SCRUB_CANARY_DIR: Final[Path] = Path(".datacron/scrubber/canaries")
DEFAULT_SCRUB_CANARIES: Final[tuple[tuple[str, str], ...]] = (
    (
        "exact-byte-lf.md",
        "# Datacron integrity canary\n\nformat: utf-8-lf\nsequence: 0123456789abcdef\n",
    ),
    (
        "exact-byte-crlf.md",
        "# Datacron integrity canary\r\n\r\nformat: utf-8-crlf\r\nsequence: fedcba9876543210\r\n",
    ),
)
DEFAULT_EXCLUDED_FOLDERS: Final[tuple[str, ...]] = (
    "_attachments",
    "_trash",
    "_archive",
)
DEFAULT_EXCLUDED_FILES: Final[tuple[str, ...]] = ()

SIDECAR_DIR_NAME: Final[str] = ".datacron"
INDEX_DIR_NAME: Final[str] = "index"
INDEX_DB_FILENAME: Final[str] = "datacron.db"
HISTORY_DIR_NAME: Final[str] = "history"
OPLOG_DIR_NAME: Final[str] = "oplog"
OPLOG_PENDING_DIR_NAME: Final[str] = "pending"
VAULT_CONFIG_FILENAME: Final[str] = "VAULT.yaml"
# Key under which the writing Datacron build (package version) is stamped, both
# in ``VAULT.yaml`` (provenance) and the ``vault/info`` MCP resource. Must match
# the ``VaultConfig.datacron_version`` field name.
VAULT_VERSION_KEY: Final[str] = "datacron_version"
LOG_FILENAME_PATTERN: Final[str] = "datacron_{date}.log"
LOG_FORMAT: Final[str] = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
VALID_SECRET_REDACTION_POLICIES: Final[frozenset[str]] = frozenset(
    {"off", "log", "retrieval", "all"}
)
VALID_DURABILITY_MODES: Final[frozenset[str]] = frozenset({"strict", "best-effort"})
VALID_TOOL_DESCRIPTION_PROFILES: Final[frozenset[str]] = frozenset({"standard", "compact"})


DEFAULT_ORGANIZATION_NAMING: Final[str] = "{slug}"
VALID_NAMING_TOKENS: Final[frozenset[str]] = frozenset({"date", "iso_date", "slug"})
_NAMING_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{([^{}]*)\}")
_ISO_DATE_NAMING_NAME: Final[str] = "iso_date"
_ISO_DATE_NAMING_PLACEHOLDER: Final[str] = "{iso_date}"


@final
class OrganizationRule(BaseModel):
    """One declarative placement rule, matched on a single frontmatter tag.

    Rules are ordered and the order is meaningful: the first rule whose ``tag``
    appears on a note wins, and resolution stops there. Notes routinely carry
    several tags at once, so declaration order is the tie-breaker -- and it is
    one the vault owner controls by reordering the list, without reading code.

    ``extra="forbid"`` is deliberate here, against the tolerant ``extra="ignore"``
    used by :class:`VaultConfig`. A misspelled key in a rule would otherwise be
    dropped in silence and leave the rule matching nothing, which is far worse
    than a loud failure at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str
    folder: str
    naming: str = DEFAULT_ORGANIZATION_NAMING
    max_kb: int | None = Field(default=None, gt=0)

    @field_validator("tag", mode="before")
    @classmethod
    def _normalize_tag(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("organization rule tag must be a string")
        tag = value.strip()
        if not tag:
            raise ValueError("organization rule tag must not be empty")
        return tag

    @field_validator("folder", mode="before")
    @classmethod
    def _normalize_folder(cls, value: object) -> str:
        # Absoluteness must be judged before the separators are trimmed, or a
        # leading slash would be silently normalized into a relative path.
        if not isinstance(value, str):
            raise ValueError("organization rule folder must be a string")
        folder = value.strip().replace("\\", "/")
        if not folder.strip("/"):
            raise ValueError("organization rule folder must not be empty")
        if folder.startswith("/") or ":" in folder:
            raise ValueError(f"organization rule folder must be vault-relative; got {folder!r}")
        trimmed = folder.strip("/")
        if any(segment in {"..", "."} for segment in trimmed.split("/")):
            raise ValueError(f"organization rule folder must not traverse directories: {folder!r}")
        return trimmed

    @field_validator("naming", mode="before")
    @classmethod
    def _normalize_naming(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("organization rule naming must be a string")
        naming = value.strip()
        if not naming:
            raise ValueError("organization rule naming must not be empty")
        token_occurrences = _NAMING_TOKEN_PATTERN.findall(naming)
        tokens = set(token_occurrences)
        unknown = sorted(tokens - VALID_NAMING_TOKENS)
        if unknown:
            allowed = ", ".join(sorted(VALID_NAMING_TOKENS))
            raise ValueError(
                f"organization rule naming uses unknown token(s) {unknown}; allowed: {allowed}"
            )
        iso_date_count = token_occurrences.count(_ISO_DATE_NAMING_NAME)
        if iso_date_count and (
            iso_date_count != 1 or not naming.startswith(_ISO_DATE_NAMING_PLACEHOLDER)
        ):
            raise ValueError(
                "organization rule naming token '{iso_date}' must appear exactly once at the start"
            )
        return naming


def _normalize_tag_value(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    tag = value.strip()
    if not tag:
        raise ValueError(f"{what} must not be empty")
    if any(character.isspace() for character in tag):
        raise ValueError(f"{what} must not contain whitespace; got {tag!r}")
    return tag


def _normalize_namespace_value(value: object, *, what: str) -> str:
    namespace = _normalize_tag_value(value, what=what)
    if "/" in namespace:
        raise ValueError(f"{what} is a namespace and must not contain '/'; got {namespace!r}")
    return namespace


@final
class OrganizationSubject(BaseModel):
    """One registered subject: its canonical tag and the spellings it replaces.

    A note that carries an alias instead of the canonical tag is reported and
    refused, so a registry entry is also the place where old names keep
    resolving to the subject that owns them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str
    aliases: tuple[str, ...] = ()

    @field_validator("tag", mode="before")
    @classmethod
    def _normalize_tag(cls, value: object) -> str:
        return _normalize_tag_value(value, what="organization subject tag")

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalize_aliases(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("organization subject aliases must be a list of strings")
        aliases = tuple(
            _normalize_tag_value(item, what="organization subject alias") for item in value
        )
        seen: set[str] = set()
        for alias in aliases:
            folded = alias.casefold()
            if folded in seen:
                raise ValueError(f"duplicate organization subject alias {alias!r}")
            seen.add(folded)
        return aliases

    @model_validator(mode="after")
    def _alias_differs_from_tag(self) -> OrganizationSubject:
        if any(alias.casefold() == self.tag.casefold() for alias in self.aliases):
            raise ValueError(f"organization subject {self.tag!r} lists itself as an alias")
        return self


@final
class OrganizationTagPolicy(BaseModel):
    """Vault-declared tag policy enforced at write time and measured by the planner.

    Every name here comes from the vault: the namespace that carries the
    placement tag, the placement tags that may double as transversal markers,
    the namespace that names a subject, the closed subject registry, the
    placement tags whose notes may carry several subjects, and the other
    namespaces admitted. Datacron ships no taxonomy and refuses unknown keys.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    placement_namespace: str
    markers: tuple[str, ...] = ()
    subject_namespace: str | None = None
    subjects: tuple[OrganizationSubject, ...] = ()
    subject_exempt_tags: tuple[str, ...] = ()
    allowed_namespaces: tuple[str, ...] = ()

    @field_validator("placement_namespace", mode="before")
    @classmethod
    def _normalize_placement_namespace(cls, value: object) -> str:
        return _normalize_namespace_value(value, what="organization tags placement_namespace")

    @field_validator("subject_namespace", mode="before")
    @classmethod
    def _normalize_subject_namespace(cls, value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return _normalize_namespace_value(value, what="organization tags subject_namespace")

    @field_validator("markers", "subject_exempt_tags", mode="before")
    @classmethod
    def _normalize_tag_lists(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("organization tags lists must be lists of strings")
        return tuple(_normalize_tag_value(item, what="organization tags entry") for item in value)

    @field_validator("allowed_namespaces", mode="before")
    @classmethod
    def _normalize_allowed_namespaces(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("organization tags allowed_namespaces must be a list of strings")
        return tuple(
            _normalize_namespace_value(item, what="organization tags allowed namespace")
            for item in value
        )

    @field_validator("subjects", mode="before")
    @classmethod
    def _coerce_subjects(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("organization tags subjects must be a list")
        return tuple({"tag": item} if isinstance(item, str) else item for item in value)

    @model_validator(mode="after")
    def _check_registry(self) -> OrganizationTagPolicy:
        if self.subjects and self.subject_namespace is None:
            raise ValueError(
                "organization tags subject_namespace is required when subjects are declared"
            )
        reserved = {self.placement_namespace.casefold()}
        if self.subject_namespace is not None:
            if self.subject_namespace.casefold() in reserved:
                raise ValueError(
                    "organization tags subject_namespace must differ from placement_namespace"
                )
            reserved.add(self.subject_namespace.casefold())
        for namespace in self.allowed_namespaces:
            if namespace.casefold() in reserved:
                raise ValueError(
                    f"organization tags allowed namespace {namespace!r} duplicates a "
                    "declared namespace"
                )
        for marker in self.markers:
            if not marker.casefold().startswith(self.placement_namespace.casefold() + "/"):
                raise ValueError(
                    f"organization tags marker {marker!r} must live in the placement namespace"
                )
        seen: dict[str, str] = {}
        for subject in self.subjects:
            if self.subject_namespace is not None and not subject.tag.casefold().startswith(
                self.subject_namespace.casefold() + "/"
            ):
                raise ValueError(
                    f"organization subject {subject.tag!r} must live in the subject namespace"
                )
            for name in (subject.tag, *subject.aliases):
                folded = name.casefold()
                previous = seen.get(folded)
                if previous is not None:
                    raise ValueError(
                        f"organization subject name {name!r} is declared twice "
                        f"({previous} and {subject.tag})"
                    )
                seen[folded] = subject.tag
        return self


@final
class OrganizationConfig(BaseModel):
    """Declarative organization policy for a vault.

    Datacron knows the *shape* of a rule and nothing else. Folder names and tag
    names come from the vault's own sidecar, never from this package, so a vault
    with a different taxonomy is served identically.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str | None = None
    rules: tuple[OrganizationRule, ...] = ()
    tags: OrganizationTagPolicy | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("organization scope must be a string")
        if value.strip() == "":
            return None
        scope = value.strip().replace("\\", "/")
        if scope.startswith("/") or ":" in scope:
            raise ValueError(f"organization scope must be vault-relative; got {scope!r}")
        trimmed = scope.strip("/")
        if not trimmed or any(segment in {"..", "."} for segment in trimmed.split("/")):
            raise ValueError(f"organization scope must not traverse directories: {scope!r}")
        return trimmed

    @field_validator("rules", mode="after")
    @classmethod
    def _reject_duplicate_tags(
        cls, value: tuple[OrganizationRule, ...]
    ) -> tuple[OrganizationRule, ...]:
        seen: set[str] = set()
        for rule in value:
            if rule.tag in seen:
                raise ValueError(f"duplicate organization rule for tag {rule.tag!r}")
            seen.add(rule.tag)
        return value

    @model_validator(mode="after")
    def _require_scope_for_active_rules(self) -> OrganizationConfig:
        if self.rules and self.scope is None:
            raise ValueError("organization scope is required when rules are declared")
        if self.tags is not None:
            _validate_tag_policy_against_rules(self.rules, self.tags)
        return self


def _validate_tag_policy_against_rules(
    rules: tuple[OrganizationRule, ...],
    tags: OrganizationTagPolicy,
) -> None:
    """Every name the policy relies on must exist among the rules, spelled the same way.

    A rule is keyed either by a placement tag (inside ``placement_namespace``) or
    by a registered subject tag: a subject rule places the notes a subject owns
    in the subject's own folder, while the note keeps carrying exactly one
    placement tag that says what it is. Markers and exemptions name placement
    rules only; a subject rule never counts as a placement tag.
    """
    if not rules:
        raise ValueError("organization tags policy requires at least one rule")
    rule_tags = {rule.tag.casefold() for rule in rules}
    prefix = tags.placement_namespace.casefold() + "/"
    subject_tags = {subject.tag.casefold() for subject in tags.subjects}
    placement_rule_tags: set[str] = set()
    for rule in rules:
        if rule.tag != rule.tag.lower():
            # The rule resolver compares tags exactly while the policy compares
            # them lowercased; lowercase rules keep both in step.
            raise ValueError(
                f"organization rule tag {rule.tag!r} must be lowercase when a tags "
                "policy is declared"
            )
        folded = rule.tag.casefold()
        if folded.startswith(prefix):
            placement_rule_tags.add(folded)
        elif folded not in subject_tags:
            raise ValueError(
                f"organization rule tag {rule.tag!r} is outside the placement namespace "
                f"{tags.placement_namespace!r} declared by the tags policy and is not a "
                "declared subject"
            )
    if not placement_rule_tags:
        raise ValueError("organization tags policy requires at least one placement rule")
    aliases = [alias for subject in tags.subjects for alias in subject.aliases]
    for alias in aliases:
        if alias.casefold() in rule_tags:
            raise ValueError(
                f"organization subject alias {alias!r} collides with a declared rule tag"
            )
    for label, names in (
        ("marker", tags.markers),
        ("subject_exempt_tags entry", tags.subject_exempt_tags),
    ):
        for name in names:
            if name.casefold() not in placement_rule_tags:
                raise ValueError(
                    f"organization tags {label} {name!r} must be a declared placement rule tag"
                )


class VaultConfig(BaseModel):
    """Typed model for ``.datacron/VAULT.yaml``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    # Provenance stamp: the Datacron build (package Calendar Version) that wrote
    # the sidecar. NOT a format-compatibility gate -- Datacron reads any vault
    # without a version check (see SPEC section 10). A future incompatible
    # on-disk format change must introduce a dedicated field, never overload this.
    datacron_version: str | None = None
    vault_id: str | None = None
    created: str | None = None
    encoding: str = DEFAULT_ENCODING
    line_endings: str = DEFAULT_LINE_ENDINGS
    history_retention_days: int = Field(default=DEFAULT_HISTORY_RETENTION_DAYS, ge=1)
    history_mode: str = DEFAULT_HISTORY_MODE
    folders: dict[str, str] = Field(default_factory=dict)
    excluded_folders: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FOLDERS))
    excluded_files: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_FILES))
    query_expansion: dict[str, list[str]] = Field(default_factory=default_query_expansion)
    # Absent block means the feature is inert: every existing vault keeps its
    # exact current behaviour, which is the non-regression guarantee for vaults
    # already published against earlier releases.
    organization: OrganizationConfig | None = None

    @field_validator("organization", mode="before")
    @classmethod
    def _normalize_organization(cls, value: object) -> object:
        # An absent or empty mapping means no policy. Other false-like YAML
        # values are type errors, not a silent way to disable organization.
        if value is None or value == {}:
            return None
        return value

    @field_validator("line_endings", mode="before")
    @classmethod
    def _normalize_line_endings(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"lf", "crlf"}:
            raise ValueError("line_endings must be 'lf' or 'crlf'")
        return normalized

    @field_validator("history_mode", mode="before")
    @classmethod
    def _normalize_history_mode(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"full", "redacted"}:
            raise ValueError("history_mode must be 'full' or 'redacted'")
        return normalized

    @field_validator("excluded_folders", mode="before")
    @classmethod
    def _normalize_excluded_folders(cls, value: object) -> list[str]:
        if value is None or value == "":
            return list(DEFAULT_EXCLUDED_FOLDERS)
        if not isinstance(value, list):
            raise TypeError("excluded_folders must be a list of folder names")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("excluded_files", mode="before")
    @classmethod
    def _normalize_excluded_files(cls, value: object) -> list[str]:
        if value is None or value == "":
            return list(DEFAULT_EXCLUDED_FILES)
        if not isinstance(value, list):
            raise TypeError("excluded_files must be a list of file names")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("query_expansion", mode="before")
    @classmethod
    def _normalize_query_expansion(cls, value: object) -> dict[str, list[str]]:
        if value is None or value == "":
            return default_query_expansion()
        if not isinstance(value, dict):
            raise TypeError("query_expansion must be a mapping of terms to term lists")
        raw_map: dict[str, list[str]] = {}
        for raw_term, raw_equivalents in value.items():
            if not isinstance(raw_equivalents, list):
                raise TypeError("query_expansion values must be lists of terms")
            term = str(raw_term).strip()
            if not term:
                continue
            raw_map[term] = [str(item).strip() for item in raw_equivalents if str(item).strip()]
        return normalize_term_map(raw_map)


def load_vault_config(path: Path) -> VaultConfig | None:
    """Load ``.datacron/VAULT.yaml`` from ``path`` if it exists."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping; found {type(data).__name__}.")
    return VaultConfig.model_validate(data)


def _split_path_list(value: str | list[str | Path] | None) -> list[Path]:
    """Parse the OS-dependent path separator into a list of resolved Paths.

    Empty entries are dropped. ``~`` is expanded. Each entry is resolved to an
    absolute path. The input may already be a list (for programmatic
    construction in tests).
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts: list[str] = [p for p in value.split(os.pathsep) if p.strip()]
        return [Path(p).expanduser().resolve() for p in parts]
    return [Path(p).expanduser().resolve() for p in value]


@final
class Settings(BaseSettings):
    """Datacron runtime settings.

    Loaded from environment variables prefixed ``DATACRON_`` and an optional
    ``.env`` file in the current working directory. All reserved runtime keys
    use the ``DATACRON_`` namespace.
    """

    model_config = SettingsConfigDict(
        env_prefix="DATACRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    log_level: str = Field(default=DEFAULT_LOG_LEVEL)
    log_dir: Path = Field(default=DEFAULT_LOG_DIR)
    read_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    write_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    vault_root: Path | None = Field(default=None)
    max_result_tokens: int = Field(default=DEFAULT_MAX_RESULT_TOKENS, ge=1)
    session_context_paths: list[str] = Field(
        default_factory=lambda: list(SESSION_DEFAULT_PATHS),
        max_length=SESSION_MAX_NOTES,
    )
    session_note_chars: int = Field(default=SESSION_NOTE_CHARS, ge=1)
    max_result_count: int = Field(default=DEFAULT_MAX_RESULT_COUNT, ge=1)
    repair_min_interval_seconds: float = Field(
        default=DEFAULT_REPAIR_MIN_INTERVAL_SECONDS,
        ge=0.0,
    )
    operation_history_purge_min_interval_seconds: float = Field(
        default=DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS,
        ge=0.0,
    )
    eval_regression_tolerance: float = Field(
        default=DEFAULT_EVAL_REGRESSION_TOLERANCE,
        ge=0.0,
    )
    contradiction_max_pairs: int = Field(default=DEFAULT_CONTRADICTION_MAX_PAIRS, ge=1)
    contradiction_max_candidates: int = Field(
        default=DEFAULT_CONTRADICTION_MAX_CANDIDATES,
        ge=1,
    )
    contradiction_max_per_note_pair: int = Field(
        default=DEFAULT_CONTRADICTION_MAX_PER_NOTE_PAIR,
        ge=1,
    )
    contradiction_summary_evidence_chars: int = Field(
        default=DEFAULT_CONTRADICTION_SUMMARY_EVIDENCE_CHARS,
        ge=1,
    )
    ripgrep_path: str = Field(default=DEFAULT_RIPGREP_PATH)
    regex_max_frame_bytes: int = Field(default=DEFAULT_REGEX_MAX_FRAME_BYTES, ge=1)
    regex_fallback_max_pattern_length: int = Field(
        default=DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH,
        ge=1,
    )
    regex_fallback_timeout_seconds: float = Field(
        default=DEFAULT_REGEX_FALLBACK_TIMEOUT_SECONDS,
        gt=0,
    )
    vault_lock_timeout_seconds: float = Field(
        default=DEFAULT_VAULT_LOCK_TIMEOUT_SECONDS,
        gt=0,
    )
    chunk_max_tokens: int = Field(default=DEFAULT_CHUNK_MAX_TOKENS, ge=1)
    get_note_max_tokens: int = Field(default=DEFAULT_GET_NOTE_MAX_TOKENS, ge=1)
    redact_secrets: str = DEFAULT_REDACT_SECRETS
    secret_redaction_patterns: Annotated[list[str], NoDecode] = Field(default_factory=list)
    read_only: bool = False
    durability: str = DEFAULT_DURABILITY_MODE
    tool_description_profile: str = DEFAULT_TOOL_DESCRIPTION_PROFILE
    scrub_notes_per_second: float = Field(default=DEFAULT_SCRUB_NOTES_PER_SECOND, gt=0)
    scrub_mebibytes_per_second: float = Field(
        default=DEFAULT_SCRUB_MEBIBYTES_PER_SECOND,
        gt=0,
    )
    scrub_max_duration_seconds: float = Field(
        default=DEFAULT_SCRUB_MAX_DURATION_SECONDS,
        gt=0,
    )
    scrub_checkpoint_interval_notes: int = Field(
        default=DEFAULT_SCRUB_CHECKPOINT_INTERVAL_NOTES,
        ge=1,
    )
    scrub_checkpoint_path: Path = DEFAULT_SCRUB_CHECKPOINT_PATH
    scrub_canary_dir: Path = DEFAULT_SCRUB_CANARY_DIR
    scrub_canaries: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_SCRUB_CANARIES)
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        if not isinstance(value, str):
            return DEFAULT_LOG_LEVEL
        normalized = value.strip().upper()
        if normalized not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Invalid DATACRON_LOG_LEVEL: {value!r}. "
                f"Expected one of {sorted(VALID_LOG_LEVELS)}."
            )
        return normalized

    @field_validator("log_dir", mode="before")
    @classmethod
    def _expand_log_dir(cls, value: object) -> Path:
        if value is None or value == "":
            return DEFAULT_LOG_DIR
        return Path(str(value)).expanduser()

    @field_validator("read_paths", mode="before")
    @classmethod
    def _parse_read_paths(cls, value: object) -> list[Path]:
        if value is None or isinstance(value, str):
            return _split_path_list(value)
        if isinstance(value, list):
            return _split_path_list(value)
        raise TypeError(f"Unsupported type for read_paths: {type(value).__name__}")

    @field_validator("write_paths", mode="before")
    @classmethod
    def _parse_write_paths(cls, value: object) -> list[Path]:
        if value is None or isinstance(value, str):
            return _split_path_list(value)
        if isinstance(value, list):
            return _split_path_list(value)
        raise TypeError(f"Unsupported type for write_paths: {type(value).__name__}")

    @field_validator("vault_root", mode="before")
    @classmethod
    def _expand_vault_root(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser().resolve()

    @field_validator("redact_secrets", mode="before")
    @classmethod
    def _normalize_redact_secrets(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_SECRET_REDACTION_POLICIES:
            raise ValueError(
                f"DATACRON_REDACT_SECRETS must be one of {sorted(VALID_SECRET_REDACTION_POLICIES)}"
            )
        return normalized

    @field_validator("secret_redaction_patterns", mode="before")
    @classmethod
    def _parse_secret_redaction_patterns(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "DATACRON_SECRET_REDACTION_PATTERNS must be a JSON string list"
                ) from exc
            value = decoded
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError("secret_redaction_patterns must be a list of regex strings")
        patterns = [item for item in value if item]
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid secret redaction regex: {pattern!r}") from exc
        return patterns

    @field_validator("durability", mode="before")
    @classmethod
    def _normalize_durability(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_DURABILITY_MODES:
            raise ValueError(f"DATACRON_DURABILITY must be one of {sorted(VALID_DURABILITY_MODES)}")
        return normalized

    @field_validator("tool_description_profile", mode="before")
    @classmethod
    def _normalize_tool_description_profile(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_TOOL_DESCRIPTION_PROFILES:
            raise ValueError(
                "DATACRON_TOOL_DESCRIPTION_PROFILE must be one of "
                f"{sorted(VALID_TOOL_DESCRIPTION_PROFILES)}"
            )
        return normalized

    @field_validator("scrub_checkpoint_path", "scrub_canary_dir", mode="before")
    @classmethod
    def _normalize_scrub_relative_path(cls, value: object) -> Path:
        path = Path(str(value))
        if path.is_absolute() or path.drive or ".." in path.parts or path == Path("."):
            raise ValueError("scrubber paths must be non-empty vault-relative paths")
        return path

    @field_validator("scrub_canaries", mode="before")
    @classmethod
    def _parse_scrub_canaries(cls, value: object) -> dict[str, str]:
        if value is None or value == "":
            return dict(DEFAULT_SCRUB_CANARIES)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("DATACRON_SCRUB_CANARIES must be a JSON string mapping") from exc
        if not isinstance(value, dict) or not value:
            raise TypeError("scrub_canaries must be a non-empty mapping of paths to content")
        canaries: dict[str, str] = {}
        for raw_name, raw_content in value.items():
            if not isinstance(raw_name, str) or not isinstance(raw_content, str):
                raise TypeError("scrub_canaries keys and values must be strings")
            name = Path(raw_name)
            if name.is_absolute() or name.drive or ".." in name.parts or name == Path("."):
                raise ValueError("scrub canary names must be safe relative paths")
            canaries[name.as_posix()] = raw_content
        return canaries


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    The result is cached. Tests should call :func:`reset_settings_cache` after
    mutating ``os.environ`` so the next call re-reads the environment.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the :func:`get_settings` cache.

    Intended for test setup/teardown only.
    """
    get_settings.cache_clear()
