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

import importlib.util
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "release_preflight.py"
_RELEASE_BATCH = _ROOT / "scripts" / "release.bat"
_PROJECT_CONFIG = (_ROOT / "pyproject.toml").read_bytes()
_EXPECTED_EMAIL = tomllib.loads(_PROJECT_CONFIG.decode("utf-8"))["tool"]["datacron"]["release"][
    "expected_email"
]
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
    # The throwaway repositories must not inherit the developer's global or system git
    # configuration (hooks, signing, identity guards); each test sets what it needs.
    merged_env["GIT_CONFIG_GLOBAL"] = os.devnull
    merged_env["GIT_CONFIG_NOSYSTEM"] = "1"
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        merged_env.pop(key, None)
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
    tag: str | None = None,
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
    elif phase == "merged":
        command.extend(("--version", _VERSION))
    elif phase == "attribution":
        command.extend(("--base-sha", base_sha or repo.base_sha))
    if tag is not None:
        command.extend(("--tag", tag))
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


def _commit_release(repo: _ReleaseRepo, env: dict[str, str] | None = None) -> None:
    _stage_version_changes(repo)
    _git(repo.root, "commit", "-m", f"chore(version): {_VERSION}", env=env)


def _tag(repo: _ReleaseRepo, target: str = "HEAD", env: dict[str, str] | None = None) -> None:
    _git(repo.root, "tag", "-a", f"v{_VERSION}", target, "-m", f"Datacron {_VERSION}", env=env)


def _commit_and_tag(repo: _ReleaseRepo) -> None:
    """A tag on the bump commit: what the publish workflow's ``tagged`` phase reads."""
    _commit_release(repo)
    _tag(repo)


def _merge_through_pull_request(repo: _ReleaseRepo, tmp_path: Path) -> str:
    """Push the bump to its release branch and merge it into origin main with --no-ff.

    This is what the pull request does on GitHub: main's new tip is a merge commit
    whose second parent is the bump. Returns that tip.
    """
    branch = f"release/v{_VERSION}"
    _git(repo.root, "push", "origin", f"HEAD:refs/heads/{branch}")
    reviewer = tmp_path / "reviewer"
    _git(tmp_path, "clone", "--branch", "main", str(repo.remote), str(reviewer))
    _git(reviewer, "config", "user.name", "Merge Tester")
    _git(reviewer, "config", "user.email", "fixture.invalid")
    _git(reviewer, "merge", "--no-ff", f"origin/{branch}", "-m", f"Merge pull request #1 {branch}")
    _git(reviewer, "push", "origin", "main")
    return _git(reviewer, "rev-parse", "HEAD").stdout.strip()


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
    (root / "pyproject.toml").write_bytes(_PROJECT_CONFIG)
    (root / "README.md").write_text("Datacron\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: initialize release repository")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    _git(root, "config", "user.email", _EXPECTED_EMAIL)
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


def test_committed_phase_accepts_exact_release_commit_without_a_tag(
    release_repo: _ReleaseRepo,
) -> None:
    _commit_release(release_repo)

    result = _preflight(release_repo, "committed")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("location", ["local", "remote"])
def test_committed_phase_refuses_a_tag_on_the_bump_commit(
    release_repo: _ReleaseRepo, location: str
) -> None:
    """The tag belongs on the merge commit of the release pull request.

    The release script used to tag the bump commit before pushing its branch, and
    this phase required it; the last three releases were tagged on main's merge
    commit instead, the only commit the Quality gate approves as main.
    """
    _commit_and_tag(release_repo)
    if location == "remote":
        _git(release_repo.root, "push", "origin", f"v{_VERSION}")
        _git(release_repo.root, "tag", "-d", f"v{_VERSION}")

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    expected = "exists locally" if location == "local" else "exists on origin"
    assert expected in result.stderr


