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
"""Datacron Companion graphical control panel package."""

from __future__ import annotations

from datacron.companion.server import (
    DEFAULT_COMPANION_HOST,
    TOKEN_HEADER_NAME,
    CompanionHTTPServer,
    create_companion_server,
    launch_companion_window,
)
from datacron.companion.theme import (
    DARK_THEME_COLORS,
    LAYOUT_TOKENS,
    WCAG_AA_NON_TEXT_RATIO,
    WCAG_AA_TEXT_RATIO,
    contrast_ratio,
    relative_luminance,
    verify_theme_wcag_aa,
)

__all__ = [
    "DARK_THEME_COLORS",
    "DEFAULT_COMPANION_HOST",
    "LAYOUT_TOKENS",
    "TOKEN_HEADER_NAME",
    "WCAG_AA_NON_TEXT_RATIO",
    "WCAG_AA_TEXT_RATIO",
    "CompanionHTTPServer",
    "contrast_ratio",
    "create_companion_server",
    "launch_companion_window",
    "relative_luminance",
    "verify_theme_wcag_aa",
]
