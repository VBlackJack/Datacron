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
"""Shared response bounds for MCP payload assembly."""

from __future__ import annotations

__all__ = ["bounded_count"]


def bounded_count(requested: int, ceiling: int) -> int:
    """Apply a positive server ceiling to a requested result count.

    Args:
        requested: Caller-requested count. Non-positive values select the ceiling.
        ceiling: Positive server-side maximum.

    Returns:
        The effective bounded count.
    """
    if requested <= 0:
        return ceiling
    return min(requested, ceiling)
