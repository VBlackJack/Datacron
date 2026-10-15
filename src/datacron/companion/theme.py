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
"""Dracula-inspired high-contrast dark theme tokens and WCAG 2.1 contrast verification."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

__all__ = [
    "DARK_THEME_COLORS",
    "LAYOUT_TOKENS",
    "WCAG_AA_NON_TEXT_RATIO",
    "WCAG_AA_TEXT_RATIO",
    "contrast_ratio",
    "relative_luminance",
    "verify_theme_wcag_aa",
]

WCAG_AA_TEXT_RATIO: Final[float] = 4.5
WCAG_AA_NON_TEXT_RATIO: Final[float] = 3.0

DARK_THEME_COLORS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "background": "#282A36",
        "surface": "#21222C",
        "card": "#44475A",
        "highlight": "#343746",
        "accent": "#BD93F9",
        "accent_hover": "#CAA9FA",
        "accent_pressed": "#A67AF7",
        "text_primary": "#F8F8F2",
        "text_secondary": "#BFC4D6",
        "border": "#7D92D2",
        "focus": "#8BE9FD",
        "success": "#50FA7B",
        "warning": "#FFB86C",
        "error": "#FF5555",
        "info": "#8BE9FD",
    }
)

LAYOUT_TOKENS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "nav_rail_width": "220px",
        "card_radius": "10px",
        "control_radius": "8px",
        "card_padding": "20px",
        "page_padding": "24px 28px",
        "control_min_height": "40px",
        "font_sans": "'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif",
        "font_mono": "Consolas, 'Cascadia Mono', monospace",
    }
)

_HEX_COLOR_CHARS: Final[int] = 6
_LINEAR_THRESHOLD: Final[float] = 0.03928
_LINEAR_DIVISOR: Final[float] = 12.92
_GAMMA_OFFSET: Final[float] = 0.055
_GAMMA_DIVISOR: Final[float] = 1.055
_GAMMA_EXPONENT: Final[float] = 2.4


def _channel_to_linear(value_8bit: int) -> float:
    normalized = value_8bit / 255.0
    if normalized <= _LINEAR_THRESHOLD:
        return normalized / _LINEAR_DIVISOR
    return float(((normalized + _GAMMA_OFFSET) / _GAMMA_DIVISOR) ** _GAMMA_EXPONENT)


def relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance for a `#RRGGBB` hex color."""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != _HEX_COLOR_CHARS:
        raise ValueError(f"Expected 6-digit hex color, got {hex_color!r}")
    r = _channel_to_linear(int(cleaned[0:2], 16))
    g = _channel_to_linear(int(cleaned[2:4], 16))
    b = _channel_to_linear(int(cleaned[4:6], 16))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground_hex: str, background_hex: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two `#RRGGBB` hex colors."""
    lum_a = relative_luminance(foreground_hex)
    lum_b = relative_luminance(background_hex)
    lighter = max(lum_a, lum_b)
    darker = min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def verify_theme_wcag_aa() -> dict[str, float]:
    """Verify all primary/secondary text and border pairs meet WCAG 2.1 AA thresholds."""
    c = DARK_THEME_COLORS
    checks = {
        "text_primary_on_background": contrast_ratio(c["text_primary"], c["background"]),
        "text_primary_on_surface": contrast_ratio(c["text_primary"], c["surface"]),
        "text_primary_on_card": contrast_ratio(c["text_primary"], c["card"]),
        "text_secondary_on_background": contrast_ratio(c["text_secondary"], c["background"]),
        "text_secondary_on_surface": contrast_ratio(c["text_secondary"], c["surface"]),
        "text_secondary_on_card": contrast_ratio(c["text_secondary"], c["card"]),
        "border_on_background": contrast_ratio(c["border"], c["background"]),
        "focus_on_background": contrast_ratio(c["focus"], c["background"]),
    }
    for name, ratio in checks.items():
        threshold = (
            WCAG_AA_NON_TEXT_RATIO if name.startswith(("border_", "focus_")) else WCAG_AA_TEXT_RATIO
        )
        if ratio < threshold:
            raise ValueError(f"WCAG AA contrast failure for {name}: {ratio:.2f} < {threshold}")
    return checks
