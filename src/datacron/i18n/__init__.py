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
"""Zero-dependency internationalization catalog loader and lookup helpers."""

from __future__ import annotations

import functools
import json
from importlib import resources
from typing import Any, Final

__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "get_section",
    "load_locale",
    "t",
]

DEFAULT_LOCALE: Final[str] = "en"
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("en", "fr")
_LOCALES_PACKAGE: Final[str] = "datacron.i18n.locales"


@functools.lru_cache(maxsize=len(SUPPORTED_LOCALES))
def load_locale(locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    """Load and cache one JSON locale catalog by language code."""
    normalized = locale.strip().lower()
    if normalized not in SUPPORTED_LOCALES:
        raise ValueError(f"Unsupported locale {locale!r}; expected one of {SUPPORTED_LOCALES}.")
    resource = resources.files(_LOCALES_PACKAGE).joinpath(f"{normalized}.json")
    raw = resource.read_text(encoding="utf-8")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"Locale catalog {normalized}.json must be a JSON object.")
    return loaded


def _resolve_node(catalog: dict[str, Any], dotted_key: str) -> object:
    current: object = catalog
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted_key)
        current = current[part]
    return current


def get_section(section: str, *, locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    """Return a shallow string dictionary for a dotted section path."""
    node = _resolve_node(load_locale(locale), section)
    if not isinstance(node, dict):
        raise KeyError(f"Locale section {section!r} is not a mapping.")
    return {str(key): str(value) for key, value in node.items()}


def t(key: str, *, locale: str = DEFAULT_LOCALE, **kwargs: object) -> str:
    """Translate a dotted key and format optional named placeholders."""
    try:
        value = _resolve_node(load_locale(locale), key)
    except KeyError:
        if locale == DEFAULT_LOCALE:
            raise
        value = _resolve_node(load_locale(DEFAULT_LOCALE), key)
    if not isinstance(value, str):
        raise KeyError(f"Translation key {key!r} does not resolve to a string.")
    return value.format(**kwargs) if kwargs else value
