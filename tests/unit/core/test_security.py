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

from pathlib import Path

import pytest

from datacron.core.config import DEFAULT_REDACT_SECRETS, Settings
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


@pytest.mark.parametrize(
    "text",
    [
        '"password": "hunter2"',
        "{'api_key': 'abcd1234efgh'}",
        "DB_PASSWORD=hunter2",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        "export GITHUB_TOKEN=abcdef123456",
        "postgres://admin:S3cretPass@db.example:5432/app",
        "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
        "github_pat_11ABCDEFG0123456789abcdefghij",
        "sk_live_abcdefghijklmnop1234",
        "xoxb-1234567890-abcdefghij",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "glpat-abcdefghij1234567890",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnop",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nxyz\n-----END PGP PRIVATE KEY BLOCK-----",
        "mot de passe : hunter2",
        "mdp: hunter2",
        "secret_key: w00t",
    ],
)
def test_common_secret_shapes_are_redacted(text: str) -> None:
    """Shapes the documentation claims, each of which used to pass through untouched."""
    redacted = SecretRedactor().redact_text(text)

    assert REDACTED in redacted
    for secret in ("hunter2", "S3cretPass", "abcd1234efgh", "w00t", "xyz"):
        assert secret not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens: 1024",
        "token_count: 5",
        "The tokenizer: whitespace",
        "https://example.com/path",
        "see [[Password Policy]] for rules",
    ],
)
def test_ordinary_text_near_secret_words_is_kept(text: str) -> None:
    assert SecretRedactor().redact_text(text) == text


def test_compound_sensitive_keys_are_redacted_in_structures() -> None:
    redacted = SecretRedactor().redact_value(
        {"DB_PASSWORD": "x", "secret_key": "w", "max_tokens": 5}
    )

    assert redacted == {"DB_PASSWORD": REDACTED, "secret_key": REDACTED, "max_tokens": 5}


def test_a_dotenv_file_in_the_working_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a stdio server the working directory is whatever the client opened."""
    (tmp_path / ".env").write_text("DATACRON_REDACT_SECRETS=off\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATACRON_REDACT_SECRETS", raising=False)

    assert Settings().redact_secrets == DEFAULT_REDACT_SECRETS
