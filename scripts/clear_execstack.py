#!/usr/bin/env python3
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
"""Clear the executable-stack request from ELF shared objects before they are bundled.

A shared object whose ``PT_GNU_STACK`` program header carries the execute flag asks the
dynamic loader to make the process stack executable when it is loaded. glibc 2.41 and newer
refuse that for a library loaded with ``dlopen``, so a one-file bundle holding such a library
dies at start-up with "cannot enable executable stack". Nothing in the interpreter needs an
executable stack, so the flag is cleared in place (one 4-byte field, no layout change) on the
interpreter the bundler runs on, and the bundler's table of contents is checked afterwards.

Only shared objects (``ET_DYN``) are patched: they are what ``dlopen`` and the bundle's
bootloader load, while no executable of the interpreter is bundled. A file that is busy
(``ETXTBSY``, for example the running interpreter, which is a PIE and so also ``ET_DYN``) is
skipped with a warning.

Two subcommands: ``clear <dir>...`` patches every ELF64 little-endian shared object under the
given directories, and ``check-toc <PKG-00.toc>`` fails when a collected binary still requests it.
Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import ast
import errno
import os
import struct
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

ELF_MAGIC: bytes = b"\x7fELF"
EI_CLASS_OFFSET: int = 4
EI_DATA_OFFSET: int = 5
ELFCLASS64: int = 2
ELFDATA2LSB: int = 1
ELF_IDENT_SIZE: int = 16

E_TYPE_OFFSET: int = 0x10
ET_DYN: int = 3
E_PHOFF_OFFSET: int = 0x20
E_PHENTSIZE_OFFSET: int = 0x36
E_PHNUM_OFFSET: int = 0x38
ELF64_HEADER_SIZE: int = 0x40

P_TYPE_OFFSET: int = 0
P_FLAGS_OFFSET: int = 4
P_FLAGS_SIZE: int = 4
PHDR_MIN_SIZE: int = P_FLAGS_OFFSET + P_FLAGS_SIZE

PT_GNU_STACK: int = 0x6474E551
PF_X: int = 0x1

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")

# PyInstaller writes the PKG target's _GUTS tuple to PKG-00.toc; the entry list is its
# "toc" element (see PyInstaller.building.api.PKG._GUTS). Each entry is
# (dest_name, src_name, typecode).
PKG_GUTS_TOC_INDEX: int = 2
_TOC_ELF_KINDS: frozenset[str] = frozenset({"BINARY", "EXTENSION"})
_TOC_KIND_INDEX: int = 2
_TOC_SOURCE_INDEX: int = 1
_TOC_MIN_FIELDS: int = 3
_MSG_EXEC_STACK: str = "executable stack requested"
_MSG_NO_STACK_HEADER: str = "no PT_GNU_STACK header (defaults to an executable stack)"
_EXIT_OK: int = 0
_EXIT_FAILURE: int = 1


def _is_elf64_le(header: bytes) -> bool:
    """True when the leading bytes describe an ELF64 little-endian object."""
    return (
        len(header) >= ELF_IDENT_SIZE
        and header[: len(ELF_MAGIC)] == ELF_MAGIC
        and header[EI_CLASS_OFFSET] == ELFCLASS64
        and header[EI_DATA_OFFSET] == ELFDATA2LSB
    )


def _read_layout(path: Path) -> tuple[int, tuple[int, int] | None] | None:
    """Return ``(e_type, stack)`` for an ELF64 little-endian file, or None for anything else.

    ``stack`` is ``(file offset of p_flags, p_flags)`` of ``PT_GNU_STACK``, or None when the
    object has no such header (or is too short to hold the tables it announces).
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(ELF64_HEADER_SIZE)
            if len(header) < ELF64_HEADER_SIZE or not _is_elf64_le(header):
                return None
            (e_type,) = _U16.unpack_from(header, E_TYPE_OFFSET)
            (phoff,) = _U64.unpack_from(header, E_PHOFF_OFFSET)
            (phentsize,) = _U16.unpack_from(header, E_PHENTSIZE_OFFSET)
            (phnum,) = _U16.unpack_from(header, E_PHNUM_OFFSET)
            if phentsize < PHDR_MIN_SIZE:
                return e_type, None
            for index in range(phnum):
                entry_offset = phoff + index * phentsize
                handle.seek(entry_offset)
                entry = handle.read(PHDR_MIN_SIZE)
                if len(entry) < PHDR_MIN_SIZE:
                    return e_type, None
                (p_type,) = _U32.unpack_from(entry, P_TYPE_OFFSET)
                if p_type == PT_GNU_STACK:
                    (p_flags,) = _U32.unpack_from(entry, P_FLAGS_OFFSET)
                    return e_type, (entry_offset + P_FLAGS_OFFSET, p_flags)
    except OSError:
        return None
    return e_type, None


