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
"""Content hashing utilities.

The canonical Datacron content hash is SHA-256 over the UTF-8 bytes of the
text after BOM removal and CRLF/CR → LF normalization, returned as a
lowercase hex string. This is the format consumed by ``Note.content_hash``
and by index freshness checks.
"""

from __future__ import annotations

import hashlib
from typing import Final

__all__ = ["HASH_HEX_LENGTH", "hash_text", "normalize_text", "sha256_bytes"]

HASH_HEX_LENGTH: Final[int] = 64
_BOM: Final[str] = "﻿"


def normalize_text(text: str) -> bytes:
    """Return the UTF-8 bytes of ``text`` with BOM stripped and LF endings.

    Both ``\\r\\n`` and bare ``\\r`` are collapsed to ``\\n``. The text is
    encoded as UTF-8 without a leading BOM.
    """
    stripped = text[1:] if text.startswith(_BOM) else text
    normalized = stripped.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Compute the canonical Datacron content hash for ``text``.

    Equivalent to ``sha256_bytes(normalize_text(text))``.
    """
    return sha256_bytes(normalize_text(text))
