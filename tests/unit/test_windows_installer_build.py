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
"""Tests for the fail-closed Windows installer build contract."""

from __future__ import annotations

import runpy
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _ROOT / "packaging" / "windows" / "build_installer.py"
_INSTALLER_SCRIPT = _ROOT / "packaging" / "windows" / "datacron-installer.iss"
_RELEASE_WORKFLOW = _ROOT / ".github" / "workflows" / "release.yml"
_SCRIPT_GLOBALS = runpy.run_path(str(_BUILD_SCRIPT), run_name="installer_build_test")
_BUILD_ERROR = cast("type[RuntimeError]", _SCRIPT_GLOBALS["InstallerBuildError"])
_VALIDATE_PAYLOAD = cast(
    "Callable[[Path, str], str]",
    _SCRIPT_GLOBALS["validate_payload"],
)
_VALIDATE_ISCC_ARGUMENTS = cast(
    "Callable[[Sequence[str]], None]",
    _SCRIPT_GLOBALS["_validate_additional_iscc_arguments"],
)


def _payload(tmp_path: Path) -> Path:
    executable = tmp_path / "datacron.exe"
    executable.write_bytes(b"placeholder")
    return executable


def test_payload_version_accepts_an_exact_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _payload(tmp_path)

    def fake_run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [str(executable), "--version"]
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout="datacron 2026.0721.01\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _VALIDATE_PAYLOAD(executable, "2026.0721.01") == "datacron 2026.0721.01"


def test_payload_version_rejects_a_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _payload(tmp_path)

    def fake_run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout="datacron 2026.0715.00\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_BUILD_ERROR, match="payload version mismatch") as error:
        _VALIDATE_PAYLOAD(executable, "2026.0721.01")
    assert "expected 'datacron 2026.0721.01'" in str(error.value)
    assert "got 'datacron 2026.0715.00'" in str(error.value)
    assert "Rebuild dist/datacron.exe" in str(error.value)


def test_payload_version_rejects_an_old_cli_without_version_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _payload(tmp_path)

    def fake_run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=2,
            stdout="",
            stderr="No such option: --version",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_BUILD_ERROR, match="version check failed with exit code 2") as error:
        _VALIDATE_PAYLOAD(executable, "2026.0721.01")
    assert "No such option: --version" in str(error.value)
    assert "Rebuild dist/datacron.exe" in str(error.value)


def test_payload_version_rejects_a_missing_executable(tmp_path: Path) -> None:
    missing = tmp_path / "missing.exe"

    with pytest.raises(_BUILD_ERROR, match="Installer payload is missing") as error:
        _VALIDATE_PAYLOAD(missing, "2026.0721.01")
    assert "Rebuild dist/datacron.exe" in str(error.value)


@pytest.mark.parametrize(
    "argument",
    [
        "/DAppVersion=2026.0721.99",
        "/DPayloadVersionVerified=2026.0721.99",
    ],
)
def test_reserved_version_defines_cannot_be_overridden(argument: str) -> None:
    with pytest.raises(_BUILD_ERROR, match="reserved version define"):
        _VALIDATE_ISCC_ARGUMENTS([argument])


def test_ci_and_iss_require_the_shared_version_guard() -> None:
    installer = _INSTALLER_SCRIPT.read_text(encoding="utf-8")
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "#ifndef PayloadVersionVerified" in installer
    assert "#if !SameStr(AppVersion, PayloadVersionVerified)" in installer
    assert "packaging\\windows\\build_installer.py @arguments" in workflow
    assert "& $iscc @arguments" not in workflow
