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
"""Whether a vault's filesystem folds path case, measured rather than assumed.

Path keys used to fold case only when ``os.name == "nt"``. The default macOS
filesystem is case-insensitive too, so there two spellings of one note were
treated as two paths; and a case-sensitive volume mounted on Windows was
folded although it keeps them apart. The answer is a property of the volume
the vault lives on, so it is probed there, once per root.

Code that builds path keys deep inside one operation reads the answer from
:func:`fold_path_key`, which follows the root entered with
:func:`case_folding_for`; outside such a block it falls back to the platform's
default filesystem.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Final

__all__ = [
    "case_folding_for",
    "filesystem_folds_case",
    "fold_path_key",
    "platform_folds_case",
]

# Platforms whose default filesystem (NTFS, APFS) is case-insensitive.
_CASE_INSENSITIVE_PLATFORMS: Final[frozenset[str]] = frozenset({"win32", "darwin"})
# Entries under the root inspected for a name that has a cased letter.
_PROBE_ENTRY_LIMIT: Final[int] = 64
# Distinct vault roots whose probe result is remembered.
_PROBE_CACHE_SIZE: Final[int] = 32

_ACTIVE_FOLDING: ContextVar[bool | None] = ContextVar("datacron_case_folding", default=None)


def platform_folds_case() -> bool:
    """Return the fallback answer: whether this platform's default filesystem folds case."""
    return sys.platform in _CASE_INSENSITIVE_PLATFORMS


def _swapped_twin_is_same(entry: Path) -> bool | None:
    """Compare ``entry`` with its swapped-case spelling; ``None`` when undecidable."""
    swapped = entry.name.swapcase()
    if swapped == entry.name:
        return None
    try:
        return os.path.samefile(entry, entry.with_name(swapped))
    except FileNotFoundError:
        return False
    except OSError:
        return None


@lru_cache(maxsize=_PROBE_CACHE_SIZE)
def _probe(root_text: str) -> bool | None:
    root = Path(root_text)
    answer = _swapped_twin_is_same(root)
    if answer is not None:
        return answer
    with suppress(OSError):
        for entry in islice(root.iterdir(), _PROBE_ENTRY_LIMIT):
            answer = _swapped_twin_is_same(entry)
            if answer is not None:
                return answer
    return None


def filesystem_folds_case(root: Path) -> bool:
    """Return whether the filesystem holding ``root`` treats path case as equal.

    The root, then its first entries, are looked up under a swapped-case
    spelling; the first name with a cased letter decides. With no such name,
    the platform default applies.
    """
    probed = _probe(str(root.expanduser().resolve()))
    return platform_folds_case() if probed is None else probed


@contextmanager
def case_folding_for(root: Path) -> Iterator[bool]:
    """Make :func:`fold_path_key` follow ``root``'s filesystem inside the block."""
    token = _ACTIVE_FOLDING.set(filesystem_folds_case(root))
    try:
        yield _ACTIVE_FOLDING.get() is True
    finally:
        _ACTIVE_FOLDING.reset(token)


def fold_path_key(value: str) -> str:
    """Casefold ``value`` when the active vault's filesystem folds case."""
    active = _ACTIVE_FOLDING.get()
    folds = platform_folds_case() if active is None else active
    return value.casefold() if folds else value
