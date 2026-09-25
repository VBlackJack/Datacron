# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Frontmatter bytes a write tool did not mean to touch must survive it.

Every write tool edits the note's own header text key by key. These tests pin
the cases that used to leave that path: a block list, a removed key, an
anchor anywhere in the header, an empty or block scalar value, and a note whose
closing delimiter is its last byte.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import parse, serialize
from datacron.core.hashing import hash_text
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import DatacronApp, build_app
from datacron.mcp.tools import (
    _append_journal_impl,
    _delete_note_section_impl,
    _get_note_impl,
    _patch_note_preamble_impl,
    _patch_note_section_impl,
    _rename_note_section_impl,
    _set_frontmatter_impl,
)

_REL_PATH = "note.md"
_NOTE_ID = "01J00000000000000000000151"

# Values YAML 1.1 reads as something else than what Obsidian shows, a comment,
# an anchor with its alias, and the two block lists create_note_ai writes.
_HAND_WRITTEN_HEADER = (
    "---\n"
    f"id: {_NOTE_ID}\n"
    "title: Hand written\n"
    "# owner: keep this reviewed every quarter\n"
    "meeting: 14:30\n"
    "zip: 01234\n"
    "flag: NO\n"
    "version: 1.10\n"
    "owner: &o alice\n"
    "reviewer: *o\n"
    "supersedes:\n"
    "  - a.md\n"
    "rejected:\n"
    "  - old idea -- because\n"
    "updated: '2026-01-01T00:00:00+00:00'\n"
    "tail: kept # inline comment\n"
    "---\n"
)
_BODY = "# Hand written\n\nIntro.\n\n## Log\n\nentry\n\n## Other\n\nmore\n"

Operation = Callable[[DatacronApp, str], Awaitable[dict[str, Any]]]


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[DatacronApp]:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(vault_root=vault, read_paths=[vault], write_paths=[vault])
    built = build_app(settings=settings, vault_root=vault)
    await built.store.open(sidecar_index_db(vault))
    try:
        yield built
    finally:
        await built.store.close()


def _note(app: DatacronApp) -> Path:
    assert app.settings.vault_root is not None
    return app.settings.vault_root / _REL_PATH


def _write(app: DatacronApp, raw: str) -> str:
    _note(app).write_bytes(raw.encode("utf-8"))
    return hash_text(raw)


def _read(app: DatacronApp) -> str:
    return _note(app).read_bytes().decode("utf-8")


def _header(raw: str) -> str:
    return raw.split("---\n")[1]


def _without_entries(header: str, keys: set[str]) -> list[str]:
    """Return the header lines, minus each named key and its indented continuation."""
    kept: list[str] = []
    dropping = False
    for line in header.splitlines():
        if line[:1] in {" ", "\t"} and dropping:
            continue
        dropping = line.split(":", 1)[0] in keys
        if not dropping:
            kept.append(line)
    return kept


_OPERATIONS: dict[str, tuple[Operation, set[str]]] = {
    "set_frontmatter extends a block list": (
        lambda app, expected: _set_frontmatter_impl(
            app, rel_path=_REL_PATH, supersedes=["a.md", "b.md"], expected_hash=expected
        ),
        {"supersedes"},
    ),
    "set_frontmatter removes a key": (
        lambda app, expected: _set_frontmatter_impl(
            app, rel_path=_REL_PATH, rejected=[], expected_hash=expected
        ),
        {"rejected"},
    ),
    "set_frontmatter adds a key": (
        lambda app, expected: _set_frontmatter_impl(
            app, rel_path=_REL_PATH, confidence="low", expected_hash=expected
        ),
        {"confidence"},
    ),
    "set_frontmatter with last_id and a block list": (
        lambda app, expected: _set_frontmatter_impl(
            app,
            rel_path=_REL_PATH,
            last_id="BL-0002",
            rejected=["new idea -- because"],
            expected_hash=expected,
        ),
        {"last_id", "rejected"},
    ),
    "append_journal": (
        lambda app, expected: _append_journal_impl(
            app, rel_path=_REL_PATH, heading="Log", entry="new", expected_hash=expected
        ),
        set(),
    ),
    "patch_note_section": (
        lambda app, expected: _patch_note_section_impl(
            app, rel_path=_REL_PATH, heading="Log", new_content="replaced", expected_hash=expected
        ),
        set(),
    ),
    "delete_note_section": (
        lambda app, expected: _delete_note_section_impl(
            app, rel_path=_REL_PATH, heading="Other", expected_hash=expected
        ),
        set(),
    ),
    "rename_note_section": (
        lambda app, expected: _rename_note_section_impl(
            app, rel_path=_REL_PATH, heading="Log", new_heading="Journal", expected_hash=expected
        ),
        set(),
    ),
    "patch_note_preamble": (
        lambda app, expected: _patch_note_preamble_impl(
            app, rel_path=_REL_PATH, new_content="New intro.", expected_hash=expected
        ),
        set(),
    ),
}