def test_committed_phase_rejects_an_extra_committed_path(
    release_repo: _ReleaseRepo,
) -> None:
    _write_version_changes(release_repo)
    (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    _git(release_repo.root, "add", *_VERSION_PATHS, "README.md")
    _git(release_repo.root, "commit", "-m", f"chore(version): {_VERSION}")

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    assert "exactly the two version files" in result.stderr


def test_committed_phase_rejects_a_parent_other_than_measured_main(
    release_repo: _ReleaseRepo,
) -> None:
    (release_repo.root / "README.md").write_text("intermediate\n", encoding="utf-8")
    _git(release_repo.root, "add", "README.md")
    _git(release_repo.root, "commit", "-m", "test: insert intermediate commit")
    _commit_release(release_repo)

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    assert "parent is not the measured origin main" in result.stderr


def test_committed_phase_rejects_and_hides_commit_emails(release_repo: _ReleaseRepo) -> None:
    sentinel = "private-object-sentinel.invalid"
    _stage_version_changes(release_repo)
    _git(release_repo.root, "config", "user.email", sentinel)
    _git(release_repo.root, "commit", "-m", f"chore(version): {_VERSION}")
    _git(release_repo.root, "config", "user.email", _EXPECTED_EMAIL)

    result = _preflight(release_repo, "committed")

    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "commit" in result.stderr
    assert sentinel not in combined


def _released_through_pull_request(
    repo: _ReleaseRepo, tmp_path: Path, tag_env: dict[str, str] | None = None
) -> str:
    """The whole arbitrated flow: bump, merge the release PR, fetch, tag main's tip."""
    _commit_release(repo)
    tip = _merge_through_pull_request(repo, tmp_path)
    _git(repo.root, "fetch", "origin")
    _tag(repo, "origin/main", env=tag_env)
    return tip


def test_merged_phase_accepts_the_tag_on_the_merge_commit(
    release_repo: _ReleaseRepo, tmp_path: Path
) -> None:
    tip = _released_through_pull_request(release_repo, tmp_path)

    result = _preflight(release_repo, "merged")

    assert result.returncode == 0, result.stderr
    tagged = _git(release_repo.root, "rev-parse", f"v{_VERSION}^{{commit}}").stdout.strip()
    assert tagged == tip


def test_merged_phase_refuses_the_tag_on_the_bump_commit(
    release_repo: _ReleaseRepo, tmp_path: Path
) -> None:
    """The old flow: the tag created on the bump commit before the merge."""
    _commit_and_tag(release_repo)
    _merge_through_pull_request(release_repo, tmp_path)
    _git(release_repo.root, "fetch", "origin")

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    assert "does not target origin main's tip" in result.stderr


def test_merged_phase_requires_the_merge_to_be_fetched(
    release_repo: _ReleaseRepo, tmp_path: Path
) -> None:
    _commit_and_tag(release_repo)
    _merge_through_pull_request(release_repo, tmp_path)

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    assert "not fetched" in result.stderr


def test_merged_phase_refuses_a_main_that_does_not_carry_the_bump(
    release_repo: _ReleaseRepo,
) -> None:
    """Tagging origin/main before the release PR merged tags the previous version."""
    _commit_release(release_repo)
    _tag(release_repo, "origin/main")

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    assert "does not carry the release version" in result.stderr


def test_merged_phase_requires_the_release_commit_in_main(
    release_repo: _ReleaseRepo, tmp_path: Path
) -> None:
    """The version file alone is not proof: the bump commit must be in main's history."""
    _write_version_changes(release_repo)
    _git(release_repo.root, "add", *_VERSION_PATHS)
    _git(release_repo.root, "commit", "-m", "chore: unrelated edit of the version files")
    _merge_through_pull_request(release_repo, tmp_path)
    _git(release_repo.root, "fetch", "origin")
    _tag(release_repo, "origin/main")

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    assert "does not contain the release commit" in result.stderr


@pytest.mark.parametrize("state", ["missing", "lightweight", "pushed"])
def test_merged_phase_requires_one_local_annotated_unpushed_tag(
    release_repo: _ReleaseRepo, tmp_path: Path, state: str
) -> None:
    _commit_release(release_repo)
    _merge_through_pull_request(release_repo, tmp_path)
    _git(release_repo.root, "fetch", "origin")
    if state == "lightweight":
        _git(release_repo.root, "tag", f"v{_VERSION}", "origin/main")
    elif state == "pushed":
        _tag(release_repo, "origin/main")
        _git(release_repo.root, "push", "origin", f"v{_VERSION}")

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    expected = {
        "missing": "does not exist locally",
        "lightweight": "not annotated",
        "pushed": "already exists on origin",
    }[state]
    assert expected in result.stderr


@pytest.mark.parametrize(
    "email", ["", "private@example.com", "malformed", "123+foreign@users.noreply.github.com"]
)
def test_merged_phase_requires_the_configured_noreply_tagger(
    release_repo: _ReleaseRepo, tmp_path: Path, email: str
) -> None:
    _released_through_pull_request(release_repo, tmp_path, tag_env={"GIT_COMMITTER_EMAIL": email})

    result = _preflight(release_repo, "merged")

    assert result.returncode == 1
    assert "release tagger" in result.stderr
    if email:
        assert email not in result.stdout + result.stderr


def test_release_batch_wires_all_phases_and_tags_after_the_merge() -> None:
    """Every phase in order, the bump reaching main through a pull request, and no tag.

    A direct push cannot satisfy the branch ruleset, which requires the Quality
    gate to have passed on the exact SHA, so the script pushes a side branch. The
    tag used to be created on the bump commit here; it now goes on main's merge
    commit, so the script only prints the post-merge commands.
    """
    content = _RELEASE_BATCH.read_text(encoding="utf-8")
    clean = content.index("scripts\\release_preflight.py clean")
    bump = content.index("scripts\\bump_version.py ||")
    bumped = content.index("scripts\\release_preflight.py bumped")
    stage = content.index("git add src\\datacron\\__init__.py server.json")
    staged = content.index("scripts\\release_preflight.py staged")
    commit = content.index('git commit -m "chore(version): %VER%"')
    committed = content.index("scripts\\release_preflight.py committed")
    push = content.index('git push origin "HEAD:refs/heads/release/v%VER%"')
    fetch = content.index("echo     git fetch origin")
    tag = content.index('echo     git tag -a v%VER% origin/main -m "Datacron %VER%"')
    merged = content.index("scripts\\release_preflight.py merged --version %VER%")
    push_tag = content.index("echo     git push origin v%VER%")

    assert clean < bump < bumped < stage < staged < commit < committed < push
    assert push < fetch < tag < merged < push_tag
    # The only tag command is the one printed for after the merge.
    assert content.count("git tag") == 1
    assert "git tag -d" not in content
    assert "git add src\\datacron\\__init__.py server.json CHANGELOG.md" not in content
    assert "--force" not in content
    assert "core.hooksPath" not in content
    assert "refs/heads/main" not in content
    assert "--atomic" not in content
    assert "gh pr create --base main --head release/v%VER%" in content


@pytest.mark.parametrize("phase", ["clean", "committed"])
@pytest.mark.parametrize("variable", ["GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"])
@pytest.mark.parametrize(
    "email", ["", "private@example.com", "malformed", "123+foreign@users.noreply.github.com"]
)
def test_effective_identity_requires_exact_configured_noreply(
    release_repo: _ReleaseRepo, phase: str, variable: str, email: str
) -> None:
    if phase == "committed":
        _commit_release(release_repo)
    result = _preflight(release_repo, phase, env={variable: email})
    assert result.returncode == 1
    assert "effective" in result.stderr
    assert "project release identity" in result.stderr
    if email:
        assert email not in result.stdout + result.stderr


@pytest.mark.parametrize("role", ["author", "committer"])
@pytest.mark.parametrize(
    "email", ["", "private@example.com", "malformed", "123+foreign@users.noreply.github.com"]
)
def test_committed_objects_require_exact_configured_noreply(
    release_repo: _ReleaseRepo, role: str, email: str
) -> None:
    # The tagger case moved to the merged phase with the tag itself.
    _commit_release(release_repo, env={f"GIT_{role.upper()}_EMAIL": email})
    result = _preflight(release_repo, "committed")
    assert result.returncode == 1
    assert "release commit" in result.stderr
    if email:
        assert email not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "config",
    [
        "",
        "[tool.datacron.release]\nexpected_email = true\n",
        "[tool.datacron.release]\nexpected_email = ''\n",
        "[tool.datacron.release]\nexpected_email = 'private@example.com'\n",
        "[broken",
    ],
)
def test_invalid_project_release_identity_fails_closed(
    release_repo: _ReleaseRepo, config: str
) -> None:
    (release_repo.root / "pyproject.toml").write_text(config, encoding="utf-8")
    _git(release_repo.root, "add", "pyproject.toml")
    _git(release_repo.root, "commit", "-m", "test: invalid release configuration")
    _git(release_repo.root, "push", "origin", "main")
    current = _git(release_repo.root, "rev-parse", "HEAD").stdout.strip()
    result = _preflight(release_repo, "clean", base_sha=current)
    assert result.returncode == 1
    assert "project release identity" in result.stderr