def _find_stack_flags(path: Path) -> tuple[int, int] | None:
    """Return ``(file offset of p_flags, p_flags)`` of ``PT_GNU_STACK``, or None."""
    layout = _read_layout(path)
    return None if layout is None else layout[1]


def requests_exec_stack(path: Path) -> bool:
    """True when ``path`` is an ELF64 object whose ``PT_GNU_STACK`` carries ``PF_X``."""
    found = _find_stack_flags(path)
    return found is not None and bool(found[1] & PF_X)


def clear_exec_stack(path: Path) -> bool:
    """Clear ``PF_X`` of a shared object in place and verify it; True when it was changed.

    Other ELF types (``ET_EXEC`` and the rest) are left alone even when they carry the flag.
    """
    layout = _read_layout(path)
    if layout is None or layout[0] != ET_DYN or layout[1] is None:
        return False
    offset, flags = layout[1]
    if not flags & PF_X:
        return False
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(_U32.pack(flags & ~PF_X))
    if requests_exec_stack(path):
        raise RuntimeError(f"executable stack flag still set after patching: {path}")
    return True


def _walk_files(roots: Sequence[Path]) -> Iterator[Path]:
    """Yield each regular file once; symlinks are neither followed nor yielded."""
    seen: set[Path] = set()
    for root in roots:
        for current, _dirs, names in os.walk(root, followlinks=False):
            for name in names:
                candidate = Path(current) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                real = candidate.resolve()
                if real in seen:
                    continue
                seen.add(real)
                yield candidate


def run_clear(roots: Sequence[Path]) -> int:
    """Patch every ELF64 object under ``roots``; print each patched path."""
    for path in _walk_files(roots):
        try:
            changed = clear_exec_stack(path)
        except OSError as error:
            if error.errno != errno.ETXTBSY:
                raise
            print(f"warning: file is busy, not patched: {path}", file=sys.stderr)
            continue
        if changed:
            print(f"cleared executable stack: {path}")
            continue
        layout = _read_layout(path)
        if layout is not None and layout[0] == ET_DYN and layout[1] is None:
            print(f"warning: no PT_GNU_STACK header, cannot clear: {path}", file=sys.stderr)
    return _EXIT_OK


def _toc_sources(toc_path: Path) -> list[Path]:
    """Source paths of the BINARY and EXTENSION entries of a PyInstaller PKG-00.toc.

    Raises ``ValueError`` when the file is not the PKG guts tuple holding an entry list.
    """
    guts = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    if (
        not isinstance(guts, (tuple, list))
        or len(guts) <= PKG_GUTS_TOC_INDEX
        or not isinstance(guts[PKG_GUTS_TOC_INDEX], list)
    ):
        raise ValueError(f"{toc_path} is not a PyInstaller PKG guts tuple with a TOC list")
    entries = guts[PKG_GUTS_TOC_INDEX]
    return [
        Path(str(entry[_TOC_SOURCE_INDEX]))
        for entry in entries
        if len(entry) >= _TOC_MIN_FIELDS and entry[_TOC_KIND_INDEX] in _TOC_ELF_KINDS
    ]


def run_check_toc(toc_path: Path) -> int:
    """Fail when a collected ELF still requests an executable stack, or none was found."""
    try:
        sources = _toc_sources(toc_path)
    except (ValueError, SyntaxError, OSError) as error:
        print(f"cannot read the PyInstaller TOC: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    elf_count = 0
    offenders: list[tuple[Path, str]] = []
    for source in sources:
        layout = _read_layout(source)
        if layout is None:
            continue
        e_type, stack = layout
        if stack is None:
            if e_type == ET_DYN:
                elf_count += 1
                offenders.append((source, _MSG_NO_STACK_HEADER))
            continue
        elf_count += 1
        if stack[1] & PF_X:
            offenders.append((source, _MSG_EXEC_STACK))
    if offenders:
        for offender, reason in offenders:
            print(f"{reason}: {offender}", file=sys.stderr)
        return _EXIT_FAILURE
    if elf_count == 0:
        print(f"no ELF with a PT_GNU_STACK header listed in {toc_path}", file=sys.stderr)
        return _EXIT_FAILURE
    print(f"{elf_count} ELF files checked, none requests an executable stack")
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    clear = commands.add_parser("clear", help="clear PF_X on PT_GNU_STACK under directories")
    clear.add_argument("directories", nargs="+", type=Path)
    check = commands.add_parser("check-toc", help="verify a PyInstaller PKG-00.toc")
    check.add_argument("toc", type=Path)
    args = parser.parse_args(argv)
    if args.command == "clear":
        return run_clear(args.directories)
    return run_check_toc(args.toc)


if __name__ == "__main__":
    sys.exit(main())
