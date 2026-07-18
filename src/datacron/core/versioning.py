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
"""Calendar-version normalization shared by release tooling and invariants."""

from __future__ import annotations

import re
from datetime import date
from typing import Final

__all__ = ["normalize_calver"]

_CALVER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<year>\d{4})\.(?P<month>\d{2})(?P<day>\d{2})\.(?P<counter>\d{2})$"
)


def normalize_calver(version: str) -> str:
    """Return the PEP 440 canonical form of a ``YYYY.MMDD.XX`` CalVer.

    Leading zeros are stripped from each release segment, for example
    ``2026.0718.03`` becomes ``2026.718.3``.

    Raises:
        ValueError: If ``version`` is not a valid ``YYYY.MMDD.XX`` calendar version.
    """
    match = _CALVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid Datacron CalVer {version!r}; expected YYYY.MMDD.XX")

    year = int(match["year"])
    month = int(match["month"])
    day = int(match["day"])
    counter = int(match["counter"])
    try:
        date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid Datacron CalVer date in {version!r}") from exc
    mmdd = int(f"{month:02d}{day:02d}")
    return f"{year}.{mmdd}.{counter}"