def test_missing_project_release_identity_fails_closed(release_repo: _ReleaseRepo) -> None:
    _git(release_repo.root, "rm", "pyproject.toml")
    _git(release_repo.root, "commit", "-m", "test: missing release configuration")
    _git(release_repo.root, "push", "origin", "main")
    current = _git(release_repo.root, "rev-parse", "HEAD").stdout.strip()
    result = _preflight(release_repo, "clean", base_sha=current)
    assert result.returncode == 1
    assert "project release identity configuration" in result.stderr


def _preflight_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_preflight", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPT.parent))
    return module


@pytest.mark.parametrize(
    ("tag", "version", "expected"),
    [
        ("v2026.0829.00", "2026.0829.00", True),
        ("v2026.0829.01", "2026.0829.00", False),
        ("2026.0829.00", "2026.0829.00", False),
        ("v2026.829.0", "2026.0829.00", False),
        ("V2026.0829.00", "2026.0829.00", False),
        (" v2026.0829.00", "2026.0829.00", False),
    ],
)
def test_tag_matches_version_requires_the_exact_prefixed_calver(
    tag: str, version: str, expected: bool
) -> None:
    assert _preflight_module().tag_matches_version(tag, version) is expected


@pytest.mark.parametrize("version", ["", "1.2.3", "2026.1332.00", "v2026.0829.00"])
def test_tag_matches_version_refuses_a_malformed_package_version(version: str) -> None:
    with pytest.raises(ValueError, match="CalVer"):
        _preflight_module().tag_matches_version(f"v{version}", version)


