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
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, final

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from datacron.core.memory_protocol import (
    SESSION_DEFAULT_PATHS,
    SESSION_DEFAULT_SECTIONS,
    SESSION_MAX_HEADING_CHARS,
    SESSION_MAX_HEADING_DEPTH,
    SESSION_MAX_NOTES,
    SESSION_MAX_SECTIONS,
    SESSION_NOTE_CHARS,
)
from datacron.core.query_expansion import default_query_expansion, normalize_term_map
from datacron.i18n import t

# Pydantic's prefix on a ValueError raised by a validator; dropped from CLI messages.
_PYDANTIC_VALUE_ERROR_PREFIX: Final[str] = "Value error, "

ULID_LENGTH: Final[int] = 26
CANONICAL_ULID_REGEX: Final[str] = rf"^[0-9A-HJKMNP-TV-Z]{{{ULID_LENGTH}}}$"
CANONICAL_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(CANONICAL_ULID_REGEX)
LEGACY_NOTE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^[0-9A-Z]{{{ULID_LENGTH}}}$")
DEFAULT_LOG_LEVEL: Final[str] = "INFO"
DEFAULT_LOG_DIR: Final[Path] = Path.home() / ".datacron" / "logs"
DEFAULT_MAX_RESULT_TOKENS: Final[int] = 8000
DEFAULT_MAX_RESULT_COUNT: Final[int] = 20
DEFAULT_HISTORY_LIMIT: Final[int] = 100
DEFAULT_REPAIR_MIN_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_OPERATION_HISTORY_PURGE_MIN_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_EVAL_REGRESSION_TOLERANCE: Final[float] = 0.02
DEFAULT_CONTRADICTION_MAX_PAIRS: Final[int] = 256
DEFAULT_CONTRADICTION_MAX_SECTIONS: Final[int] = 4096
"""Sections one scan will hold, separately from the pairs it will examine.

One number used to serve both, so a vault with more than 256 sections had
everything past the first 256 silently excluded from the audit, in index
insertion order: editing a note moved it to the end of that order and out of the
window. Collecting a section is cheap, examining a pair issues a search, so the
two budgets are not the same size.
"""
DEFAULT_CONTRADICTION_MAX_CANDIDATES: Final[int] = 20
DEFAULT_CONTRADICTION_MAX_PER_NOTE_PAIR: Final[int] = 2
DEFAULT_CONTRADICTION_SUMMARY_EVIDENCE_CHARS: Final[int] = 160
DEFAULT_CONTRADICTION_PROVENANCE_LABELS: Final[dict[str, str]] = {
    "contradiction": t("contradiction.provenance.contradiction", locale="fr"),
    "refinement": t("contradiction.provenance.refinement", locale="fr"),
    "open_question": t("contradiction.provenance.open_question", locale="fr"),
}
DEFAULT_CONTRADICTION_SOURCE_CONNECTOR: Final[str] = t(
    "contradiction.provenance.source_connector", locale="fr"
)
TOKEN_ESTIMATE_CHARS_PER_TOKEN: Final[int] = 4
TEMPORAL_OVERFETCH_FACTOR: Final[int] = 3
# group_by_note widens its window by this factor until it holds enough distinct notes,
# and never past this many chunks, so one hub note cannot hide every other match.
GROUPED_OVERFETCH_GROWTH: Final[int] = 4
GROUPED_OVERFETCH_MAX_CHUNKS: Final[int] = 4000
# A search_text query runs on the index connection every tool call shares, so its
# size is bounded well above any question and far below a pasted document.
MAX_SEARCH_QUERY_CHARS: Final[int] = 2048
MAX_SEARCH_QUERY_TERMS: Final[int] = 64
# The one place that names the archive tags and the state-note namespace. A vault
# overrides the archive tags in its organization tag policy; the index, the library
# and the planner read them from here or from that policy, never from a local copy.
DEFAULT_ARCHIVE_TAGS: Final[tuple[str, ...]] = (
    "meta/archive",
    "memory/archive",
    "archive/compacted",
    "archive/journal-digest",
)
# Frontmatter ``status`` values that retire a note, compared casefolded. The index
# demotes such a note and the offline library classes it historical: one set, so the
# two never disagree about the same note.
HISTORICAL_STATUSES: Final[frozenset[str]] = frozenset({"archived", "historical", "superseded"})
DEFAULT_STATE_NOTE_NAMESPACE: Final[str] = "kind"
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
# Separate serialized envelope ceiling; body token budgets do not include JSON escaping.
DEFAULT_MAX_READ_RESPONSE_CHARS: Final[int] = 128 * 1024
MIN_READ_RESPONSE_CHARS: Final[int] = 4096
DEFAULT_LOG_RETENTION_DAYS: Final[int] = 14
# Forty-two months. Retention decides when the only stored copy of a previous
# version of a note is deleted, and a project can sit untouched for months and then
# resume, so a window measured in weeks silently discarded the history of everything
# paused. The sweep that applies it costs nothing extra for a long window: it reads
# nothing at all until the journal actually holds a record older than the cutoff.
DEFAULT_HISTORY_RETENTION_DAYS: Final[int] = 1278
DEFAULT_HISTORY_MODE: Final[str] = "full"
# Canonical text encoding and line-ending convention for the sidecar and the
# ``VAULT.yaml`` ``encoding``/``line_endings`` fields. Single source of truth so
# the writer (bootstrap) and the model default cannot drift apart.
DEFAULT_ENCODING: Final[str] = "utf-8"
DEFAULT_LINE_ENDINGS: Final[str] = "lf"
DEFAULT_REDACT_SECRETS: Final[str] = "all"
DEFAULT_DURABILITY_MODE: Final[str] = "best-effort"
DEFAULT_TOOL_DESCRIPTION_PROFILE: Final[str] = "standard"
DEFAULT_TOOL_PROFILE: Final[str] = "standard"
TOOL_PROFILE_ESSENTIAL: Final[str] = "essential"
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
MEMORY_SUBFOLDER_NAME: Final[str] = "_memory"
# The default runbook vault, beside _memory: in the memory vault when one is
# configured, at the vault root otherwise.
RUNBOOKS_SUBFOLDER_NAME: Final[str] = "_runbooks"
# The top folders a memory vault serves. Anything else under it is refused.
MEMORY_ROOT_SUBFOLDERS: Final[tuple[str, ...]] = (MEMORY_SUBFOLDER_NAME, RUNBOOKS_SUBFOLDER_NAME)
INDEX_DIR_NAME: Final[str] = "index"
INDEX_DB_FILENAME: Final[str] = "datacron.db"
HISTORY_DIR_NAME: Final[str] = "history"
OPLOG_DIR_NAME: Final[str] = "oplog"
OPLOG_PENDING_DIR_NAME: Final[str] = "pending"
# Companion memory inbox. The review marks live in a sidecar file keyed by operation id,
# never in the operation journal or in a note.
COMPANION_STATE_DIR_NAME: Final[str] = "companion"
INBOX_REVIEW_STATE_FILENAME: Final[str] = "review-state.json"
INBOX_REVIEW_STATE_VERSION: Final[int] = 1
INBOX_REVIEW_LOCK_KEY: Final[str] = "companion-review"
# Reviewed operations kept in the state file; the oldest marks are dropped past this.
INBOX_REVIEW_MAX_ENTRIES: Final[int] = 10_000
INBOX_REVIEW_MAX_STATE_BYTES: Final[int] = 4 * 1024 * 1024
INBOX_DEFAULT_LIMIT: Final[int] = 50
INBOX_MAX_LIMIT: Final[int] = 200
INBOX_MAX_OFFSET: Final[int] = 100_000
INBOX_FILTER_MAX_CHARS: Final[int] = 256
INBOX_OPERATION_ID_MAX_CHARS: Final[int] = 128
INBOX_SINCE_ALL: Final[str] = "all"
# Selector value -> window in seconds; "all" has no entry and means no lower bound.
INBOX_SINCE_WINDOWS_SECONDS: Final[dict[str, int]] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}
INBOX_DEFAULT_SINCE: Final[str] = INBOX_SINCE_ALL
# Largest unified diff the inbox returns, in characters; the rest is cut and flagged.
INBOX_DIFF_MAX_CHARS: Final[int] = 200_000
# Combined size of the two texts above which no diff is computed (difflib is quadratic).
INBOX_DIFF_MAX_SOURCE_BYTES: Final[int] = 1024 * 1024
INBOX_DIFF_CONTEXT_LINES: Final[int] = 3
INBOX_DIFF_FROM_LABEL: Final[str] = "before"
INBOX_DIFF_TO_LABEL: Final[str] = "after"
# Actor recorded on a revert made from the Companion (model: the CLI operator actor).
COMPANION_REVERT_ACTOR: Final[str] = "companion:local-operator"
INBOX_REVERT_REQUEST_PREFIX: Final[str] = "companion-revert-"
# Files an operating system shell writes into any folder it displays, casefolded.
# Recovery directories ignore them instead of reading them as unexplained evidence.
OS_METADATA_FILENAMES: Final[frozenset[str]] = frozenset({"desktop.ini", ".ds_store", "thumbs.db"})
# macOS writes an AppleDouble companion ``._<name>`` beside files on non-HFS volumes.
OS_METADATA_FILENAME_PREFIX: Final[str] = "._"
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
VALID_TOOL_PROFILES: Final[frozenset[str]] = frozenset(
    {DEFAULT_TOOL_PROFILE, TOOL_PROFILE_ESSENTIAL}
)

