# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Require real file and directory symlinks before accepting Windows CI evidence."""

from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    """Fail rather than silently skip link-confinement tests on a validation host."""
    with TemporaryDirectory(prefix="datacron-link-gate-") as directory:
        root = Path(directory)
        target = root / "target"
        target.mkdir()
        (target / "note.md").write_text("probe", encoding="utf-8")
        (root / "file-link").symlink_to(target / "note.md")
        (root / "dir-link").symlink_to(target, target_is_directory=True)
        if (root / "file-link").read_text() != "probe" or not (root / "dir-link").is_dir():
            raise RuntimeError("symlink round trip failed")


if __name__ == "__main__":
    main()