def test_tagged_phase_accepts_the_tag_of_the_committed_version(
    release_repo: _ReleaseRepo,
) -> None:
    _commit_and_tag(release_repo)

    result = _preflight(release_repo, "tagged", tag=f"v{_VERSION}")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("tag", "fragment"),
    [
        ("v2026.0829.01", "does not match datacron.__version__"),
        (_VERSION, "does not match datacron.__version__"),
        ("v2026.829.0", "does not match datacron.__version__"),
        (None, "release tag is required"),
    ],
)
def test_tagged_phase_rejects_a_tag_that_is_not_v_plus_the_package_version(
    release_repo: _ReleaseRepo, tag: str | None, fragment: str
) -> None:
    _commit_and_tag(release_repo)

    result = _preflight(release_repo, "tagged", tag=tag)

    assert result.returncode == 1
    assert fragment in result.stderr


def test_tagged_phase_fails_closed_on_an_unreadable_package_version(
    release_repo: _ReleaseRepo,
) -> None:
    _commit_and_tag(release_repo)
    (release_repo.root / "src" / "datacron" / "__init__.py").write_text(
        "VERSION = None\n", encoding="utf-8", newline="\n"
    )

    result = _preflight(release_repo, "tagged", tag=f"v{_VERSION}")

    assert result.returncode == 1
    assert "package version could not be read" in result.stderr


def test_committed_phase_rejects_a_version_that_does_not_name_the_committed_version(
    release_repo: _ReleaseRepo,
) -> None:
    _stage_version_changes(release_repo)
    (release_repo.root / "src" / "datacron" / "__init__.py").write_text(
        '__version__ = "2026.0829.01"\n', encoding="utf-8", newline="\n"
    )
    _git(release_repo.root, "add", *_VERSION_PATHS)
    _git(release_repo.root, "commit", "-m", f"chore(version): {_VERSION}")

    result = _preflight(release_repo, "committed")

    assert result.returncode == 1
    assert "does not match datacron.__version__" in result.stderr


_ASSISTANT_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


@pytest.mark.parametrize(
    ("message", "credited"),
    [
        (f"fix: thing\n\n{_ASSISTANT_TRAILER}\n", True),
        ("fix: thing\n\nco-authored-by: Codex <codex@openai.com>\n", True),
        ("fix: thing\n\nCo-Authored-By: ChatGPT <bot@example.invalid>\n", True),
        ("fix: thing\n\nCo-authored-by: Anthropic Bot <bot@example.invalid>\n", True),
        ("docs: thing\n\nGenerated with a code assistant\n", True),
        ("fix: thing\n\nCo-Authored-By: Jane Doe <jane@example.invalid>\n", False),
        ("docs: regenerated with the new template\n", False),
        ("fix(search): match Claude-style notes\n", False),
    ],
)
def test_credits_an_assistant_reads_trailers_and_footers(message: str, credited: bool) -> None:
    assert _preflight_module().credits_an_assistant(message) is credited


def test_attribution_phase_accepts_a_clean_range(release_repo: _ReleaseRepo) -> None:
    _commit_release(release_repo)

    result = _preflight(release_repo, "attribution")

    assert result.returncode == 0, result.stderr


def test_attribution_phase_refuses_a_credited_commit_after_the_base(
    release_repo: _ReleaseRepo,
) -> None:
    """Twenty-two historical commits carry such a trailer; none may be added."""
    _commit_release(release_repo)
    (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    _git(release_repo.root, "add", "README.md")
    _git(release_repo.root, "commit", "-m", f"docs: change\n\n{_ASSISTANT_TRAILER}")
    credited = _git(release_repo.root, "rev-parse", "HEAD").stdout.strip()

    result = _preflight(release_repo, "attribution")

    assert result.returncode == 1
    assert credited[:12] in result.stderr


def test_attribution_phase_ignores_commits_before_the_base(release_repo: _ReleaseRepo) -> None:
    (release_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    _git(release_repo.root, "add", "README.md")
    _git(release_repo.root, "commit", "-m", f"docs: change\n\n{_ASSISTANT_TRAILER}")
    base = _git(release_repo.root, "rev-parse", "HEAD").stdout.strip()
    _commit_release(release_repo)

    result = _preflight(release_repo, "attribution", base_sha=base)

    assert result.returncode == 0, result.stderr
