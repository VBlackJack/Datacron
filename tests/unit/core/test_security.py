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
"""Tests for deterministic secret redaction."""

from __future__ import annotations

import pytest

from datacron.core.config import Settings
from datacron.core.security import REDACTED, SecretRedactor


def test_conservative_policy_is_default() -> None:
    settings = Settings()
    assert settings.redact_secrets == "all"
    assert SecretRedactor.log_enabled(settings)
    assert SecretRedactor.retrieval_enabled(settings)


@pytest.mark.parametrize(
    ("raw", "visible"),
    [
        ("password: correct-horse-battery", "password: [REDACTED]"),
        ("Authorization: Bearer abcdefghijklmnop", "Authorization: Bearer [REDACTED]"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz", "token [REDACTED]"),
        ("fingerprint=12:34:56:78", "fingerprint=[REDACTED]"),
    ],
)
def test_default_detector_redacts_secret_values(raw: str, visible: str) -> None:
    assert SecretRedactor().redact_text(raw) == visible


def test_sensitive_mapping_key_redacts_unlabelled_value() -> None:
    redacted = SecretRedactor().redact_value({"api_key": "plain-value", "label": "safe"})
    assert redacted == {"api_key": REDACTED, "label": "safe"}


def test_custom_detector_pattern_is_configurable() -> None:
    redactor = SecretRedactor((r"CUSTOM-[0-9]{4}",))
    assert redactor.redact_text("value CUSTOM-1234") == f"value {REDACTED}"


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="DATACRON_REDACT_SECRETS"):
        Settings(redact_secrets="sometimes")
