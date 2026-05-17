# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Smoke tests for the shared fixtures exposed in ``tests/conftest.py``.

These cover the public surface that Codex's indexing and eval tests are
allowed to rely on. If we change a fixture signature, these tests fail
loudly before Codex sees the breakage.
"""

from __future__ import annotations

from collections.abc import Callable

from datacron.core.hashing import hash_text
from datacron.core.models import Chunk, ChunkType, Note

NoteFactory = Callable[..., Note]
ChunkFactory = Callable[..., Chunk]


def test_note_factory_default(note_factory: NoteFactory) -> None:
    note = note_factory()
    assert isinstance(note, Note)
    assert len(note.id) == 26
    assert note.rel_path == "note.md"
    assert note.title == "Test Note"
    assert note.content_hash == hash_text(note.raw_content)


def test_note_factory_recomputes_hash(note_factory: NoteFactory) -> None:
    note = note_factory(raw_content="# Custom\n\nBody.\n")
    assert note.raw_content == "# Custom\n\nBody.\n"
    assert note.content_hash == hash_text("# Custom\n\nBody.\n")


def test_note_factory_honors_overrides(note_factory: NoteFactory) -> None:
    note = note_factory(
        rel_path="folder/sub.md",
        title="Sub",
        tags=["foo"],
    )
    assert note.rel_path == "folder/sub.md"
    assert note.title == "Sub"
    assert note.tags == ["foo"]


def test_chunk_factory_default(chunk_factory: ChunkFactory) -> None:
    chunk = chunk_factory()
    assert isinstance(chunk, Chunk)
    assert chunk.chunk_type is ChunkType.NARRATIVE
    assert chunk.ordinal == 0
    assert chunk.chunk_id.endswith("::::0000")


def test_chunk_factory_bound_to_note(
    note_factory: NoteFactory, chunk_factory: ChunkFactory
) -> None:
    note = note_factory(rel_path="foo/bar.md")
    chunk = chunk_factory(note=note, header_path="intro", ordinal=3)
    assert chunk.note_id == note.id
    assert chunk.note_rel_path == "foo/bar.md"
    assert chunk.chunk_id == f"{note.id}::intro::0003"
