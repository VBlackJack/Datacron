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
"""Unit tests for the zero-dependency internationalization catalog loader."""

from __future__ import annotations

from typing import Any

import pytest

from datacron.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, get_section, load_locale, t


def _leaf_keys(mapping: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in mapping.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.update(_leaf_keys(value, path))
        else:
            keys.add(path)
    return keys


def test_locales_have_matching_leaf_keys() -> None:
    """Every supported locale catalog must define the exact same leaf keys."""
    baseline = _leaf_keys(load_locale(DEFAULT_LOCALE))
    assert baseline
    for locale in SUPPORTED_LOCALES:
        assert _leaf_keys(load_locale(locale)) == baseline


def test_get_section_returns_localized_library_strings() -> None:
    """Section lookup returns localized dictionaries for English and French."""
    en_library = get_section("library", locale="en")
    fr_library = get_section("library", locale="fr")
    assert en_library["home"] == "Knowledge library"
    assert fr_library["home"] == "Bibliothèque de connaissances"
    assert set(en_library) == set(fr_library)


def test_t_formats_named_placeholders_and_rejects_unknown_locale() -> None:
    """t() formats placeholders and rejects unsupported locales or non-string sections."""
    rendered_en = t("library.folder_title", locale="en", subject="Architecture")
    rendered_fr = t("library.folder_title", locale="fr", subject="Architecture")
    assert rendered_en == "Navigation: Architecture"
    assert rendered_fr == "Navigation : Architecture"

    with pytest.raises(ValueError, match="Unsupported locale"):
        load_locale("de")

    with pytest.raises(KeyError):
        t("library")

    with pytest.raises(KeyError):
        t("nonexistent.key")
