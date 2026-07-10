# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.hashing`."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from datacron.core.hashing import (
    FRESHNESS_CONTRACT_ID,
    HASH_HEX_LENGTH,
    hash_text,
    normalize_text,
    sha256_bytes,
)

_FRESHNESS_VECTOR_MANIFEST = Path(__file__).parents[2] / "fixtures" / "freshness-contract-v1.json"


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("hello\nworld\n", b"hello\nworld\n"),
            ("hello\r\nworld\r\n", b"hello\nworld\n"),
            ("hello\rworld\r", b"hello\nworld\n"),
            ("mixed\r\nrouge\rok\n", b"mixed\nrouge\nok\n"),
        ],
    )
    def test_line_endings(self, raw: str, expected: bytes) -> None:
        assert normalize_text(raw) == expected

    def test_bom_stripped(self) -> None:
        text = "\ufeffhello"
        assert normalize_text(text) == b"hello"

    def test_unicode_passthrough(self) -> None:
        text = "caf\u00e9 -- na\u00efve"
        assert normalize_text(text) == text.encode("utf-8")


class TestHash:
    def test_hash_length_and_alphabet(self) -> None:
        result = hash_text("anything")
        assert len(result) == HASH_HEX_LENGTH
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_distinguishes_line_endings(self) -> None:
        assert hash_text("hello\nworld") != hash_text("hello\r\nworld")
        assert hash_text("a\rb") != hash_text("a\nb")

    def test_hash_distinguishes_bom(self) -> None:
        assert hash_text("\ufeffhello") != hash_text("hello")

    def test_hash_matches_sha256(self) -> None:
        text = "datacron"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert hash_text(text) == expected

    def test_sha256_bytes(self) -> None:
        assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()

    def test_freshness_contract_vectors_are_byte_exact(self, tmp_path: Path) -> None:
        """The shared contract hashes bytes written without text conversion."""
        manifest = json.loads(_FRESHNESS_VECTOR_MANIFEST.read_text(encoding="utf-8"))
        assert manifest["contract"] == FRESHNESS_CONTRACT_ID

        for vector in manifest["vectors"]:
            payload = base64.b64decode(vector["base64"], validate=True)
            target = tmp_path / f"{vector['id']}.md"
            target.write_bytes(payload)
            assert sha256_bytes(target.read_bytes()) == vector["sha256"]
