# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Select a smaller CI matrix only for proven documentation-only diffs."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

_DOC_FILES = frozenset({"README.md", "README.en.md", "CHANGELOG.md"})
_DOC_ROOTS = ("docs/fr/", "docs/en/")
_FULL_MATRIX = {
    "os": ["ubuntu-latest", "windows-latest"],
    "python-version": ["3.11", "3.12", "3.13"],
}
_DOC_MATRIX = {"os": ["ubuntu-latest"], "python-version": ["3.12"]}


def documentation_only(paths: list[str]) -> bool:
    """Accept only a nonempty diff entirely inside the editorial allowlist."""
    return bool(paths) and all(
        path in _DOC_FILES or (path.startswith(_DOC_ROOTS) and path.endswith(".md"))
        for path in paths
    )


def changed_paths() -> list[str]:
    """Read the exact Git diff; missing history never enables the fast path."""
    if os.environ.get("FORCE_FULL") == "true":
        return []
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name == "push" and os.environ["GITHUB_REF"].startswith("refs/heads/"):
        base = event.get("before", "")
    elif event_name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
    else:
        return []
    if not base or set(base) == {"0"}:
        return []
    head = os.environ["GITHUB_SHA"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (base, head)):
        raise ValueError("Expected full Git commit hashes")
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("Git is required for scope detection")
    # Disable rename detection so both removed and added paths must be editorial.
    result = subprocess.run(  # noqa: S603 - resolved executable and validated commit hashes
        [git, "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []


def main() -> None:
    """Emit the matrix, falling back to complete validation on uncertainty."""
    logging.basicConfig(level=logging.INFO)
    try:
        docs_only = documentation_only(changed_paths())
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        logging.exception("Cannot prove a documentation-only change; running the full matrix")
        docs_only = False
    matrix = _DOC_MATRIX if docs_only else _FULL_MATRIX
    logging.info("Documentation-only change: %s", docs_only)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(matrix)}\n")


if __name__ == "__main__":
    main()
