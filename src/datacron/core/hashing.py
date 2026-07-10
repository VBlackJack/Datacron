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
"""Exact-byte content hashing and explicit text-normalization utilities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Final

__all__ = [
    "FRESHNESS_CONTRACT_ID",
    "HASH_HEX_LENGTH",
    "hash_text",
    "index_generation_hash",
    "normalize_text",
    "sha256_bytes",
]

FRESHNESS_CONTRACT_ID: Final[str] = "freshness-contract-v1"
HASH_HEX_LENGTH: Final[int] = 64
_BOM: Final[str] = "\ufeff"


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
    """Return SHA-256 of the exact UTF-8 encoding of ``text``.

    This function intentionally preserves BOM, EOL, and Unicode code-point
    differences. Call :func:`normalize_text` explicitly when normalization is
    part of a separate derived-text contract.
    """
    return sha256_bytes(text.encode("utf-8"))


def index_generation_hash(indexed: Mapping[str, tuple[str, str]]) -> str:
    """Hash exact indexed path, identity, and content-hash rows."""
    digest = hashlib.sha256()
    for rel_path, (note_id, content_hash) in sorted(indexed.items()):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(note_id.encode("ascii"))
        digest.update(b"\x00")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
