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
"""Tests for the Markdown chunker."""

from __future__ import annotations

import pytest

from datacron.indexing.chunker import _slug_header_path


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        ([], ""),
        (["Architecture", "Chunking strategy"], "architecture/chunking-strategy"),
        (["Café déjà vu", "Résumé & notes"], "cafe-deja-vu/resume-notes"),
        (["  API: v2.1 / MCP  ", "FTS5 + BM25"], "api-v2-1-mcp/fts5-bm25"),
        (["Repeated---punctuation", "A__B"], "repeated-punctuation/a-b"),
    ],
)
def test_slug_header_path_follows_contract_rules(
    headings: list[str],
    expected: str,
) -> None:
    assert _slug_header_path(headings) == expected
