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
"""Source-side argument refusals of ``move_note_section``.

The destination side is covered by the integration tests; the source side re-raises
the shared validator's plain ``ValueError`` and names no selector.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from datacron.core.config import Settings
from datacron.core.frontmatter import serialize
from datacron.core.hashing import sha256_bytes
from datacron.core.markdown_sections import SectionSelectorError
from datacron.core.paths import sidecar_index_db
from datacron.mcp.server import build_app
from datacron.mcp.tools.section_move import _move_note_section_impl, _selector

_HASH = "0" * 64


@pytest.mark.parametrize(
    ("heading", "level", "message"),
    [
        ("", None, "heading must contain nonempty single-line text"),
        ("   ", None, "heading must contain nonempty single-line text"),
        ("Item\nMore", None, "heading must contain nonempty single-line text"),
        ("Item\rMore", None, "heading must contain nonempty single-line text"),
        ("Item", 0, "heading level must be an integer between 1 and 6"),
        ("Item", 7, "heading level must be an integer between 1 and 6"),
        ("Item", True, "heading level must be an integer between 1 and 6"),
    ],
)
def test_source_selector_refusals_keep_the_shared_message(
    heading: str, level: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$") as excinfo:
        _selector(heading, level, None, _HASH)

    assert not isinstance(excinfo.value, SectionSelectorError)


def test_source_selector_strips_and_accepts_every_level() -> None:
    for level in range(1, 7):
        assert _selector("  Item  ", level, None, _HASH) == "Item"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"rel_path": "note.txt"}, "rel_path must end with .md"),
        ({"rel_path": "note.md.bak"}, "rel_path must end with .md"),
        ({"heading": ""}, "heading must contain nonempty single-line text"),
        ({"heading": "Item\nArchive"}, "heading must contain nonempty single-line text"),
        ({"heading_level": 9}, "heading level must be an integer between 1 and 6"),
    ],
)
async def test_source_refusals_never_touch_the_note(
    tmp_path: Path, arguments: dict[str, Any], message: str
) -> None:
    raw = serialize(
        {"id": "01J00000000000000000000091"}, "# Root\n\n### Item\n\n## Archive\n"
    ).encode()
    path = tmp_path / "note.md"
    path.write_bytes(raw)
    app = build_app(
        settings=Settings(vault_root=tmp_path, read_paths=[tmp_path], write_paths=[tmp_path]),
        vault_root=tmp_path,
    )
    await app.store.open(sidecar_index_db(tmp_path))
    try:
        result = await _move_note_section_impl(
            app,
            **{
                "rel_path": "note.md",
                "heading": "Item",
                "destination_heading": "Archive",
                "expected_hash": sha256_bytes(raw),
                **arguments,
            },
        )

        assert result["error"]["message"] == message
        assert "selector" not in result["error"]
        assert path.read_bytes() == raw
        assert not await app.vault_writer.list_operations()
    finally:
        await app.store.close()
