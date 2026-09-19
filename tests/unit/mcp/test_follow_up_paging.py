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
"""Tests for how get_follow_up sizes one page against the token budget."""

from __future__ import annotations

from typing import Any

import pytest

from datacron.mcp.tools import follow_up_read
from datacron.mcp.tools.follow_up_read import _page
from datacron.mcp.tools.session import rendered_size


class TestFollowUpPageSizing:
    """Sizing a page must not cost one serialization of it per record dropped."""

    @staticmethod
    def _records(count: int) -> list[dict[str, Any]]:
        filler = "commitment text that makes a record about one kilobyte long. " * 16
        return [
            {
                "record_id": f"rec-{index:05d}",
                "note_rel_path": "people/someone.md",
                "status": "open",
                "owner": "someone",
                "due": "2026-12-31",
                "text": f"{filler} {index}",
            }
            for index in range(count)
        ]

    @staticmethod
    def _shrink_one_at_a_time(
        records: list[dict[str, Any]], legacy: int, offset: int, snapshot: str, maximum: int
    ) -> dict[str, Any]:
        """The loop the binary search replaces, kept to pin the two against each other."""
        page = records[offset:]
        output: dict[str, Any] = {
            "records": page,
            "returned": len(page),
            "total": len(records),
            "offset": offset,
            "next_offset": None,
            "snapshot_hash": snapshot,
            "legacy_notes": legacy,
            "coverage": "explicit_notes_structured_entries_only",
            "truncated": offset > 0,
            "omitted": 0,
        }
        while rendered_size(output) > maximum and page:
            page.pop()
            output.update(
                returned=len(page),
                truncated=True,
                omitted=len(records) - offset - len(page),
                next_offset=offset + len(page),
            )
        return output

    @pytest.mark.parametrize("offset", [0, 7])
    @pytest.mark.parametrize("maximum", [4000, 32000, 400000])
    def test_the_page_is_the_one_the_shrinking_loop_produced(
        self, maximum: int, offset: int
    ) -> None:
        """Halving is only legitimate if it lands on the same record.

        The budget values span a page of a handful of records, the default page, and
        a budget the whole set fits inside, where the contract says `next_offset` is
        null and nothing is omitted.
        """
        records = self._records(60)

        produced = _page(list(records), 0, offset, "snap", maximum)
        expected = self._shrink_one_at_a_time(list(records), 0, offset, "snap", maximum)

        assert produced == expected
        assert produced["returned"] > 0

    def test_sizing_a_page_does_not_serialise_it_once_per_record(self) -> None:
        """The work must follow the log of the records, not the records.

        Each measurement re-serialises the page it is measuring, so dropping one
        record at a time paid one serialisation of the whole remaining page per
        record dropped. Nothing bounds how many records a note holds: a canonical
        person or project note that accumulated a few hundred commitments turned a
        read into seconds, once per page, on the synchronous path. Measured before
        this change, 2000 records took 12.5 seconds to size one page of 27.

        Counting serialisations rather than time makes the bound immune to the
        machine, and twentyfold more records must not cost threefold more work.
        """
        counts: list[int] = []
        for count in (100, 2000):
            records = self._records(count)
            calls: list[int] = []

            def counting(payload: object, seen: list[int] = calls) -> int:
                seen.append(1)
                return rendered_size(payload)

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(follow_up_read, "rendered_size", counting)
                page = _page(records, 0, 0, "snap", 32000)
            assert page["returned"] == 27
            counts.append(len(calls))

        assert counts[1] < counts[0] * 3, (
            f"{counts[0]} serialisations for 100 records, {counts[1]} for 2000"
        )
