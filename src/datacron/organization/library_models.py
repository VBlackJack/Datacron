# Copyright 2026 Julien Bombled
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
"""Contracts for offline navigation and explicitly sourced editorial proposals."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from datacron.core.config import DEFAULT_STATE_NOTE_NAMESPACE
from datacron.core.memory_protocol import SESSION_DOMAIN_TAGS

LIBRARY_SCHEMA = "human-library-v1"
MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = "snapshot.json"
PREVIEW_DIRECTORY = "preview"
REPORT_NAME = "review.md"
# Every other file of a review bundle, named once so the writer and the checker agree.
PAYLOADS_DIRECTORY: Final[str] = "payloads"
CHANGES_NAME: Final[str] = "changes.diff"
AUDIT_NAME: Final[str] = "audit.json"
RECIPE_NAME: Final[str] = "recipe.json"
SUBJECT_TEMPLATE_NAME: Final[str] = "subject-template.md"
EDITORIAL_RECIPE_MAX_NOTES: Final[int] = 64
# Working defaults for a vault that declares nothing: the state-note kinds hang off the
# shared state-note namespace and the people tag is the session "people" domain tag.
DEFAULT_STATE_NOTE_KINDS: Final[tuple[str, ...]] = ("platform", "development", "mission")
DEFAULT_PROCEDURE_TAGS: Final[tuple[str, ...]] = (
    "memory/procedure",
    "memory/reference",
    "memory/howto",
)


class LibraryOptions(BaseModel):
    """User-selected scope, language and bounded readability thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: str
    home: str
    tags: list[str]
    language: Literal["en", "fr"] = "en"
    areas: dict[str, str] = Field(default_factory=dict)
    folder_labels: dict[str, str] = Field(default_factory=dict)
    max_note_chars: int = Field(default=24000, ge=1)
    max_section_chars: int = Field(default=8000, ge=1)
    max_notes: int = Field(default=10000, ge=1)
    max_note_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    max_attachment_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    max_export_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    state_tags: list[str] = Field(
        default_factory=lambda: [
            f"{DEFAULT_STATE_NOTE_NAMESPACE}/{kind}" for kind in DEFAULT_STATE_NOTE_KINDS
        ]
    )
    people_tags: list[str] = Field(default_factory=lambda: [SESSION_DOMAIN_TAGS["people"]])
    procedure_tags: list[str] = Field(default_factory=lambda: list(DEFAULT_PROCEDURE_TAGS))


class SourceReference(BaseModel):
    """An exact source revision, never an assertion of semantic correctness."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditorialNote(BaseModel):
    """A human-reviewable synthesis or split section with retained originals."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    sources: list[SourceReference] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    archive_sources: list[str] = Field(default_factory=list)


class EditorialRecipe(BaseModel):
    """Explicit editorial input; preparation does not approve or apply it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    notes: list[EditorialNote] = Field(min_length=1, max_length=EDITORIAL_RECIPE_MAX_NOTES)


class Finding(BaseModel):
    """A measured issue or explicitly labelled review candidate."""

    code: str
    path: str
    detail: str
    link_style: Literal["markdown", "wiki"] | None = None


class LibraryAudit(BaseModel):
    """A scoped inventory, with no inference that old means obsolete."""

    schema_version: str = LIBRARY_SCHEMA
    scope: str
    notes: int
    source_hashes: dict[str, str]
    findings: list[Finding]
    coverage: str = "admitted_notes_in_scope; external_links_not_fetched"
