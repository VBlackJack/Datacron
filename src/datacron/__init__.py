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
"""Datacron - local-first MCP server for Markdown vaults.

Public API surface kept intentionally narrow. Most consumers should import
from :mod:`datacron.core.models` for the frozen Pydantic types, or use the
``datacron`` CLI.
"""

from __future__ import annotations

# Calendar Versioning: YYYY.MMDD.XX (UTC year, zero-padded month+day, same-day
# build counter starting at 00). Single source of truth; pyproject reads it via
# hatch's dynamic version.
__version__ = "2026.0720.00"

__all__ = ["__version__"]
