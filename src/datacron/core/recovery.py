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
"""Typed contracts for fail-closed operation recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

__all__ = [
    "BlockedOperation",
    "RecoveryRepairAction",
    "RecoveryRepairResult",
]

RecoveryRepairAction: TypeAlias = Literal["restore-before", "adopt-disk"]


@dataclass(frozen=True)
class BlockedOperation:
    """Sanitized operation evidence that requires explicit operator repair."""

    operation_id: str
    rel_path: str
    reason: str
    expected_before_hash: str | None
    expected_after_hash: str
    disk_hash: str | None
    restore_before_available: bool
    adopt_disk_available: bool


@dataclass(frozen=True)
class RecoveryRepairResult:
    """Durable evidence returned after one confirmed recovery repair."""

    operation_id: str
    repair_operation_id: str
    rel_path: str
    action: RecoveryRepairAction
    before_hash: str
    after_hash: str
