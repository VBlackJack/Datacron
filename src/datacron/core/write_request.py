# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Request identity propagated into the existing durable note transaction."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from datacron.core.operation_log import OperationRecord

__all__ = ["ACTIVE_WRITE_REQUEST", "ReplayedWriteError", "WriteRequest"]


@dataclass
class WriteRequest:
    """Content-free request identity and its committed receipt."""

    key_hash: str
    fingerprint: str
    record: OperationRecord | None = None


class ReplayedWriteError(Exception):
    """Stop before mutation when a request already has a durable receipt."""

    def __init__(self, record: OperationRecord) -> None:
        super().__init__("write request was already committed")
        self.record = record


ACTIVE_WRITE_REQUEST: ContextVar[WriteRequest | None] = ContextVar(
    "datacron_write_request", default=None
)