@pytest.mark.parametrize("name", list(_OPERATIONS))
async def test_untouched_frontmatter_keys_keep_their_bytes(app: DatacronApp, name: str) -> None:
    """Only the keys an edit changes are re-rendered; every other byte stays.

    When the key-by-key edit was refused, the fallback re-dumped the whole
    header through PyYAML: ``14:30`` became ``870``, ``01234`` became ``668``,
    ``NO`` became ``false``, ``1.10`` became ``1.1`` and the comment went. A
    block list (the style create_note_ai writes, so every second edit), a key
    removal, or an anchor anywhere in the header sent every tool there.
    """
    operation, touched = _OPERATIONS[name]
    before = _HAND_WRITTEN_HEADER + _BODY
    expected = _write(app, before)
    original_metadata, _ = parse(before)

    result = await operation(app, expected)

    assert "error" not in result, result
    after = _read(app)
    dropped = {*touched, "updated"}
    assert _without_entries(_header(after), dropped) == _without_entries(_header(before), dropped)
    metadata, _ = parse(after)
    assert metadata["updated"] != original_metadata["updated"]
    for key in original_metadata.keys() - dropped:
        assert metadata[key] == original_metadata[key], key


async def test_a_block_list_edit_keeps_block_style(app: DatacronApp) -> None:
    expected = _write(app, _HAND_WRITTEN_HEADER + _BODY)

    result = await _set_frontmatter_impl(
        app, rel_path=_REL_PATH, supersedes=["a.md", "b.md"], expected_hash=expected
    )

    assert "error" not in result, result
    assert "supersedes:\n  - a.md\n  - b.md\nrejected:\n" in _read(app)


async def test_a_removed_key_takes_its_continuation_lines_and_nothing_else(
    app: DatacronApp,
) -> None:
    expected = _write(app, _HAND_WRITTEN_HEADER + _BODY)

    result = await _set_frontmatter_impl(
        app, rel_path=_REL_PATH, rejected=[], expected_hash=expected
    )

    assert "error" not in result, result
    after = _read(app)
    assert "rejected" not in after
    assert "old idea" not in after
    assert "supersedes:\n  - a.md\nupdated: '" in after


@pytest.mark.parametrize(
    ("header", "operation"),
    [
        (
            "updated: &u '2026-01-01T00:00:00+00:00'\nreviewed: *u\n",
            lambda app, expected: _append_journal_impl(
                app, rel_path=_REL_PATH, heading="Log", entry="new", expected_hash=expected
            ),
        ),
        (
            "owner: &o alice\nconfidence: *o\n",
            lambda app, expected: _set_frontmatter_impl(
                app, rel_path=_REL_PATH, confidence="low", expected_hash=expected
            ),
        ),
        (
            "confidence: &c high\nowner: *c\n",
            lambda app, expected: _set_frontmatter_impl(
                app, rel_path=_REL_PATH, confidence="low", expected_hash=expected
            ),
        ),
    ],
)
async def test_an_edit_to_an_anchored_key_is_refused_not_rewritten(
    app: DatacronApp, header: str, operation: Operation
) -> None:
    """An anchor or alias on the edited key cannot be edited precisely; say so."""
    before = f"---\nid: {_NOTE_ID}\nmeeting: 14:30\n{header}---\n# N\n\n## Log\n\nentry\n"
    expected = _write(app, before)

    result = await operation(app, expected)

    assert result["error"]["code"] == "frontmatter_edit_refused", result
    assert result["error"]["next_action"]
    assert _read(app) == before


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
async def test_a_note_ending_on_its_closing_delimiter_keeps_its_metadata(
    app: DatacronApp, eol: str
) -> None:
    """The new body was glued to ``---`` and the note lost its id, title and tags."""
    before = eol.join(["---", f"id: {_NOTE_ID}", "title: Stub", "tags: [keep]", "---"])
    _write(app, before)

    result = await _append_journal_impl(app, rel_path=_REL_PATH, heading="Log", entry="first")

    assert "error" not in result, result
    after = _read(app)
    assert f"{eol}---{eol}## Log" in after
    note = await _get_note_impl(app, id_or_path=_REL_PATH, fmt="full")
    assert (note["id"], note["title"], note["tags"]) == (_NOTE_ID, "Stub", ["keep"])


