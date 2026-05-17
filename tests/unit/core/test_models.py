# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the frozen contract in ``datacron.core.models``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from datacron.core.models import (
    Chunk,
    ChunkType,
    EvalQuestion,
    EvalResult,
    IndexStats,
    Note,
    SearchResult,
    Wikilink,
)

VALID_ULID = "01HQXR7K9YZ8M2N3PQRSTV4WX5"
VALID_HASH = "a" * 64


def _build_note(**overrides: object) -> Note:
    defaults: dict[str, object] = {
        "id": VALID_ULID,
        "path": Path("vault") / "welcome.md",
        "rel_path": "welcome.md",
        "title": "Welcome",
        "frontmatter": {"title": "Welcome"},
        "content": "# Welcome\n",
        "raw_content": "---\ntitle: Welcome\n---\n\n# Welcome\n",
        "created": datetime(2026, 5, 17, tzinfo=UTC),
        "updated": datetime(2026, 5, 17, tzinfo=UTC),
        "content_hash": VALID_HASH,
        "tags": ["welcome"],
        "aliases": [],
    }
    defaults.update(overrides)
    return Note.model_validate(defaults)


def _build_chunk(**overrides: object) -> Chunk:
    defaults: dict[str, object] = {
        "chunk_id": f"{VALID_ULID}::intro::0000",
        "note_id": VALID_ULID,
        "note_rel_path": "welcome.md",
        "header_path": "intro",
        "section_title": "Intro",
        "chunk_type": ChunkType.NARRATIVE,
        "content": "Hello world",
        "ordinal": 0,
        "content_hash": VALID_HASH,
        "token_count": 3,
        "line_start": 1,
        "line_end": 1,
    }
    defaults.update(overrides)
    return Chunk.model_validate(defaults)


class TestNote:
    def test_valid_note(self) -> None:
        note = _build_note()
        assert note.id == VALID_ULID
        assert note.title == "Welcome"

    def test_note_is_frozen(self) -> None:
        note = _build_note()
        with pytest.raises(ValidationError):
            setattr(note, "title", "Other")  # noqa: B010

    def test_invalid_ulid_length(self) -> None:
        with pytest.raises(ValidationError):
            _build_note(id="too-short")

    def test_invalid_hash_pattern(self) -> None:
        with pytest.raises(ValidationError):
            _build_note(content_hash="ZZZ")

    def test_invalid_uppercase_hash(self) -> None:
        with pytest.raises(ValidationError):
            _build_note(content_hash="A" * 64)

    def test_defaults(self) -> None:
        note = _build_note(frontmatter={}, tags=[], aliases=[])
        assert note.frontmatter == {}
        assert note.tags == []
        assert note.aliases == []


class TestChunk:
    def test_valid_chunk(self) -> None:
        chunk = _build_chunk()
        assert chunk.chunk_type is ChunkType.NARRATIVE
        assert chunk.ordinal == 0

    def test_negative_ordinal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build_chunk(ordinal=-1)

    def test_negative_token_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build_chunk(token_count=-1)

    def test_invalid_line_ranges_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build_chunk(line_start=0)
        with pytest.raises(ValidationError):
            _build_chunk(line_end=0)

    def test_lang_optional(self) -> None:
        chunk = _build_chunk(chunk_type=ChunkType.CODE, lang="python")
        assert chunk.lang == "python"

    def test_chunk_is_frozen(self) -> None:
        chunk = _build_chunk()
        with pytest.raises(ValidationError):
            setattr(chunk, "ordinal", 5)  # noqa: B010


class TestChunkType:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("FRONTMATTER", "frontmatter"),
            ("NARRATIVE", "narrative"),
            ("HEADING", "heading"),
            ("CODE", "code"),
            ("TABLE", "table"),
            ("LIST", "list"),
            ("QUOTE", "quote"),
        ],
    )
    def test_values(self, name: str, value: str) -> None:
        assert ChunkType[name].value == value


class TestWikilink:
    def test_minimal(self) -> None:
        wl = Wikilink(source_chunk_id="c1", target_alias="Welcome")
        assert wl.resolved_note_id is None
        assert wl.display_text is None

    def test_full(self) -> None:
        wl = Wikilink(
            source_chunk_id="c1",
            target_alias="Welcome",
            resolved_note_id=VALID_ULID,
            display_text="Hi",
            header_anchor="intro",
            block_ref="abc",
        )
        assert wl.resolved_note_id == VALID_ULID


class TestSearchResult:
    def test_round_trip(self) -> None:
        chunk = _build_chunk()
        sr = SearchResult(chunk=chunk, score=0.42, snippet="**hello** world")
        assert sr.score == 0.42
        assert sr.snippet.startswith("**hello**")


class TestIndexStats:
    def test_round_trip(self) -> None:
        stats = IndexStats(
            note_count=3,
            chunk_count=12,
            last_indexed_at=datetime(2026, 5, 17, tzinfo=UTC),
            db_size_bytes=1024,
            db_path=Path("datacron.db"),
        )
        assert stats.chunk_count == 12

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IndexStats(
                note_count=-1,
                chunk_count=0,
                db_size_bytes=0,
                db_path=Path("x.db"),
            )


class TestEvalModels:
    def test_question_defaults(self) -> None:
        q = EvalQuestion(id="q1", question="What is x?")
        assert q.expected_chunk_ids == []
        assert q.expected_paths == []

    def test_result_clamped_precision(self) -> None:
        with pytest.raises(ValidationError):
            EvalResult(
                question_id="q1",
                retrieved_chunk_ids=[],
                citation_precision=1.5,
                latency_ms=10.0,
                tokens_returned=0,
            )

    def test_result_valid(self) -> None:
        result = EvalResult(
            question_id="q1",
            retrieved_chunk_ids=["c1", "c2"],
            recall_at_k={5: 0.5, 10: 0.8},
            citation_precision=0.5,
            latency_ms=12.5,
            tokens_returned=200,
            trust_label="high",
        )
        assert result.recall_at_k[10] == 0.8
