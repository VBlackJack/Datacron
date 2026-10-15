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
"""English and French labels for persistent, plugin-free library pages."""

from __future__ import annotations

from typing import Final

from datacron.i18n import SUPPORTED_LOCALES, get_section

TEXT: Final[dict[str, dict[str, str]]] = {
    locale: get_section("library", locale=locale) for locale in SUPPORTED_LOCALES
}
