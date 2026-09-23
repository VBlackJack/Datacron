# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for :mod:`datacron.core.paths`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from datacron.core.config import Settings
from datacron.core.paths import (
    PathConfinementError,
    assert_vault_rel_path,
    assert_within_paths,
    assert_within_read_paths,
    assert_within_write_paths,
    is_within,
    read_ulid_sidecar_strict,
    sidecar_dir,
    sidecar_index_db,
    sidecar_index_dir,
    sidecar_vault_config,
    strip_extended_length_prefix,
)


class TestIsWithin:
    def test_match(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b.txt"
        nested.parent.mkdir(parents=True)
        nested.write_text("x", encoding="utf-8")
        assert is_within(nested, tmp_path)

    def test_outside(self, tmp_path: Path) -> None:
        outside = tmp_path / "a"
        sibling = tmp_path / "b"
        outside.mkdir()
        sibling.mkdir()
        assert not is_within(sibling, outside)


class TestAssertWithinPaths:
    def test_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "ok.md"
        target.write_text("hi", encoding="utf-8")
        resolved = assert_within_paths(target, [tmp_path], kind="read")
        assert resolved == target.resolve()

    def test_rejected(self, tmp_path: Path) -> None:
        outside = Path("/").resolve()
        with pytest.raises(PathConfinementError):
            assert_within_paths(outside, [tmp_path])

    def test_empty_roots(self, tmp_path: Path) -> None:
        with pytest.raises(PathConfinementError):
            assert_within_paths(tmp_path, [])


class TestSettingsBacked:
    def test_read_paths(self, tmp_path: Path) -> None:
        settings = Settings(read_paths=[tmp_path])
        nested = tmp_path / "note.md"
        nested.write_text("x", encoding="utf-8")
        assert assert_within_read_paths(nested, settings=settings) == nested.resolve()

    def test_write_paths_empty_denies_all(self, tmp_path: Path) -> None:
        settings = Settings(write_paths=[])
        with pytest.raises(PathConfinementError, match="No write paths are configured"):
            assert_within_write_paths(tmp_path / "anywhere.md", settings=settings)

    def test_write_paths_allowed(self, tmp_path: Path) -> None:
        settings = Settings(write_paths=[tmp_path])
        nested = tmp_path / "note.md"
        assert assert_within_write_paths(nested, settings=settings) == nested.resolve()

    def test_write_paths_rejected_outside_roots(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        settings = Settings(write_paths=[allowed])
        with pytest.raises(PathConfinementError, match="outside the allowed write roots"):
            assert_within_write_paths(outside / "note.md", settings=settings)


class TestSidecarHelpers:
    def test_layout(self, tmp_path: Path) -> None:
        assert sidecar_dir(tmp_path) == tmp_path.resolve() / ".datacron"
        assert sidecar_index_dir(tmp_path) == tmp_path.resolve() / ".datacron" / "index"
        assert (
            sidecar_index_db(tmp_path) == tmp_path.resolve() / ".datacron" / "index" / "datacron.db"
        )
        assert sidecar_vault_config(tmp_path) == tmp_path.resolve() / ".datacron" / "VAULT.yaml"


class TestReadUlidSidecarStrict:
    """The compare-and-set reader returns the exact bytes and refuses ambiguous files."""

    def test_returns_raw_bytes_and_string_pairs(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "ulids.json"
        raw = b'{"a.md": "01J5S0C0000000000000000001", "b.md": "01J5S0C0000000000000000002"}'
        sidecar.write_bytes(raw)

        raw_bytes, mapping = read_ulid_sidecar_strict(sidecar, max_bytes=1024)

        assert raw_bytes == raw
        assert mapping == {
            "a.md": "01J5S0C0000000000000000001",
            "b.md": "01J5S0C0000000000000000002",
        }

    @pytest.mark.parametrize(
        ("content", "fragment"),
        [
            (b"", "must not be empty"),
            (b'{"a.md": "x", "a.md": "y"}', "duplicate JSON object key"),
            (b'{"a.md": NaN}', "non-finite JSON constant"),
            (b'{"a.md": 1}', "only string pairs"),
            (b'["a.md"]', "only string pairs"),
            (b'{"a.md": "\xff"}', "not strict UTF-8"),
        ],
    )
    def test_refuses_ambiguous_or_malformed_content(
        self, tmp_path: Path, content: bytes, fragment: str
    ) -> None:
        sidecar = tmp_path / "ulids.json"
        sidecar.write_bytes(content)

        with pytest.raises(ValueError, match=fragment):
            read_ulid_sidecar_strict(sidecar, max_bytes=1024)

    def test_refuses_a_file_over_the_byte_bound(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "ulids.json"
        sidecar.write_bytes(b'{"a.md": "' + b"x" * 64 + b'"}')

        with pytest.raises(ValueError, match="limit is 16"):
            read_ulid_sidecar_strict(sidecar, max_bytes=16)

    def test_refuses_a_missing_or_non_regular_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a regular file"):
            read_ulid_sidecar_strict(tmp_path / "missing.json", max_bytes=1024)
        with pytest.raises(ValueError, match="not a regular file"):
            read_ulid_sidecar_strict(tmp_path, max_bytes=1024)


class TestAssertVaultRelPath:
    """The screen that must refuse an escape before it can become I/O."""

    @pytest.mark.parametrize(
        "rel_path",
        [
            "",
            "note.md",
            "folder/note.md",
            "folder/sub/note.md",
            "dotted.name.md",
            "a folder with spaces/note.md",
            "accents-eaeiou/note.md",
        ],
    )
    def test_accepts_an_ordinary_vault_path(self, rel_path: str) -> None:
        assert assert_vault_rel_path(rel_path) == rel_path

    @pytest.mark.parametrize(
        ("rel_path", "fragment"),
        [
            ("//evil.example.com/share/x.md", "UNC share"),
            ("\\\\evil.example.com\\share\\x.md", "UNC share"),
            ("C:/Windows/win.ini", "absolute or name a drive"),
            ("C:note.md", "absolute or name a drive"),
            ("/etc/passwd.md", "must not be absolute"),
            ("../outside.md", "traverse directories"),
            ("folder/../../outside.md", "traverse directories"),
            ("folder/../outside.md", "traverse directories"),
            ("note\x00.md", "control characters"),
            ("note\n.md", "control characters"),
        ],
    )
    def test_refuses_an_escape(self, rel_path: str, fragment: str) -> None:
        with pytest.raises(PathConfinementError, match=fragment):
            assert_vault_rel_path(rel_path)

    @pytest.mark.parametrize(
        "rel_path", ["facts /note.md", "facts./note.md", "folder/trailing /note.md"]
    )
    def test_refuses_a_component_windows_would_rewrite(
        self, rel_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        with pytest.raises(PathConfinementError, match="end with a dot or a space"):
            assert_vault_rel_path(rel_path)

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_a_trailing_dot_is_an_ordinary_name_elsewhere(
        self, platform: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Applied everywhere, the Win32 rule dropped "Clients/Acme Inc./" from every listing."""
        monkeypatch.setattr(sys, "platform", platform)

        assert assert_vault_rel_path("Clients/Acme Inc./meeting.md") == (
            "Clients/Acme Inc./meeting.md"
        )


class TestStripExtendedLengthPrefix:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("\\\\?\\C:\\vault\\note.md", "C:\\vault\\note.md"),
            ("\\\\?\\UNC\\server\\share\\note.md", "\\\\server\\share\\note.md"),
        ],
    )
    def test_removes_the_prefix_windows_resolution_adds(self, raw: str, expected: str) -> None:
        assert str(strip_extended_length_prefix(Path(raw))) == expected

    def test_leaves_an_ordinary_path_untouched(self, tmp_path: Path) -> None:
        assert strip_extended_length_prefix(tmp_path) == tmp_path
