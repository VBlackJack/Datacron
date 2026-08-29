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
"""Discriminating tests for the fail-closed release Git preflight."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "release_preflight.py"
_RELEASE_BATCH = _ROOT / "scripts" / "release.bat"
_VERSION = "2026.0829.00"
_VERSION_PATHS = ("server.json", "src/datacron/__init__.py")


@dataclass(frozen=True)
class _ReleaseRepo:
    root: Path
    remote: Path
    base_sha: str


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _git(
    repo: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *arguments], cwd=repo, env=env, check=check)


def _preflight(
    repo: _ReleaseRepo,
    phase: str,
    *,
    env: dict[str, str] | None = None,
    base_sha: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(_SCRIPT),
        phase,
        "--repo-root",
        str(repo.root),
    ]
    if phase in {"clean", "committed"}:
        command.extend(("--version", _VERSION, "--base-sha", base_sha or repo.base_sha))
    return _run(command, cwd=repo.root, env=env, check=False)


def _write_version_changes(repo: _ReleaseRepo) -> None:
    (repo.root / "src" / "datacron" / "__init__.py").write_text(
        f'__version__ = "{_VERSION}"\n', encoding="utf-8", newline="\n"
    )
    (repo.root / "server.json").write_text(
        '{"version":"2026.829.0","packages":[{"version":"2026.829.0"}]}\n',
        encoding="utf-8",
        newline="\n",
    )


def _stage_version_changes(repo: _ReleaseRepo) -> None:
    _write_version_changes(repo)
    _git(repo.root, "add", *_VERSION_PATHS)


def _commit_and_tag(repo: _ReleaseRepo) -> None:
    _stage_version_changes(repo)
    _git(repo.root, "commit", "-m", f"chore(version): {_VERSION}")
    _git(repo.root, "tag", "-a", f"v{_VERSION}", "-m", f"Datacron {_VERSION}")


@pytest.fixture
def release_repo(tmp_path: Path) -> _ReleaseRepo:
    remote = tmp_path / "origin.git"
    root = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(root))
    _git(root, "config", "user.name", "Release Tester")
    _git(root, "config", "user.email", "fixture.invalid")
    (root / "src" / "datacron").mkdir(parents=True)
    (root / "src" / "datacron" / "__init__.py").write_text(
        '__version__ = "2026.0828.01"\n', encoding="utf-8", newline="\n"
    )
    (root / "server.json").write_text(
        '{"version":"2026.828.1","packages":[{"version":"2026.828.1"}]}\n',
        encoding="utf-8",
        newline="\n",
    )
    (root / "README.md").write_text("Datacron\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: initialize release repository")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    _git(root, "config", "user.email", "")
    base_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    return _ReleaseRepo(root=root, remote=remote, base_sha=base_sha)


def test_clean_phase_accepts_safe_repo_without_mutating_it(release_repo: _ReleaseRepo) -> None:
    before_head = _git(release_repo.root, "rev-parse", "HEAD").stdout
    before_status = _git(
        release_repo.root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    ).stdout
    before_refs = _git(release_repo.root, "show-ref").stdout

    result = _preflight(release_repo, "clean")

    assert result.returncode == 0, result.stderr
    assert _git(release_repo.root, "rev-parse", "HEAD").stdout == before_head
    assert (
        _git(
            release_repo.root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout
        == before_status
    )
    assert _git(release_repo.root, "show-ref").stdout == before_refs


@pytest.mark.parametrize("head_state", ["feature", "detached"])
def test_clean_phase_rejects_non_main_head(release_repo: _ReleaseRepo, head_state: str) -> None:
    if head_state == "feature":
        _git(release_repo.root, "switch", "-c", "feature")
    else:
        _git(release_repo.root, "switch", "--detach")

    result = _preflight(release_repo, "clean")

    assert result.returncode == 1
    assert "symbolic main branch" in result.stderr


@pytest.mark.parametrize("dirty_state", ["unstaged", "staged", "untracked"])
def test_clean_phase_rejects_every_dirty_state(
    release_repo: _ReleaseRepo, dirty_state: str
) -> None:
    if dirty_state == "untracked":
        (release_repo.root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    else:
        (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
        if dirty_state == "staged":
            _git(release_repo.root, "add", "README.md")

    result = _preflight(release_repo, "clean")

    assert result.returncode == 1
    assert "clean phase" in result.stderr


@pytest.mark.parametrize("divergence", ["local-ahead", "remote-ahead"])
def test_clean_phase_rejects_remote_divergence(
    release_repo: _ReleaseRepo, tmp_path: Path, divergence: str
) -> None:
    if divergence == "local-ahead":
        (release_repo.root / "README.md").write_text("local ahead\n", encoding="utf-8")
        _git(release_repo.root, "add", "README.md")
        _git(release_repo.root, "commit", "-m", "test: advance local main")
    else:
        other = tmp_path / "other"
        _git(tmp_path, "clone", "--branch", "main", str(release_repo.remote), str(other))
        _git(other, "config", "user.name", "Remote Tester")
        _git(other, "config", "user.email", "fixture.invalid")
        (other / "README.md").write_text("remote ahead\n", encoding="utf-8")
        _git(other, "add", "README.md")
        _git(other, "commit", "-m", "test: advance remote main")
        _git(other, "push", "origin", "main")

    result = _preflight(release_repo, "clean")

    assert result.returncode == 1
    assert "not synchronized" in result.stderr


def test_clean_phase_fails_closed_when_origin_is_unreachable(
    release_repo: _ReleaseRepo, tmp_path: Path
) -> None:
    _git(release_repo.root, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = _preflight(release_repo, "clean")

    assert result.returncode == 1
    assert "required Git release check failed" in result.stderr


@pytest.mark.parametrize(
    ("variable", "role"),
    [
        ("GIT_AUTHOR_EMAIL", "author"),
        ("GIT_COMMITTER_EMAIL", "committer"),
    ],
)
def test_identity_failure_never_leaks_the_email_value(
    release_repo: _ReleaseRepo, variable: str, role: str
) -> None:
    sentinel = "private-sentinel.invalid"

    result = _preflight(release_repo, "clean", env={variable: sentinel})

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert role in result.stderr
    assert sentinel not in combined


@pytest.mark.parametrize("location", ["local", "remote"])
def test_clean_phase_rejects_tag_collision(release_repo: _ReleaseRepo, location: str) -> None:
    tag = f"v{_VERSION}"
    _git(release_repo.root, "tag", "-a", tag, "-m", f"Datacron {_VERSION}")
    if location == "remote":
        _git(release_repo.root, "push", "origin", tag)
        _git(release_repo.root, "tag", "-d", tag)

    result = _preflight(release_repo, "clean")

    assert result.returncode == 1
    expected = "exists locally" if location == "local" else "exists on origin"
    assert expected in result.stderr


def test_bumped_and_staged_phases_accept_only_the_two_version_files(
    release_repo: _ReleaseRepo,
) -> None:
    _write_version_changes(release_repo)
    bumped = _preflight(release_repo, "bumped")
    assert bumped.returncode == 0, bumped.stderr

    _git(release_repo.root, "add", *_VERSION_PATHS)
    staged = _preflight(release_repo, "staged")
    assert staged.returncode == 0, staged.stderr


@pytest.mark.parametrize("invalid_state", ["missing", "extra", "already-staged"])
def test_bumped_phase_rejects_non_exact_status(
    release_repo: _ReleaseRepo, invalid_state: str
) -> None:
    _write_version_changes(release_repo)
    if invalid_state == "missing":
        _git(release_repo.root, "restore", "server.json")
    elif invalid_state == "extra":
        (release_repo.root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    else:
        _git(release_repo.root, "add", "server.json")

    result = _preflight(release_repo, "bumped")

    assert result.returncode == 1
    assert "bumped phase" in result.stderr


@pytest.mark.parametrize("invalid_state", ["extra-staged", "remaining-unstaged"])
def test_staged_phase_rejects_non_exact_status(
    release_repo: _ReleaseRepo, invalid_state: str
) -> None:
    _stage_version_changes(release_repo)
    (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    if invalid_state == "extra-staged":
        _git(release_repo.root, "add", "README.md")

    result = _preflight(release_repo, "staged")

    assert result.returncode == 1
    assert "staged phase" in result.stderr


def test_committed_phase_accepts_exact_release_commit_and_tag(
    release_repo: _ReleaseRepo,
) -> None:
    _commit_and_tag(release_repo)

    result = _preflight(release_repo, "committed")

    assert result.returncode == 0, result.stderr


def test_committed_phase_rejects_an_extra_committed_path(
    release_repo: _ReleaseRepo,
) -> None:
    _write_version_changes(release_repo)
    (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    _git(release_repo.root, "add", *_VERSION_PATHS, "README.md")
    _git(release_repo.root, "commit", "-m", f"chore(version): {_VERSION}")
    _git(release_repo.root, "tag", "-a", f"v{_VERSION}", "-m", f"Datacron {_VERSION}")

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    assert "exactly the two version files" in result.stderr


def test_committed_phase_rejects_a_parent_other_than_measured_main(
    release_repo: _ReleaseRepo,
) -> None:
    (release_repo.root / "README.md").write_text("intermediate\n", encoding="utf-8")
    _git(release_repo.root, "add", "README.md")
    _git(release_repo.root, "commit", "-m", "test: insert intermediate commit")
    _commit_and_tag(release_repo)

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    assert "parent is not the measured origin main" in result.stderr


@pytest.mark.parametrize("object_kind", ["commit", "tag"])
def test_committed_phase_rejects_and_hides_object_emails(
    release_repo: _ReleaseRepo, object_kind: str
) -> None:
    sentinel = "private-object-sentinel.invalid"
    _stage_version_changes(release_repo)
    if object_kind == "commit":
        _git(release_repo.root, "config", "user.email", sentinel)
    _git(release_repo.root, "commit", "-m", f"chore(version): {_VERSION}")
    if object_kind == "tag":
        _git(release_repo.root, "config", "user.email", sentinel)
    _git(release_repo.root, "tag", "-a", f"v{_VERSION}", "-m", f"Datacron {_VERSION}")
    _git(release_repo.root, "config", "user.email", "")

    result = _preflight(release_repo, "committed")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert object_kind in result.stderr
    assert sentinel not in combined


def test_release_batch_wires_all_phases_and_one_atomic_push() -> None:
    content = _RELEASE_BATCH.read_text(encoding="utf-8")
    clean = content.index("scripts\\release_preflight.py clean")
    bump = content.index("scripts\\bump_version.py ||")
    bumped = content.index("scripts\\release_preflight.py bumped")
    stage = content.index("git add src\\datacron\\__init__.py server.json")
    staged = content.index("scripts\\release_preflight.py staged")
    commit = content.index('git commit -m "chore(version): %VER%"')
    tag = content.index('git tag -a "v%VER%"')
    committed = content.index("scripts\\release_preflight.py committed")
    push = content.index(
        'git push --atomic origin "HEAD:refs/heads/main" "refs/tags/v%VER%:refs/tags/v%VER%"'
    )

    assert clean < bump < bumped < stage < staged < commit < tag < committed < push
    assert "git add src\\datacron\\__init__.py server.json CHANGELOG.md" not in content
    assert "--force" not in content
    assert "core.hooksPath" not in content