_APPEND: Operation = lambda app, expected: _append_journal_impl(  # noqa: E731
    app, rel_path=_REL_PATH, heading="Log", entry="new entry", expected_hash=expected
)
_SET_CONFIDENCE: Operation = lambda app, expected: _set_frontmatter_impl(  # noqa: E731
    app, rel_path=_REL_PATH, confidence="low", expected_hash=expected
)


@pytest.mark.parametrize(
    ("entry", "operation", "key", "value"),
    [
        ("updated:\n", _APPEND, "updated", None),
        ("updated: >-\n  2026-01-01\n", _APPEND, "updated", None),
        ("confidence: |\n  high\n  more\n", _SET_CONFIDENCE, "confidence", "low"),
        ("confidence:\n", _SET_CONFIDENCE, "confidence", "low"),
        ("confidence: # to fill\n", _SET_CONFIDENCE, "confidence", "low"),
    ],
)
async def test_empty_and_block_scalar_values_are_replaced_whole(
    app: DatacronApp, entry: str, operation: Operation, key: str, value: str | None
) -> None:
    """A blank ``updated:`` (a common Obsidian template) made every write fail.

    The value span of an empty node starts at the next key and a block scalar's
    span ends after its newline, so the splice broke the header; the parser
    error escaped as a FrontmatterError instead of a refusal.
    """
    before = f"---\nid: {_NOTE_ID}\ntitle: T\n{entry}status: x\n---\n# T\n\n## Log\n\na\n"
    expected = _write(app, before)

    result = await operation(app, expected)

    assert "error" not in result, result
    after = _read(app)
    assert after.startswith(f"---\nid: {_NOTE_ID}\ntitle: T\n{key}: ")
    assert "\nstatus: x\n" in after
    assert after.endswith("---\n# T\n\n## Log\n\na\n") or "new entry" in after
    metadata, _ = parse(after)
    assert metadata["status"] == "x"
    assert metadata["id"] == _NOTE_ID
    if value is not None:
        assert metadata[key] == value
    else:
        assert isinstance(metadata[key], str)
        assert metadata[key].startswith("20")


async def test_an_unclosed_opening_delimiter_keeps_the_body_whitespace(app: DatacronApp) -> None:
    """A never-closed ``---`` returned a stripped body, so an edit dropped the final newline."""
    before = "---\nIntro para\n\n## A\n\na\n\n## B\n\nb\n  \n"
    expected = _write(app, before)

    result = await _patch_note_section_impl(
        app, rel_path=_REL_PATH, heading="A", new_content="NEW", expected_hash=expected
    )

    assert "error" not in result, result
    after = _read(app)
    assert after.endswith("---\nIntro para\n\n## A\n\nNEW\n\n## B\n\nb\n  \n")


async def test_the_empty_mapping_serialize_writes_takes_new_keys(app: DatacronApp) -> None:
    """``serialize({})`` writes ``{}``; appending a key to it must not be refused."""
    before = serialize({}, "# Project\n\n## Log\n")
    expected = _write(app, before)

    result = await _append_journal_impl(
        app, rel_path=_REL_PATH, heading="Log", entry="first", expected_hash=expected
    )

    assert "error" not in result, result
    after = _read(app)
    assert after.startswith("---\nupdated: '")
    assert "{}" not in after
    assert after.endswith("---\n# Project\n\n## Log\n\nfirst\n")