# Essential profile verbs: bounds and conventions, named once so the tools, the
# tests and the documentation cannot drift apart.
ESSENTIAL_REMEMBER_HEADING: Final[str] = "Remembered"
ESSENTIAL_REMEMBER_KINDS: Final[tuple[str, ...]] = ("fact", "decision", "preference")
ESSENTIAL_REMEMBER_DEFAULT_KIND: Final[str] = "fact"
ESSENTIAL_REMEMBER_MAX_CONTENT_CHARS: Final[int] = 8000
ESSENTIAL_REMEMBER_MAX_SOURCE_CHARS: Final[int] = 512
ESSENTIAL_REMEMBER_TITLE_MAX_CHARS: Final[int] = 80
ESSENTIAL_REMEMBER_ENTRY_DATE_FORMAT: Final[str] = "%Y-%m-%d"
ESSENTIAL_REMEMBER_CAS_RETRIES: Final[int] = 1
ESSENTIAL_REMEMBER_SOURCE_LABEL: Final[str] = "Source"
ESSENTIAL_REMEMBER_ORIGIN: Final[str] = "ai"
ESSENTIAL_REMEMBER_CONFIDENCE: Final[str] = "medium"
ESSENTIAL_REMEMBER_MAX_CANDIDATES: Final[int] = 10
ESSENTIAL_RECALL_DEFAULT_LIMIT: Final[int] = 5
ESSENTIAL_RECALL_MAX_LIMIT: Final[int] = 10
ESSENTIAL_RECALL_EXCERPT_MAX_CHARS: Final[int] = 1500
ESSENTIAL_WHAT_CHANGED_DEFAULT_LIMIT: Final[int] = 50
ESSENTIAL_WHAT_CHANGED_MAX_LIMIT: Final[int] = 200
ESSENTIAL_WHAT_CHANGED_MAX_OUT_OF_BAND: Final[int] = 50
ESSENTIAL_WHAT_CHANGED_SUMMARY_MAX_CHARS: Final[int] = 160
ESSENTIAL_INSTRUCTIONS_MAX_CHARS: Final[int] = 900

