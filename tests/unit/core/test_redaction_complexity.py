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
"""The secret detectors stay linear on long runs of name characters."""

from __future__ import annotations

import time
from typing import Final

import pytest

from datacron.core.security import REDACTED, SecretRedactor

# 96 KB of one repeated unit took 45 s in the key-name detector; a linear scan
# takes milliseconds. The bound is generous so a loaded machine cannot flake it.
_INPUT_BYTES: Final[int] = 96_000
_MAX_SECONDS: Final[float] = 1.0


@pytest.mark.parametrize("unit", ["token_", "a_", "a-", "password-", "x.y+"])
def test_redaction_of_a_long_run_is_linear(unit: str) -> None:
    text = unit * (_INPUT_BYTES // len(unit))
    started = time.perf_counter()
    SecretRedactor().redact_text(text)
    assert time.perf_counter() - started < _MAX_SECONDS


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("--token=abcdef123", "abcdef123"),
        ("DB__PASSWORD: hunter22", "hunter22"),
        ("_secret_key = s3cr3tvalue", "s3cr3tvalue"),
        ('"github_token": "ghx-value"', "ghx-value"),
        ("1-https://user:pa55word@example.invalid", "pa55word"),
    ],
)
def test_keys_starting_inside_a_run_are_still_detected(text: str, secret: str) -> None:
    redacted = SecretRedactor().redact_text(text)
    assert secret not in redacted
    assert REDACTED in redacted
