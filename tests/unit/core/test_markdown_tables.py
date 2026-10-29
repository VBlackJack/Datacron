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
"""Pipe table row upsert inside one Markdown section."""

from __future__ import annotations

import pytest

from datacron.core.markdown_sections import SectionStructureError
from datacron.core.markdown_tables import section_own_text, upsert_table_row

_HEADER = ("Placeholder", "Meaning", "On Mets")
_VARIABLES = (
    "# Variables\n"
    "\n"
    "## Servers\n"
    "\n"
    "| Placeholder | Meaning | On Mets |\n"
    "|---|---|---|\n"
    "| `<UPSTREAM>` | Upstream WSUS server | `tpcpdc03d` |\n"
    "| `<REPLICA>` | Replica WSUS server | `tpcpdc03v` |\n"
    "\n"
    "## Network\n"
    "\n"
    "| Placeholder | Meaning | On Mets |\n"
    "|---|---|---|\n"
    "| `<DOMAIN>` | AD domain | `ts.mets.fr` |\n"
)


def test_a_row_is_replaced_by_its_first_cell() -> None:
    row = ("`<upstream>`", "Upstream WSUS server", "`tpcpdc04d` (measured 2026-10-29)")

    rendered, action = upsert_table_row(_VARIABLES, "Servers", row, _HEADER)

    assert action == "replaced"
    assert (
        "| `<upstream>` | Upstream WSUS server | `tpcpdc04d` (measured 2026-10-29) |\n" in rendered
    )
    assert "tpcpdc03d" not in rendered
    assert rendered.count("\n") == _VARIABLES.count("\n")


def test_an_identical_row_is_unchanged() -> None:
    row = ("`<REPLICA>`", "Replica WSUS server", "`tpcpdc03v`")

    assert upsert_table_row(_VARIABLES, "Servers", row, _HEADER) == (_VARIABLES, "unchanged")


def test_a_new_row_follows_the_last_row_of_its_section_only() -> None:
    row = ("`<LB_A>`", "Load balancer pair", "`lpcpdc01d`")

    rendered, action = upsert_table_row(_VARIABLES, "Servers", row, _HEADER)

    assert action == "appended"
    assert (
        "| `<REPLICA>` | Replica WSUS server | `tpcpdc03v` |\n"
        "| `<LB_A>` | Load balancer pair | `lpcpdc01d` |\n\n## Network"
    ) in rendered
    assert section_own_text(rendered, "Network") == section_own_text(_VARIABLES, "Network")


def test_a_key_in_another_section_is_not_touched() -> None:
    row = ("`<DOMAIN>`", "AD domain", "`other`")

    rendered, action = upsert_table_row(_VARIABLES, "Servers", row, _HEADER)

    assert action == "appended"
    assert "`ts.mets.fr`" in rendered


def test_a_missing_section_is_created_with_its_header() -> None:
    row = ("`<VIP_A>`", "Virtual IP, site A", "`10.46.122.30`")

    rendered, action = upsert_table_row(_VARIABLES, "Load balancing", row, _HEADER)

    assert action == "created"
    assert rendered.endswith(
        "## Load balancing\n\n| Placeholder | Meaning | On Mets |\n| --- | --- | --- |\n"
        "| `<VIP_A>` | Virtual IP, site A | `10.46.122.30` |\n"
    )


def test_a_section_without_table_receives_one() -> None:
    body = "# Variables\n\n## Ports\n\nNo port recorded yet.\n"

    rendered, action = upsert_table_row(
        body, "Ports", ("`<HTTPS_PORT>`", "HTTPS", "`8531`"), _HEADER
    )

    assert action == "appended"
    assert "No port recorded yet.\n\n| Placeholder | Meaning | On Mets |" in rendered


def test_line_ends_of_the_note_are_kept() -> None:
    crlf = _VARIABLES.replace("\n", "\r\n")

    rendered, _action = upsert_table_row(crlf, "Servers", ("`<LB_A>`", "m", "v"), _HEADER)

    assert "| `<LB_A>` | m | v |\r\n" in rendered
    assert "\n" not in rendered.replace("\r\n", "")


def test_a_table_that_ends_the_note_without_a_line_end() -> None:
    body = "## Servers\n\n| A | B | C |\n|---|---|---|\n| `<X>` | x | x |"

    rendered, action = upsert_table_row(body, "Servers", ("`<Y>`", "y", "y"), _HEADER)

    assert action == "appended"
    assert rendered.endswith("| `<X>` | x | x |\n| `<Y>` | y | y |\n")


def test_an_escaped_pipe_stays_inside_its_cell() -> None:
    body = "## Servers\n\n| A | B | C |\n|---|---|---|\n| `<X>` | a \\| b | x |\n"

    rendered, action = upsert_table_row(body, "Servers", ("`<X>`", "a \\| b", "x"), _HEADER)

    assert (rendered, action) == (body, "unchanged")


def test_markup_inside_a_row_cannot_change_the_note_structure() -> None:
    """A line that starts with a pipe never opens a fence or a heading."""
    row = ("`<UPSTREAM>`", "``` <!-- ## not a heading", "x")

    rendered, action = upsert_table_row(_VARIABLES, "Servers", row, _HEADER)

    assert action == "replaced"
    assert section_own_text(rendered, "Network") == section_own_text(_VARIABLES, "Network")


def test_an_edit_that_hides_a_heading_is_refused() -> None:
    body = "## Servers\n\n| A | B | C |\n|---|---|---|\n| `<X>` | x | x |\n"

    with pytest.raises(SectionStructureError):
        upsert_table_row(body, "Servers", ("`<Y>`", "y", "y\n```"), _HEADER)


def test_a_row_must_fit_its_header() -> None:
    with pytest.raises(ValueError, match="as many cells"):
        upsert_table_row(_VARIABLES, "Servers", ("`<X>`",), _HEADER)


def test_section_own_text_stops_at_the_next_heading() -> None:
    assert section_own_text(_VARIABLES, "Missing") is None
    own = section_own_text(_VARIABLES, "Servers")
    assert own is not None
    assert own.startswith("\n| Placeholder")
    assert "## Network" not in own
