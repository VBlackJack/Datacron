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
"""Tests for pure Markdown section helpers."""

from __future__ import annotations

import pytest


def test_rename_atx_heading_line_preserves_prefix_level_separator_and_eol() -> None:
    from datacron.core.markdown_sections import rename_atx_heading_line

    assert rename_atx_heading_line("   ###\t  Old title\r\n", "New title") == (
        "   ###\t  New title\r\n"
    )


def test_rename_atx_heading_line_refuses_non_heading() -> None:
    from datacron.core.markdown_sections import rename_atx_heading_line

    with pytest.raises(ValueError, match="ATX heading line"):
        rename_atx_heading_line("plain text\n", "New title")
