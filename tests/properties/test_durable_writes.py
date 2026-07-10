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
"""Property guards for the durable note-write core."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from datacron.core.config import Settings, VaultConfig
from datacron.core.frontmatter import parse, serialize
from datacron.core.vault import FilesystemVaultReader
from datacron.core.vault_writer import (
    FAULT_POINTS,
    FilesystemVaultWriter,
    atomic_durable_write,
)

pytestmark = pytest.mark.invariants

_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", blacklist_characters=("\x00",)),
    max_size=80,
)
_INLINE_TEXT = st.text(
    alphabet=st.characters(
        codec="utf-8",
        blacklist_characters=("\r", "\n"),
    ),
    max_size=80,
)
_T = TypeVar("_T")
_SUPPRESS_FIXTURE_CHECK = [HealthCheck.function_scoped_fixture]


def _writer(vault: Path, *, line_endings: str = "lf") -> FilesystemVaultWriter:
    return FilesystemVaultWriter(
        vault,
        Settings(write_paths=[vault]),
        VaultConfig(line_endings=line_endings),
    )


def _run(coroutine: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coroutine)


def _normalize_eol(text: str, eol: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if eol == "\n" else normalized.replace("\n", eol)


@settings(max_examples=20, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(content=_TEXT, policy=st.sampled_from(("lf", "crlf")))
def test_prop_01_write_read_identity(tmp_path: Path, content: str, policy: str) -> None:
    """PROP-01: a write/read round trip changes only the declared EOL policy."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = vault / "identity.md"
    target.unlink(missing_ok=True)
    writer = _writer(vault, line_endings=policy)
    expected_eol = "\n" if policy == "lf" else "\r\n"
    expected = _normalize_eol(content, expected_eol)

    returned_hash = _run(writer.write_note_atomic("identity.md", content, overwrite=False))
    note = _run(FilesystemVaultReader(vault).read_note(target))

    assert note.raw_content == expected
    assert target.read_bytes() == expected.encode("utf-8")
    assert returned_hash == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize("fault_point", FAULT_POINTS)
@settings(max_examples=5, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(old=_TEXT, new=_TEXT)
def test_prop_05_crash_atomicity(
    tmp_path: Path,
    old: str,
    new: str,
    fault_point: str,
) -> None:
    """PROP-05: every injected crash leaves the complete old or new bytes."""
    target = tmp_path / "atomic.md"
    old_bytes = old.encode("utf-8")
    new_bytes = new.encode("utf-8")
    target.write_bytes(old_bytes)

    def crash_at(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"injected crash at {point}")

    with pytest.raises(RuntimeError, match=re.escape(f"injected crash at {fault_point}")):
        atomic_durable_write(target, new_bytes, fault_injector=crash_at)

    assert target.read_bytes() in {old_bytes, new_bytes}
    assert not list(tmp_path.glob(".atomic.md.*.tmp"))


@settings(max_examples=10, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(bodies=st.lists(_TEXT, min_size=2, max_size=6, unique=True))
def test_prop_06_concurrent_writes_no_corruption(tmp_path: Path, bodies: list[str]) -> None:
    """PROP-06: concurrent complete writes serialize without byte corruption."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = vault / "concurrent.md"
    initial = serialize({"id": "01HQXR7K9YZ8M2N3PQRSTV4WX5"}, "initial")
    target.write_bytes(initial.encode("utf-8"))
    writer = _writer(vault)
    candidates = [
        serialize(
            {"id": "01HQXR7K9YZ8M2N3PQRSTV4WX5", "title": f"candidate-{index}"},
            body,
        )
        for index, body in enumerate(bodies)
    ]

    async def write_all() -> None:
        await asyncio.gather(
            *(
                writer.write_note_atomic("concurrent.md", content, overwrite=True)
                for content in candidates
            )
        )

    _run(write_all())
    final_bytes = target.read_bytes()
    final_text = final_bytes.decode("utf-8", errors="strict")
    metadata, _body = parse(final_text)

    assert final_text in [_normalize_eol(candidate, "\n") for candidate in candidates]
    assert metadata["id"] == "01HQXR7K9YZ8M2N3PQRSTV4WX5"
    assert b"\x00" not in final_bytes


@settings(max_examples=16, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(
    source_eol=st.sampled_from(("\n", "\r\n")),
    raw_text=_INLINE_TEXT,
)
def test_prop_12_encoding_roundtrip(
    tmp_path: Path,
    source_eol: str,
    raw_text: str,
) -> None:
    """PROP-12: touched notes keep one EOL and preserve significant Unicode."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = vault / "encoding.md"
    significant = "NBSP:\u00a0 ZWSP:\u200b NFC:\u00e9"
    normalized_text = unicodedata.normalize("NFC", raw_text)
    original = _normalize_eol(f"line one\n{significant}\n{normalized_text}", source_eol)
    target.write_bytes(original.encode("utf-8"))
    writer = _writer(vault, line_endings="crlf" if source_eol == "\n" else "lf")

    _run(writer.mutate_note_atomic("encoding.md", lambda current: f"{current}\nnext"))
    final = target.read_bytes().decode("utf-8", errors="strict")

    assert significant in final
    assert unicodedata.normalize("NFC", normalized_text) in final
    if source_eol == "\r\n":
        assert "\r\n" in final
        assert "\n" not in final.replace("\r\n", "")
    else:
        assert "\r" not in final
        assert "\n" in final


@settings(max_examples=20, deadline=None, suppress_health_check=_SUPPRESS_FIXTURE_CHECK)
@given(content=_TEXT, policy=st.sampled_from(("lf", "crlf")))
def test_prop_13_hash_matches_disk(tmp_path: Path, content: str, policy: str) -> None:
    """PROP-13: the returned hash is SHA-256 of post-fsync disk bytes."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "hash.md").unlink(missing_ok=True)
    writer = _writer(vault, line_endings=policy)

    returned_hash = _run(writer.write_note_atomic("hash.md", content, overwrite=False))
    disk_bytes = (vault / "hash.md").read_bytes()

    assert returned_hash == hashlib.sha256(disk_bytes).hexdigest()