# Runbook capture. The excluded directories are a hard limit arbitrated on
# 2026-10-29: a vault cannot declare them away. They are generic names, matched as
# a path component anywhere under a runbook and compared casefolded whatever the
# filesystem, because refusing too much here is the safe side. Folders specific
# to one vault are declared by that vault in runbooks.protected_paths.
RUNBOOK_EXCLUDED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "confluence-source", "confluence-updates"}
)
RUNBOOK_CAPTURE_KINDS: Final[tuple[str, ...]] = (
    "error_fix",
    "command",
    "server",
    "variable",
    "contact",
    "decision",
    "fact",
    "open_question",
)
RUNBOOK_MARKDOWN_SUFFIX: Final[str] = ".md"
RUNBOOK_NAMING_SLUG: Final[str] = "slug"
RUNBOOK_NAMING_SERVICE: Final[str] = "service"
DEFAULT_RUNBOOK_DIRECTORY_NAMING: Final[str] = "{slug}-runbook"
RUNBOOK_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_RUNBOOK_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
_RUNBOOK_LANGUAGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
)


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
    # Days after ``last_verified`` before a note of this type reads as stale in
    # retrieval. Absent means the type carries no freshness expectation.
    review_after_days: int | None = None

    @field_validator("review_after_days", mode="before")
    @classmethod
    def _strict_review_after_days(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"organization rule review_after_days must be an integer; got {value!r}"
            )
        if value < 1:
            raise ValueError(f"organization rule review_after_days must be at least 1; got {value}")
        return value

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
        return _normalize_vault_folder(value, what="organization rule folder")

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


