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
"""Deterministic secret detection for retrieval and logging boundaries."""

# ruff: noqa: S105 - this module contains detector regexes, never credentials

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from datacron.core.config import Settings

__all__ = ["REDACTED", "SecretRedactor"]

REDACTED: Final[str] = "[REDACTED]"

_LABELLED_SECRET: Final[str] = (
    r"(?i)(?P<prefix>\b(?:password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|fingerprint|thumbprint)"
    r"\b\s*(?::|=|\bis\b)\s*)"
    r"(?P<secret>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_BEARER_SECRET: Final[str] = r"(?i)(?P<prefix>\bBearer\s+)(?P<secret>[A-Za-z0-9._~+/-]{12,}={0,2})"
_KNOWN_TOKEN: Final[str] = (
    r"(?P<secret>\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16})\b)"
)
_SLUGGED_SECRET: Final[str] = (
    r"(?i)(?P<prefix>\b(?:password|passwd|pwd|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|fingerprint|thumbprint)-)"
    r"(?P<secret>[A-Za-z0-9][A-Za-z0-9._-]{3,})"
)
_PEM_PRIVATE_KEY: Final[str] = (
    r"(?s)(?P<secret>-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----)"
)
_DEFAULT_PATTERNS: Final[tuple[str, ...]] = (
    _PEM_PRIVATE_KEY,
    _LABELLED_SECRET,
    _SLUGGED_SECRET,
    _BEARER_SECRET,
    _KNOWN_TOKEN,
)
_SENSITIVE_KEY: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|fingerprint|thumbprint)"
)


class SecretRedactor:
    """Redact likely secret values without interpreting vault content."""

    def __init__(self, custom_patterns: Sequence[str] = ()) -> None:
        self._patterns = tuple(re.compile(pattern) for pattern in _DEFAULT_PATTERNS)
        self._custom_patterns = tuple(re.compile(pattern) for pattern in custom_patterns)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretRedactor:
        """Build the detector from validated runtime configuration."""
        return cls(settings.secret_redaction_patterns)

    def redact_text(self, value: str) -> str:
        """Return ``value`` with detected secret values replaced."""
        redacted = value
        for pattern in self._patterns:
            redacted = pattern.sub(self._replace_secret_group, redacted)
        for pattern in self._custom_patterns:
            redacted = pattern.sub(self._replace_custom_match, redacted)
        return redacted

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings while preserving JSON-like structure."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                key: REDACTED
                if isinstance(key, str) and _SENSITIVE_KEY.fullmatch(key.strip())
                else self.redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        return value

    @staticmethod
    def _replace_secret_group(match: re.Match[str]) -> str:
        start, end = match.span("secret")
        relative_start = start - match.start()
        relative_end = end - match.start()
        matched = match.group(0)
        return f"{matched[:relative_start]}{REDACTED}{matched[relative_end:]}"

    @staticmethod
    def _replace_custom_match(match: re.Match[str]) -> str:
        if "secret" not in match.re.groupindex:
            return REDACTED
        return SecretRedactor._replace_secret_group(match)

    @staticmethod
    def log_enabled(settings: Settings) -> bool:
        """Return whether runtime logs must apply optional redaction."""
        return settings.redact_secrets in {"log", "all"}

    @staticmethod
    def retrieval_enabled(settings: Settings) -> bool:
        """Return whether MCP retrieval payloads must apply redaction."""
        return settings.redact_secrets in {"retrieval", "all"}
