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
"""The build step that clears the executable-stack request works on real ELF layouts."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "clear_execstack.py"

_PT_LOAD = 1
_PF_R = 4
_PF_W = 2
_PHENTSIZE = 56
_PHOFF = 64
_ELF_BIG_ENDIAN = 2
_ELFCLASS32 = 1
_ET_EXEC = 2
_TRUNCATED_ENTRY = 6


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("clear_execstack", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_script()


def _elf64(
    *headers: tuple[int, int],
    class_byte: int = 2,
    data_byte: int = 1,
    e_type: int = _mod.ET_DYN,
) -> bytes:
    """A minimal ELF64 little-endian image with the given (p_type, p_flags) headers."""
    ident = b"\x7fELF" + bytes([class_byte, data_byte, 1]) + bytes(9)
    header = bytearray(ident)
    header += bytes(_PHOFF - len(header))
    struct.pack_into("<H", header, 0x10, e_type)
    struct.pack_into("<Q", header, 0x20, _PHOFF)
    struct.pack_into("<H", header, 0x36, _PHENTSIZE)
    struct.pack_into("<H", header, 0x38, len(headers))
    body = bytearray()
    for p_type, p_flags in headers:
        entry = bytearray(_PHENTSIZE)
        struct.pack_into("<II", entry, 0, p_type, p_flags)
        body += entry
    return bytes(header + body)


def _rwe_stack() -> bytes:
    return _elf64((_PT_LOAD, _PF_R), (_mod.PT_GNU_STACK, _PF_R | _PF_W | _mod.PF_X))


def _clean_stack() -> bytes:
    return _elf64((_PT_LOAD, _PF_R), (_mod.PT_GNU_STACK, _PF_R | _PF_W))


def test_exec_stack_flag_is_cleared_and_only_that_field_changes(tmp_path: Path) -> None:
    target = tmp_path / "libpython.so"
    original = _rwe_stack()
    target.write_bytes(original)

    assert _mod.clear_exec_stack(target) is True

    patched = target.read_bytes()
    assert len(patched) == len(original)
    assert not _mod.requests_exec_stack(target)
    differing = [i for i, (a, b) in enumerate(zip(original, patched, strict=True)) if a != b]
    assert len(differing) == 1


def test_file_without_the_flag_stays_byte_identical(tmp_path: Path) -> None:
    target = tmp_path / "clean.so"
    target.write_bytes(_clean_stack())
    before = target.read_bytes()
    assert _mod.clear_exec_stack(target) is False
    assert target.read_bytes() == before


def test_elf_without_a_stack_header_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "nostack.so"
    target.write_bytes(_elf64((_PT_LOAD, _PF_R)))
    assert _mod.clear_exec_stack(target) is False


@pytest.mark.parametrize(
    "content",
    [
        b"plain text, not an ELF",
        b"",
        b"\x7fELF",
        _elf64((_mod.PT_GNU_STACK, _mod.PF_X), class_byte=_ELFCLASS32),
        _elf64((_mod.PT_GNU_STACK, _mod.PF_X), data_byte=_ELF_BIG_ENDIAN),
        _elf64((_PT_LOAD, 0), (_mod.PT_GNU_STACK, _mod.PF_X))[
            : _PHOFF + _PHENTSIZE + _TRUNCATED_ENTRY
        ],
    ],
    ids=["text", "empty", "magic-only", "elf32", "big-endian", "truncated"],
)
def test_non_matching_files_are_skipped_untouched(tmp_path: Path, content: bytes) -> None:
    target = tmp_path / "other.bin"
    target.write_bytes(content)
    assert _mod.clear_exec_stack(target) is False
    assert target.read_bytes() == content


def test_clear_walks_directories_and_skips_symlinks(tmp_path: Path) -> None:
    nested = tmp_path / "lib" / "deep"
    nested.mkdir(parents=True)
    real = nested / "libpython.so"
    real.write_bytes(_rwe_stack())
    outside = tmp_path / "outside.so"
    outside.write_bytes(_rwe_stack())
    link = tmp_path / "lib" / "link.so"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    assert _mod.main(["clear", str(tmp_path / "lib")]) == 0

    assert not _mod.requests_exec_stack(real)
    assert _mod.requests_exec_stack(outside)


def _toc(tmp_path: Path, entries: list[tuple[str, str, str]]) -> Path:
    toc = tmp_path / "PKG-00.toc"
    guts: tuple[object, ...] = (
        "datacron",
        {},
        entries,
        "libpython3.11.so",
        False,
        False,
        False,
        [],
        None,
        None,
        None,
    )
    assert guts[_mod.PKG_GUTS_TOC_INDEX] is entries
    toc.write_text(repr(guts), encoding="utf-8")
    return toc


def test_toc_index_matches_the_installed_pyinstaller() -> None:
    guts = pytest.importorskip("PyInstaller.building.api").PKG._GUTS

    assert [name for name, _check in guts].index("toc") == _mod.PKG_GUTS_TOC_INDEX


def test_check_toc_refuses_the_flat_entry_list_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "libbad.so"
    bad.write_bytes(_rwe_stack())
    toc = tmp_path / "PKG-00.toc"
    toc.write_text(repr([("libbad.so", str(bad), "BINARY")]), encoding="utf-8")
    assert _mod.main(["check-toc", str(toc)]) == 1
    assert "PKG guts tuple" in capsys.readouterr().err


def test_check_toc_fails_on_an_executable_stack_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "libbad.so"
    bad.write_bytes(_rwe_stack())
    toc = _toc(tmp_path, [("libbad.so", str(bad), "BINARY")])
    assert _mod.main(["check-toc", str(toc)]) == 1
    assert "libbad.so" in capsys.readouterr().err


def test_check_toc_passes_on_clean_entries(tmp_path: Path) -> None:
    good = tmp_path / "libgood.so"
    good.write_bytes(_clean_stack())
    data = tmp_path / "notes.txt"
    data.write_text("data", encoding="utf-8")
    toc = _toc(
        tmp_path,
        [("libgood.so", str(good), "BINARY"), ("notes.txt", str(data), "DATA")],
    )
    assert _mod.main(["check-toc", str(toc)]) == 0


def test_check_toc_ignores_non_binary_kinds(tmp_path: Path) -> None:
    good = tmp_path / "libgood.so"
    good.write_bytes(_clean_stack())
    bad = tmp_path / "libbad.so"
    bad.write_bytes(_rwe_stack())
    toc = _toc(
        tmp_path,
        [("libgood.so", str(good), "EXTENSION"), ("libbad.so", str(bad), "DATA")],
    )
    assert _mod.main(["check-toc", str(toc)]) == 0


def test_check_toc_fails_when_no_elf_is_listed(tmp_path: Path) -> None:
    data = tmp_path / "notes.txt"
    data.write_text("data", encoding="utf-8")
    toc = _toc(tmp_path, [("notes.txt", str(data), "BINARY")])
    assert _mod.main(["check-toc", str(toc)]) == 1


def test_check_toc_fails_on_a_shared_object_without_a_stack_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_path / "libnostack.so"
    lib.write_bytes(_elf64((_PT_LOAD, _PF_R)))
    toc = _toc(tmp_path, [("libnostack.so", str(lib), "BINARY")])
    assert _mod.main(["check-toc", str(toc)]) == 1
    assert "no PT_GNU_STACK header" in capsys.readouterr().err


def test_check_toc_skips_an_executable_without_a_stack_header(tmp_path: Path) -> None:
    exe = tmp_path / "tool"
    exe.write_bytes(_elf64((_PT_LOAD, _PF_R), e_type=_ET_EXEC))
    toc = _toc(tmp_path, [("tool", str(exe), "BINARY")])
    assert _mod.main(["check-toc", str(toc)]) == 1  # skipped, so no ELF was reached


def test_clear_warns_but_does_not_fail_on_a_shared_object_without_a_stack_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "libnostack.so").write_bytes(_elf64((_PT_LOAD, _PF_R)))
    assert _mod.main(["clear", str(tmp_path)]) == 0
    assert "warning" in capsys.readouterr().err