def _normalize_vault_folder(value: object, *, what: str) -> str:
    # Absoluteness must be judged before the separators are trimmed, or a
    # leading slash would be silently normalized into a relative path.
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    folder = value.strip().replace("\\", "/")
    if not folder.strip("/"):
        raise ValueError(f"{what} must not be empty")
    if folder.startswith("/") or ":" in folder:
        raise ValueError(f"{what} must be vault-relative; got {folder!r}")
    trimmed = folder.strip("/")
    if any(segment in {"..", "."} for segment in trimmed.split("/")):
        raise ValueError(f"{what} must not traverse directories: {folder!r}")
    return trimmed


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
    # Tags that mark a note as archived for ranking and for the offline library.
    archive_tags: tuple[str, ...] = DEFAULT_ARCHIVE_TAGS

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

    @field_validator("markers", "subject_exempt_tags", "archive_tags", mode="before")
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

    ``state_note_min_notes`` and ``linking_since`` switch on two folder-level
    measurements over the subject rules; both stay off until the vault declares
    them, so an existing sidecar measures exactly what it measured before. They
    are read strictly (a boolean is not a count, a number is not a date) and
    require a tag policy with a ``subject_namespace``: without subject rules the
    measurements cannot exist, and a silent no-op would hide a misconfiguration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str | None = None
    rules: tuple[OrganizationRule, ...] = ()
    tags: OrganizationTagPolicy | None = None
    state_note_min_notes: int | None = None
    linking_since: date | None = None

    @field_validator("state_note_min_notes", mode="before")
    @classmethod
    def _strict_state_note_min_notes(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"organization state_note_min_notes must be an integer; got {value!r}")
        if value < 1:
            raise ValueError(f"organization state_note_min_notes must be at least 1; got {value}")
        return value

    @field_validator("linking_since", mode="before")
    @classmethod
    def _strict_linking_since(cls, value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError(
                    f"organization linking_since must be a YYYY-MM-DD date; got {value!r}"
                ) from exc
        raise ValueError(f"organization linking_since must be a date; got {value!r}")

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
        subject_namespace = None if self.tags is None else self.tags.subject_namespace
        if subject_namespace is None:
            for key, value in (
                ("state_note_min_notes", self.state_note_min_notes),
                ("linking_since", self.linking_since),
            ):
                if value is not None:
                    raise ValueError(
                        f"organization {key} requires organization.tags.subject_namespace: "
                        "the folder measurements exist only over subject rules"
                    )
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
    placement_rule_tags = _placement_rule_tags(rules, tags)
    if not placement_rule_tags:
        raise ValueError("organization tags policy requires at least one placement rule")
    _validate_policy_names(tags, rule_tags, placement_rule_tags)


def _placement_rule_tags(
    rules: tuple[OrganizationRule, ...],
    tags: OrganizationTagPolicy,
) -> set[str]:
    """Return the placement rule tags; refuse a rule the policy could not classify."""
    prefix = tags.placement_namespace.casefold() + "/"
    # The evaluator and the rule resolver compare tags with ``lower()``; admitting a
    # subject rule under ``casefold()`` would accept a rule no compliant note can reach.
    subject_tags = {subject.tag.lower() for subject in tags.subjects}
    subject_prefix = (
        None if tags.subject_namespace is None else tags.subject_namespace.lower() + "/"
    )
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
        elif (
            rule.tag not in subject_tags
            or subject_prefix is None
            or not rule.tag.startswith(subject_prefix)
        ):
            # The evaluator classifies a tag by its lowercased namespace: a rule
            # whose namespace only matches the policy under casefold() could win
            # the placement and still be reported as an unknown tag.
            raise ValueError(
                f"organization rule tag {rule.tag!r} is outside the placement namespace "
                f"{tags.placement_namespace!r} declared by the tags policy and is not a "
                "declared subject"
            )
    return placement_rule_tags


def _validate_policy_names(
    tags: OrganizationTagPolicy,
    rule_tags: set[str],
    placement_rule_tags: set[str],
) -> None:
    """Every alias, marker and exemption the policy names must match the rules."""
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
            if name.casefold() not in rule_tags:
                raise ValueError(f"organization tags {label} {name!r} must be a declared rule tag")
            if name.casefold() not in placement_rule_tags:
                raise ValueError(
                    f"organization tags {label} {name!r} must be a declared placement rule tag"
                )


_EXCLUSION_SEPARATORS: Final[tuple[str, ...]] = ("/", "\\")


def _exclusion_names(value: list[object], *, field: str) -> list[str]:
    """Return the single path-component names an exclusion list may hold.

    Admission compares each entry with one path component at a time, so an entry
    holding a separator could never match anything: ``Clients/Confidential`` and
    ``sub/blocked.md`` were accepted and excluded nothing, silently. One trailing
    separator is only how a folder is often written and is dropped; any other
    separator is refused, naming the entry, so the vault does not start with an
    exclusion it believes in and does not have.
    """
    names: list[str] = []
    for item in value:
        entry = str(item).strip()
        if not entry:
            continue
        name = entry[:-1].strip() if entry.endswith(_EXCLUSION_SEPARATORS) else entry
        if not name or any(separator in name for separator in _EXCLUSION_SEPARATORS):
            raise ValueError(
                f"{field} entry {entry!r} must be a single folder or file name, "
                "without a path separator; entries match one path component anywhere "
                "in the vault"
            )
        names.append(name)
    return names


def _normalize_runbook_component(value: object, *, what: str) -> str:
    """Return one path component a runbook may write, or refuse it.

    A component is a single folder or file stem: no separator, no drive, no
    traversal, and never one of :data:`RUNBOOK_EXCLUDED_DIRECTORIES`, so no
    declared name can route a write into a tree the runbook rules keep out.
    """
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    name = value.strip()
    if not name:
        raise ValueError(f"{what} must not be empty")
    if any(separator in name for separator in ("/", "\\", ":")) or name in {".", ".."}:
        raise ValueError(f"{what} must be a single folder or file name; got {name!r}")
    if name.casefold() in RUNBOOK_EXCLUDED_DIRECTORIES:
        raise ValueError(f"{what} {name!r} is a directory runbooks never write into")
    return name


def _normalize_runbook_name(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not _RUNBOOK_NAME_PATTERN.match(value.strip()):
        raise ValueError(
            f"{what} must be a lowercase identifier ({_RUNBOOK_NAME_PATTERN.pattern}); "
            f"got {value!r}"
        )
    return value.strip()


def _normalize_runbook_text(value: object, *, what: str) -> str:
    """Return a non-empty single-line text the vault declares for rendering."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{what} must not be empty")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{what} must be a single line")
    return text


def _normalize_runbook_language(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not _RUNBOOK_LANGUAGE_PATTERN.match(value.strip()):
        raise ValueError(f"{what} must be a language tag such as 'en' or 'fr'; got {value!r}")
    return value.strip()


def _validate_single_token_naming(naming: str, token: str, *, what: str) -> str:
    """Refuse a naming that is not one ``{token}`` plus a literal, non-empty affix."""
    tokens = _NAMING_TOKEN_PATTERN.findall(naming)
    if tokens != [token]:
        raise ValueError(f"{what} must contain '{{{token}}}' exactly once and no other token")
    literal = naming.replace(f"{{{token}}}", "")
    _normalize_runbook_component(naming.replace(f"{{{token}}}", token), what=what)
    return literal


@final
class RunbookRole(BaseModel):
    """One file of a runbook template, found by the pattern its stem matches.

    The stem is what a creation uses; ``match`` is how an existing runbook's file
    for the role is found, because numbering differs between runbooks
    (``50-troubleshooting`` in one, ``70-troubleshooting`` in another). The stem
    must satisfy its own pattern, or a file the template creates would not be
    found again by the next capture.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stem: str
    match: str
    # The H1 of a file this role creates; without it the capture refuses to create one.
    title: str | None = None

    @field_validator("stem", mode="before")
    @classmethod
    def _normalize_stem(cls, value: object) -> str:
        stem = _normalize_runbook_component(value, what="runbook role stem")
        if stem.casefold().endswith(RUNBOOK_MARKDOWN_SUFFIX):
            raise ValueError(
                f"runbook role stem must not carry the {RUNBOOK_MARKDOWN_SUFFIX} suffix; "
                f"got {stem!r}"
            )
        return stem

    @field_validator("match", mode="before")
    @classmethod
    def _compile_match(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runbook role match must be a non-empty regular expression")
        pattern = value.strip()
        if has_catastrophic_regex_shape(pattern):
            raise ValueError(f"runbook role match has a catastrophic regex shape: {pattern!r}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"runbook role match is not a valid regex: {pattern!r}") from exc
        return pattern

    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_runbook_text(value, what="runbook role title")

    @model_validator(mode="after")
    def _stem_satisfies_match(self) -> RunbookRole:
        if re.fullmatch(self.match, self.stem) is None:
            raise ValueError(
                f"runbook role stem {self.stem!r} does not match its own pattern {self.match!r}"
            )
        return self

    def matches(self, stem: str) -> bool:
        """Return whether an existing file stem plays this role."""
        return re.fullmatch(self.match, stem) is not None


@final
class RunbookQuickref(BaseModel):
    """The folder of per-service survival sheets, with its own language."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    folder: str
    language: str
    naming: str = f"{{{RUNBOOK_NAMING_SERVICE}}}"

    @field_validator("folder", mode="before")
    @classmethod
    def _normalize_folder(cls, value: object) -> str:
        return _normalize_runbook_component(value, what="runbook quickref folder")

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        return _normalize_runbook_language(value, what="runbook quickref language")

    @field_validator("naming", mode="before")
    @classmethod
    def _normalize_naming(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("runbook quickref naming must be a string")
        naming = value.strip()
        _validate_single_token_naming(
            naming, RUNBOOK_NAMING_SERVICE, what="runbook quickref naming"
        )
        return naming


@final
class RunbookTemplate(BaseModel):
    """The shape of one kind of runbook: its files, its words and its routing.

    Every rendered word comes from here, in the template's language, so Datacron
    ships no runbook text. ``routing`` maps a capture kind to a declared role;
    a kind left out has no default and a capture of it must name its role.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str
    readme: str
    roles: dict[str, RunbookRole]
    quickref: RunbookQuickref | None = None
    observation_label: str
    labels: dict[str, str] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        return _normalize_runbook_language(value, what="runbook template language")

    @field_validator("readme", mode="before")
    @classmethod
    def _normalize_readme(cls, value: object) -> str:
        return _normalize_runbook_component(value, what="runbook template readme")

    @field_validator("observation_label", mode="before")
    @classmethod
    def _normalize_texts(cls, value: object) -> str:
        return _normalize_runbook_text(value, what="runbook template text")

    @field_validator("roles", mode="before")
    @classmethod
    def _normalize_role_names(cls, value: object) -> object:
        if not isinstance(value, dict) or not value:
            raise ValueError("runbook template roles must be a non-empty mapping")
        return {
            _normalize_runbook_name(key, what="runbook role name"): item
            for key, item in value.items()
        }

    @field_validator("labels", mode="before")
    @classmethod
    def _normalize_labels(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("runbook template labels must be a mapping")
        return {
            _normalize_runbook_name(key, what="runbook label name"): _normalize_runbook_text(
                item, what=f"runbook label {key!r}"
            )
            for key, item in value.items()
        }

    @field_validator("routing", mode="before")
    @classmethod
    def _normalize_routing(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("runbook template routing must be a mapping")
        routing: dict[str, str] = {}
        for kind, role in value.items():
            if kind not in RUNBOOK_CAPTURE_KINDS:
                raise ValueError(
                    f"runbook routing kind must be one of {list(RUNBOOK_CAPTURE_KINDS)}; "
                    f"got {kind!r}"
                )
            routing[kind] = _normalize_runbook_name(role, what=f"runbook routing role of {kind!r}")
        return routing

    @model_validator(mode="after")
    def _check_files(self) -> RunbookTemplate:
        stems: dict[str, str] = {self.readme.casefold(): "readme"}
        for name, role in self.roles.items():
            previous = stems.get(role.stem.casefold())
            if previous is not None:
                raise ValueError(
                    f"runbook role {name!r} reuses the file stem {role.stem!r} of {previous!r}"
                )
            stems[role.stem.casefold()] = name
            # A creation must be found again by its own role only: a stem that a
            # second role also claims would make the next capture ambiguous.
            claimants = [other for other, item in self.roles.items() if item.matches(role.stem)]
            if claimants != [name]:
                raise ValueError(
                    f"runbook role stem {role.stem!r} is matched by several roles: {claimants}"
                )
        if self.quickref is not None and self.quickref.folder.casefold() in stems:
            raise ValueError(
                f"runbook quickref folder {self.quickref.folder!r} collides with a file stem"
            )
        for kind, role_name in self.routing.items():
            if role_name not in self.roles:
                raise ValueError(
                    f"runbook routing sends {kind!r} to the undeclared role {role_name!r}"
                )
        return self


@final
class RunbooksConfig(BaseModel):
    """How runbooks look in the vault folder named by ``DATACRON_RUNBOOK_VAULT``.

    Each child folder of that runbook vault named by ``directory_naming`` is one
    runbook; ``runbook_capture`` creates a new one there for a project named in
    conversation. The template of a runbook is ``assignments[slug]`` when the
    vault assigns one, else ``default_template``. Declaring this block does not make
    anything writable: without ``DATACRON_RUNBOOK_VAULT`` no runbook is.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory_naming: str = DEFAULT_RUNBOOK_DIRECTORY_NAMING
    templates: dict[str, RunbookTemplate]
    default_template: str | None = None
    assignments: dict[str, str] = Field(default_factory=dict)
    # Paths, relative to the runbook vault, the runbook writer always refuses.
    # Empty by default: the names are the vault's.
    protected_paths: tuple[str, ...] = ()

    @field_validator("directory_naming", mode="before")
    @classmethod
    def _normalize_directory_naming(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("runbooks directory_naming must be a string")
        naming = value.strip()
        literal = _validate_single_token_naming(
            naming, RUNBOOK_NAMING_SLUG, what="runbooks directory_naming"
        )
        # Without a literal part every child folder of the runbook vault would be
        # a runbook, including folders that were never meant to be one.
        if not literal:
            raise ValueError(
                "runbooks directory_naming needs a literal part besides '{slug}', "
                f"such as {DEFAULT_RUNBOOK_DIRECTORY_NAMING!r}"
            )
        return naming

    @field_validator("templates", mode="before")
    @classmethod
    def _normalize_template_names(cls, value: object) -> object:
        if not isinstance(value, dict) or not value:
            raise ValueError("runbooks templates must be a non-empty mapping")
        return {
            _normalize_runbook_name(key, what="runbook template name"): item
            for key, item in value.items()
        }

    @field_validator("default_template", mode="before")
    @classmethod
    def _normalize_default_template(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_runbook_name(value, what="runbooks default_template")

    @field_validator("assignments", mode="before")
    @classmethod
    def _normalize_assignments(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("runbooks assignments must map a runbook slug to a template")
        assignments: dict[str, str] = {}
        for slug, template in value.items():
            if not isinstance(slug, str) or not RUNBOOK_SLUG_PATTERN.match(slug.strip()):
                raise ValueError(
                    f"runbooks assignments key must be a runbook slug "
                    f"({RUNBOOK_SLUG_PATTERN.pattern}); got {slug!r}"
                )
            assignments[slug.strip()] = _normalize_runbook_name(
                template, what=f"runbooks assignment of {slug!r}"
            )
        return assignments

    @field_validator("protected_paths", mode="before")
    @classmethod
    def _normalize_protected_paths(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("runbooks protected_paths must be a list of relative paths")
        return tuple(
            _normalize_vault_folder(item, what="runbooks protected path") for item in value
        )

    @model_validator(mode="after")
    def _check_templates(self) -> RunbooksConfig:
        if self.default_template is None and len(self.templates) > 1:
            raise ValueError(
                "runbooks default_template is required when several templates are declared"
            )
        if self.default_template is not None and self.default_template not in self.templates:
            raise ValueError(
                f"runbooks default_template names the undeclared template {self.default_template!r}"
            )
        for slug, template in self.assignments.items():
            if template not in self.templates:
                raise ValueError(
                    f"runbooks assignment of {slug!r} names the undeclared template {template!r}"
                )
        return self

    def template_for(self, slug: str) -> RunbookTemplate:
        """Return the template of the runbook ``slug``: its assignment, else the default."""
        name = self.assignments.get(slug) or self.default_template or next(iter(self.templates))
        return self.templates[name]

    def directory_name(self, slug: str) -> str:
        """Return the folder name of the runbook ``slug``."""
        return self.directory_naming.replace(f"{{{RUNBOOK_NAMING_SLUG}}}", slug)

    def directory_slug(self, name: str, *, fold_case: bool = False) -> str | None:
        """Return the slug of a folder named like a runbook, or ``None``."""
        prefix, _, suffix = self.directory_naming.partition(f"{{{RUNBOOK_NAMING_SLUG}}}")
        start, end = len(prefix), len(name) - len(suffix)
        if end <= start:
            return None
        head, tail = name[:start], name[end:]
        if fold_case:
            head, tail, prefix, suffix = (
                head.casefold(),
                tail.casefold(),
                prefix.casefold(),
                suffix.casefold(),
            )
        if head != prefix or tail != suffix:
            return None
        return name[start:end]

    def protects(self, parts: Sequence[str]) -> bool:
        """Return whether ``parts``, relative to the runbook vault, lie in a protected path."""
        folded = [part.casefold() for part in parts]
        for protected in self.protected_paths:
            prefix = [segment.casefold() for segment in protected.split("/")]
            if folded[: len(prefix)] == prefix:
                return True
        return False


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
    top_level_archive_tags: tuple[str, ...] | None = Field(
        default=None,
        alias="archive_tags",
        exclude=True,
    )
    # Absent block means the feature is inert: every existing vault keeps its
    # exact current behaviour, which is the non-regression guarantee for vaults
    # already published against earlier releases.
    organization: OrganizationConfig | None = None
    # Same non-regression guarantee: no block, no runbook behaviour anywhere.
    runbooks: RunbooksConfig | None = None

    @field_validator("top_level_archive_tags", mode="before")
    @classmethod
    def _normalize_top_level_archive_tags(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("archive_tags must be a list of strings")
        return tuple(_normalize_tag_value(item, what="archive_tags entry") for item in value)

    @field_validator("organization", "runbooks", mode="before")
    @classmethod
    def _normalize_organization(cls, value: object) -> object:
        # An absent or empty mapping means no policy. Other false-like YAML
        # values are type errors, not a silent way to disable organization.
        if value is None or value == {}:
            return None
        return value

    @property
    def archive_tags(self) -> frozenset[str]:
        """Casefolded tags that mark an archived note: top-level, policy's, or defaults."""
        if self.top_level_archive_tags is not None:
            declared: tuple[str, ...] = self.top_level_archive_tags
        else:
            policy = self.organization.tags if self.organization is not None else None
            declared = policy.archive_tags if policy is not None else DEFAULT_ARCHIVE_TAGS
        return frozenset(tag.casefold() for tag in declared)

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
        return _exclusion_names(value, field="excluded_folders")

    @field_validator("excluded_files", mode="before")
    @classmethod
    def _normalize_excluded_files(cls, value: object) -> list[str]:
        if value is None or value == "":
            return list(DEFAULT_EXCLUDED_FILES)
        if not isinstance(value, list):
            raise TypeError("excluded_files must be a list of file names")
        return _exclusion_names(value, field="excluded_files")

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


_BRACE_QUANTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^\{\d+(?:,\d*)?\}")
_BACKREF_QUANTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"\\[1-9]\d*(?:[+*]|\{\d)")


def has_catastrophic_regex_shape(pattern: str) -> bool:
    """Return True if ``pattern`` has nested repetition or quantified alternation."""
    if _BACKREF_QUANTIFIER_RE.search(pattern):
        return True
    stack: list[bool] = []
    in_char_class = False
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_char_class:
            if char == "]":
                in_char_class = False
            index += 1
            continue
        if char == "[":
            in_char_class = True
            index += 1
            continue
        if char == "(":
            stack.append(False)
            index += 1
            continue
        if char == ")":
            has_inner = stack.pop() if stack else False
            next_slice = pattern[index + 1 :]
            is_quantified = next_slice.startswith(("+", "*")) or bool(
                _BRACE_QUANTIFIER_RE.match(next_slice)
            )
            if has_inner and is_quantified:
                return True
            if (has_inner or is_quantified) and stack:
                stack[-1] = True
            index += 1
            continue
        if stack and (
            char in {"+", "*", "|"}
            or (char == "{" and bool(_BRACE_QUANTIFIER_RE.match(pattern[index:])))
        ):
            stack[-1] = True
        index += 1
    return False


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


def validate_extra_vault_prefix(raw_prefix: object) -> str:
    """Return the trimmed mount prefix, or raise when it is not one safe folder name."""
    if not isinstance(raw_prefix, str):
        raise TypeError("extra_read_vaults prefixes must be strings")
    prefix = raw_prefix.strip().strip("/\\")
    if (
        not prefix
        or "/" in prefix
        or "\\" in prefix
        or ":" in prefix
        or prefix.startswith(".")
        or any(char.isspace() for char in prefix)
        or prefix.casefold()
        in {name.casefold() for name in (*MEMORY_ROOT_SUBFOLDERS, SIDECAR_DIR_NAME)}
    ):
        raise ValueError(
            f"extra_read_vaults prefix must be a single safe folder name; got {raw_prefix!r}"
        )
    return prefix


def _parse_extra_read_vaults_str(raw: str) -> dict[object, object]:
    stripped = raw.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "DATACRON_EXTRA_READ_VAULTS JSON must be a mapping of prefix to path"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError("DATACRON_EXTRA_READ_VAULTS JSON must be a mapping of prefix to path")
        return decoded
    mapping: dict[object, object] = {}
    for entry in stripped.split(os.pathsep):
        item = entry.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"DATACRON_EXTRA_READ_VAULTS entry must be 'prefix=path'; got {item!r}"
            )
        key, raw_path = item.split("=", 1)
        mapping[key] = raw_path
    return mapping


def _parse_extra_read_vaults_value(value: object) -> dict[str, Path]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        raw_mapping = _parse_extra_read_vaults_str(value)
    elif isinstance(value, dict):
        raw_mapping = value
    else:
        raise TypeError(f"Unsupported type for extra_read_vaults: {type(value).__name__}")
    result: dict[str, Path] = {}
    seen_folded: set[str] = set()
    for raw_prefix, raw_target in raw_mapping.items():
        prefix = validate_extra_vault_prefix(raw_prefix)
        folded = prefix.casefold()
        if folded in seen_folded:
            raise ValueError(f"Duplicate extra_read_vaults prefix: {prefix!r}")
        seen_folded.add(folded)
        target_str = str(raw_target).strip() if raw_target is not None else ""
        if not target_str:
            raise ValueError(f"extra_read_vaults path for {prefix!r} must not be empty")
        result[prefix] = Path(target_str).expanduser().resolve()
    return result


def _roots_overlap(first: Path, second: Path) -> bool:
    """Return whether one resolved root is the other or contains it."""
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def extra_mount_warnings(vault_root: Path, extra_read_vaults: Mapping[str, Path]) -> list[str]:
    """Return one message per mount that hides docs notes or reads nothing.

    A mount prefix is a top-level folder name of the merged vault, compared without
    case, and the mount always wins: a docs folder of the same name drops out of the
    walk, the index and search, even when the mount path does not exist. The masking
    is the documented rule and stays; these messages make it visible at startup
    instead of letting the docs notes vanish silently.
    """
    messages: list[str] = []
    folded_prefixes = {prefix.casefold(): prefix for prefix in extra_read_vaults}
    try:
        top_level = sorted(
            entry.name
            for entry in vault_root.iterdir()
            if entry.name.casefold() in folded_prefixes and entry.is_dir()
        )
    except OSError:
        top_level = []
    for folder in top_level:
        prefix = folded_prefixes[folder.casefold()]
        messages.append(
            f"extra_read_vaults mount {prefix!r} masks the docs folder {folder!r} of "
            f"{vault_root}: its notes are left out of the index and search while the "
            "mount is configured; rename the folder or choose another prefix"
        )
    for prefix, mount_root in extra_read_vaults.items():
        if not mount_root.is_dir():
            messages.append(
                f"extra_read_vaults mount {prefix!r} points to {mount_root}, which is not "
                "an existing folder: the mount reads no notes"
            )
    return messages


class RootTopologyError(ValueError):
    """The vault, memory and mount roots of one configuration contain one another."""


def configuration_error_detail(error: ValidationError) -> str:
    """Return the messages of a settings validation error as one line, for a CLI."""
    details = []
    for item in error.errors():
        message = str(item.get("msg", ""))
        details.append(message.removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX))
    return "; ".join(details) or str(error)


def validate_root_topology(
    vault_root: Path | None,
    memory_root: Path | None,
    extra_read_vaults: Mapping[str, Path],
) -> None:
    """Refuse a vault, memory and mount layout whose roots contain one another.

    Every root is walked on its own and its notes keyed relative to it, so a root that
    sits inside another one is read twice under two identities: a mount above the vault
    re-keys every docs note under a read-only prefix, a mount inside it lists its notes
    twice, a memory root inside the docs vault puts ``_memory`` in the docs scope, and a
    memory root above it claims every docs path for the memory scope. A memory root equal
    to the vault root is the single-vault layout and stays valid. Paths are compared
    resolved and with the platform's path semantics (case-insensitive on Windows).

    Settings runs this when it is built, and the server runs it again on the roots it
    actually serves, because a binding made with ``model_copy`` skips the validators.
    """
    dual_memory = memory_root if memory_root is not None and memory_root != vault_root else None
    if vault_root is not None and dual_memory is not None:
        if vault_root.is_relative_to(dual_memory):
            raise RootTopologyError(
                f"memory_root {dual_memory} contains vault_root {vault_root}; "
                "the two vaults must be separate folders, for example siblings"
            )
        if dual_memory.is_relative_to(vault_root):
            raise RootTopologyError(
                f"memory_root {dual_memory} is inside vault_root {vault_root}; "
                "the two vaults must be separate folders, for example siblings"
            )
    named_roots = [("vault_root", vault_root), ("memory_root", dual_memory)]
    mounts = list(extra_read_vaults.items())
    for index, (prefix, mount_root) in enumerate(mounts):
        for name, root in named_roots:
            if root is not None and _roots_overlap(mount_root, root):
                raise RootTopologyError(
                    f"extra_read_vaults mount {prefix!r} ({mount_root}) overlaps {name} "
                    f"{root}; a mount must neither contain nor sit inside it"
                )
        for other_prefix, other_root in mounts[index + 1 :]:
            if _roots_overlap(mount_root, other_root):
                raise RootTopologyError(
                    f"extra_read_vaults mounts {prefix!r} ({mount_root}) and "
                    f"{other_prefix!r} ({other_root}) overlap; mounts must be disjoint folders"
                )


@final
class Settings(BaseSettings):
    """Datacron runtime settings.

    Loaded from environment variables prefixed ``DATACRON_``. All reserved
    runtime keys use the ``DATACRON_`` namespace.

    No ``.env`` file is read. One used to be, from the current working
    directory, which for a stdio server is whatever folder the MCP client
    opened: a cloned repository's ``.env`` could set
    ``DATACRON_REDACT_SECRETS=off``, the log directory or the ripgrep binary
    without anything saying so.
    """

    model_config = SettingsConfigDict(
        env_prefix="DATACRON_",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    log_level: str = Field(default=DEFAULT_LOG_LEVEL)
    log_dir: Path = Field(default=DEFAULT_LOG_DIR)
    read_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    write_paths: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    # The vault folder that holds runbooks. Only the runbook capture writes there,
    # and the generic write tools never treat it as a write path.
    runbook_vault: Path | None = Field(default=None)
    vault_root: Path | None = Field(default=None)
    memory_root: Path | None = Field(default=None)
    extra_read_vaults: Annotated[dict[str, Path], NoDecode] = Field(default_factory=dict)
    max_result_tokens: int = Field(default=DEFAULT_MAX_RESULT_TOKENS, ge=1)
    session_context_paths: list[str] = Field(
        default_factory=lambda: list(SESSION_DEFAULT_PATHS),
        max_length=SESSION_MAX_NOTES,
    )
    session_note_chars: int = Field(default=SESSION_NOTE_CHARS, ge=1)
    session_context_sections: dict[str, list[list[str]]] = Field(
        default_factory=lambda: {
            path: [list(heading_path) for heading_path in sections]
            for path, sections in SESSION_DEFAULT_SECTIONS.items()
        }
    )

    @field_validator("session_context_sections")
    @classmethod
    def _validate_session_sections(
        cls, value: dict[str, list[list[str]]]
    ) -> dict[str, list[list[str]]]:
        if len(value) > SESSION_MAX_NOTES:
            raise ValueError("session_context_sections exceeds the orientation note limit")
        for path, sections in value.items():
            if (
                not path
                or "\\" in path
                or path.startswith("/")
                or ":" in path
                or ".." in path.split("/")
            ):
                raise ValueError("session_context_sections requires vault-relative note paths")
            if len(sections) > SESSION_MAX_SECTIONS:
                raise ValueError("session_context_sections exceeds the per-note section limit")
            seen: set[tuple[str, ...]] = set()
            for heading_path in sections:
                if not 1 <= len(heading_path) <= SESSION_MAX_HEADING_DEPTH or any(
                    not heading.strip() or len(heading) > SESSION_MAX_HEADING_CHARS
                    for heading in heading_path
                ):
                    raise ValueError("session_context_sections contains an invalid heading path")
                identity = tuple(heading_path)
                if identity in seen:
                    raise ValueError("session_context_sections contains duplicate heading paths")
                seen.add(identity)
        return value

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
    contradiction_max_sections: int = Field(default=DEFAULT_CONTRADICTION_MAX_SECTIONS, ge=1)
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
    max_read_response_chars: int = Field(
        default=DEFAULT_MAX_READ_RESPONSE_CHARS, ge=MIN_READ_RESPONSE_CHARS
    )
    redact_secrets: str = DEFAULT_REDACT_SECRETS
    secret_redaction_patterns: Annotated[list[str], NoDecode] = Field(default_factory=list)
    read_only: bool = False
    durability: str = DEFAULT_DURABILITY_MODE
    tool_description_profile: str = DEFAULT_TOOL_DESCRIPTION_PROFILE
    tool_profile: str = DEFAULT_TOOL_PROFILE
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
    def _parse_write_paths(cls, value: object, info: ValidationInfo) -> list[Path]:
        if value is None or isinstance(value, str):
            return _split_path_list(value)
        if isinstance(value, list):
            return _split_path_list(value)
        raise TypeError(f"Unsupported type for {info.field_name}: {type(value).__name__}")

    @field_validator("vault_root", "memory_root", "runbook_vault", mode="before")
    @classmethod
    def _expand_vault_root(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser().resolve()

    @field_validator("extra_read_vaults", mode="before")
    @classmethod
    def _parse_extra_read_vaults(cls, value: object) -> dict[str, Path]:
        return _parse_extra_read_vaults_value(value)

    @model_validator(mode="after")
    def _refuse_overlapping_roots(self) -> Settings:
        validate_root_topology(self.vault_root, self.memory_root, self.extra_read_vaults)
        return self

    @model_validator(mode="after")
    def _extend_read_paths_with_extra_vaults(self) -> Settings:
        if self.read_paths and self.extra_read_vaults:
            merged = list(self.read_paths)
            for extra_root in self.extra_read_vaults.values():
                if extra_root not in merged:
                    merged.append(extra_root)
            object.__setattr__(self, "read_paths", merged)
        return self

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
            if len(pattern) > DEFAULT_REGEX_FALLBACK_MAX_PATTERN_LENGTH:
                raise ValueError(f"secret redaction regex exceeds max length: {pattern!r}")
            if has_catastrophic_regex_shape(pattern):
                raise ValueError(
                    f"secret redaction regex rejected as potentially catastrophic: {pattern!r}"
                )
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

    @field_validator("tool_profile", mode="before")
    @classmethod
    def _normalize_tool_profile(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_TOOL_PROFILES:
            raise ValueError(f"DATACRON_TOOL_PROFILE must be one of {sorted(VALID_TOOL_PROFILES)}")
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
