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
"""Fail-closed Git preflight checks for the Windows release script."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from re import Pattern
from re import compile as re_compile
from shutil import which
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_EXPECTED_PATHS: Final[frozenset[str]] = frozenset({"server.json", "src/datacron/__init__.py"})
_VERSION_RE: Final[Pattern[str]] = re_compile(
    r"(?P<year>\d{4})\.(?P<month>\d{2})(?P<day>\d{2})\.(?P<counter>\d{2})"
)
_SHA_RE: Final[Pattern[str]] = re_compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IDENTITY_RE: Final[Pattern[str]] = re_compile(r" <(?P<email>[^<>]*)> \d+ [+-]\d{4}\s*$")
_GIT_EXECUTABLE: Final[str | None] = which("git")


class ReleasePreflightError(RuntimeError):
    """Raised when a release Git invariant is not satisfied."""


def _run_git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    if _GIT_EXECUTABLE is None:
        raise ReleasePreflightError("Git is unavailable for the release preflight.")
    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            [_GIT_EXECUTABLE, *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise ReleasePreflightError("Git is unavailable for the release preflight.") from exc
    if completed.returncode not in allowed_returncodes:
        raise ReleasePreflightError("A required Git release check failed.")
    return completed


def _validated_version(value: str | None) -> str:
    if value is None:
        raise ReleasePreflightError("The release version is required for this phase.")
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ReleasePreflightError("The release version is not a valid Datacron CalVer.")
    try:
        date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError as exc:
        raise ReleasePreflightError("The release version is not a valid Datacron CalVer.") from exc
    return value


def _validated_base_sha(value: str | None) -> str:
    if value is None or _SHA_RE.fullmatch(value.casefold()) is None:
        raise ReleasePreflightError("The measured main SHA is required for this phase.")
    return value.casefold()


def _git_output(repo_root: Path, arguments: Sequence[str]) -> str:
    return _run_git(repo_root, arguments).stdout.strip()


def _status_records(repo_root: Path) -> frozenset[str]:
    completed = _run_git(
        repo_root,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ),
    )
    return frozenset(record for record in completed.stdout.split("\0") if record)


def _require_status(repo_root: Path, expected: frozenset[str], phase: str) -> None:
    if _status_records(repo_root) != expected:
        raise ReleasePreflightError(f"The Git status is not valid for the {phase} phase.")


def _require_empty_identity_text(identity: str, role: str) -> None:
    match = _IDENTITY_RE.search(identity)
    if match is None or match["email"]:
        raise ReleasePreflightError(f"The Git {role} email must be empty.")


def _require_empty_identity(repo_root: Path, variable: str, role: str) -> None:
    _require_empty_identity_text(_git_output(repo_root, ("var", variable)), f"effective {role}")


def _remote_main_sha(repo_root: Path) -> str:
    completed = _run_git(
        repo_root,
        ("ls-remote", "--exit-code", "--heads", "origin", "refs/heads/main"),
        allowed_returncodes=frozenset({0, 2}),
    )
    if completed.returncode != 0:
        raise ReleasePreflightError("The origin main branch could not be resolved.")
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise ReleasePreflightError("The origin main branch did not resolve uniquely.")
    fields = lines[0].split()
    if (
        len(fields) != 2
        or _SHA_RE.fullmatch(fields[0].casefold()) is None
        or fields[1] != "refs/heads/main"
    ):
        raise ReleasePreflightError("The origin main branch returned an invalid reference.")
    return fields[0].casefold()


def _require_tag_absent(repo_root: Path, version: str) -> None:
    tag_ref = f"refs/tags/v{version}"
    local = _run_git(
        repo_root,
        ("show-ref", "--verify", "--quiet", tag_ref),
        allowed_returncodes=frozenset({0, 1}),
    )
    if local.returncode == 0:
        raise ReleasePreflightError("The release tag already exists locally.")
    remote = _run_git(
        repo_root,
        ("ls-remote", "--exit-code", "--tags", "origin", tag_ref),
        allowed_returncodes=frozenset({0, 2}),
    )
    if remote.returncode == 0:
        raise ReleasePreflightError("The release tag already exists on origin.")


def _check_clean(repo_root: Path, version: str, base_sha: str) -> None:
    branch = _run_git(
        repo_root,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        allowed_returncodes=frozenset({0, 1}),
    )
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise ReleasePreflightError("A release must start from the symbolic main branch.")
    _require_status(repo_root, frozenset(), "clean")
    head_sha = _git_output(repo_root, ("rev-parse", "--verify", "HEAD")).casefold()
    if head_sha != base_sha or _remote_main_sha(repo_root) != base_sha:
        raise ReleasePreflightError("Local HEAD and origin main are not synchronized.")
    _require_empty_identity(repo_root, "GIT_AUTHOR_IDENT", "author")
    _require_empty_identity(repo_root, "GIT_COMMITTER_IDENT", "committer")
    _require_tag_absent(repo_root, version)


def _check_bumped(repo_root: Path) -> None:
    expected = frozenset(f" M {path}" for path in _EXPECTED_PATHS)
    _require_status(repo_root, expected, "bumped")


def _check_staged(repo_root: Path) -> None:
    expected = frozenset(f"M  {path}" for path in _EXPECTED_PATHS)
    _require_status(repo_root, expected, "staged")


def _check_committed(repo_root: Path, version: str, base_sha: str) -> None:
    _require_status(repo_root, frozenset(), "committed")
    head_sha = _git_output(repo_root, ("rev-parse", "--verify", "HEAD")).casefold()
    parent_sha = _git_output(repo_root, ("rev-parse", "--verify", "HEAD^")).casefold()
    if parent_sha != base_sha:
        raise ReleasePreflightError(
            "The release commit parent is not the measured origin main commit."
        )
    changed_paths = frozenset(
        _run_git(
            repo_root,
            ("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
        ).stdout.splitlines()
    )
    if changed_paths != _EXPECTED_PATHS:
        raise ReleasePreflightError(
            "The release commit does not contain exactly the two version files."
        )
    commit_emails = (
        _run_git(repo_root, ("show", "-s", "--format=%ae%x00%ce", "HEAD"))
        .stdout.rstrip("\r\n")
        .split("\0")
    )
    if commit_emails != ["", ""]:
        raise ReleasePreflightError("The release commit author and committer emails must be empty.")
    tag_ref = f"refs/tags/v{version}"
    if _git_output(repo_root, ("cat-file", "-t", tag_ref)) != "tag":
        raise ReleasePreflightError("The release tag is not annotated.")
    tag_object = _run_git(repo_root, ("cat-file", "-p", tag_ref)).stdout
    tagger_lines = [line for line in tag_object.splitlines() if line.startswith("tagger ")]
    if len(tagger_lines) != 1:
        raise ReleasePreflightError("The release tag does not contain one tagger identity.")
    _require_empty_identity_text(tagger_lines[0], "release tagger")
    tag_target = _git_output(
        repo_root, ("rev-parse", "--verify", f"{tag_ref}^{{commit}}")
    ).casefold()
    if tag_target != head_sha:
        raise ReleasePreflightError("The release tag does not target the release commit.")
    _require_empty_identity(repo_root, "GIT_AUTHOR_IDENT", "author")
    _require_empty_identity(repo_root, "GIT_COMMITTER_IDENT", "committer")


def run_phase(
    phase: str,
    *,
    repo_root: Path,
    version: str | None,
    base_sha: str | None,
) -> None:
    """Validate one release phase without changing the repository."""
    if phase == "clean":
        _check_clean(
            repo_root,
            _validated_version(version),
            _validated_base_sha(base_sha),
        )
    elif phase == "bumped":
        _check_bumped(repo_root)
    elif phase == "staged":
        _check_staged(repo_root)
    elif phase == "committed":
        _check_committed(
            repo_root,
            _validated_version(version),
            _validated_base_sha(base_sha),
        )
    else:
        raise ReleasePreflightError("Unknown release preflight phase.")


def main(argv: list[str] | None = None) -> int:
    """Run one fail-closed release preflight phase."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("clean", "bumped", "staged", "committed"))
    parser.add_argument("--version")
    parser.add_argument("--base-sha")
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        run_phase(
            args.phase,
            repo_root=args.repo_root.resolve(),
            version=args.version,
            base_sha=args.base_sha,
        )
    except ReleasePreflightError as exc:
        sys.stderr.write(f"Release preflight failed: {exc}\n")
        return 1
    sys.stdout.write(f"Release preflight {args.phase} phase passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
