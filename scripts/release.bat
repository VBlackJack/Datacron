@echo off
REM Copyright 2026 Julien Bombled
REM
REM Licensed under the Apache License, Version 2.0 (the "License");
REM you may not use this file except in compliance with the License.
REM You may obtain a copy of the License at
REM
REM     http://www.apache.org/licenses/LICENSE-2.0
REM
REM Unless required by applicable law or agreed to in writing, software
REM distributed under the License is distributed on an "AS IS" BASIS,
REM WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
REM See the License for the specific language governing permissions and
REM limitations under the License.
REM
REM One-click Datacron release (Windows): compute the next CalVer, bump
REM __init__.py, commit, and push the bump to a release branch. The tag is
REM created after the release pull request merges, on main's merge commit;
REM its push triggers the GitHub release workflow that builds the binaries.

setlocal EnableExtensions

REM Repository root (this script lives in scripts/).
cd /d "%~dp0.." || (echo Cannot reach repo root & exit /b 1)

REM Prefer the project virtualenv, fall back to python on PATH.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM Compute the next CalVer without writing anything yet.
set "VER="
for /f "delims=" %%v in ('"%PY%" scripts\bump_version.py --dry-run') do set "VER=%%v"
if "%VER%"=="" (echo Could not compute the next version. & exit /b 1)

REM Require release notes before changing, committing, or tagging the version.
findstr /L /C:"## [%VER%]" CHANGELOG.md >nul
if errorlevel 1 (
    echo CHANGELOG.md has no entry for %VER%. Add release notes before tagging.
    exit /b 1
)

REM Capture the exact main commit that the clean preflight verifies against origin.
set "BASE="
for /f "delims=" %%s in ('git rev-parse HEAD 2^>nul') do set "BASE=%%s"
if "%BASE%"=="" (echo Could not resolve the current Git commit. & exit /b 1)

"%PY%" scripts\release_preflight.py clean --version "%VER%" --base-sha "%BASE%" || (
    echo Release preflight failed before any change.
    exit /b 1
)

echo.
echo   Next Datacron release: %VER%
echo.
choice /c YN /m "Bump, commit and push release/v%VER%"
if errorlevel 2 (echo Aborted, nothing changed. & exit /b 0)

"%PY%" scripts\bump_version.py || (echo Bump failed. & exit /b 1)
"%PY%" scripts\release_preflight.py bumped || (echo Bumped state is unsafe. & exit /b 1)
git add src\datacron\__init__.py server.json || (echo git add failed. & exit /b 1)
"%PY%" scripts\release_preflight.py staged || (echo Staged state is unsafe. & exit /b 1)
git commit -m "chore(version): %VER%" || (echo git commit failed. & exit /b 1)
"%PY%" scripts\release_preflight.py committed --version "%VER%" --base-sha "%BASE%" || (
    echo Committed release state is unsafe; nothing was pushed.
    exit /b 1
)
REM The main ruleset requires the Quality gate to have passed on the exact SHA, which
REM a direct push cannot satisfy. The bump goes to a side branch and its PR carries the
REM SHA through the gate. No tag is created here: the release tag goes on the merge
REM commit of that PR, main's tip once it lands, which does not exist yet.
git push origin "HEAD:refs/heads/release/v%VER%" || (
    echo Pushing the release branch failed; the local commit is still here.
    echo   git reset --hard %BASE%
    exit /b 1
)

echo.
echo   Pushed release/v%VER%. Finish the release with:
echo.
echo     gh pr create --base main --head release/v%VER% --fill
echo     gh pr merge --merge ^<number^>          (or merge it from the web UI)
echo.
echo   Once main carries the merge, tag its tip and push the tag:
echo.
echo     git fetch origin
echo     git tag -a v%VER% origin/main -m "Datacron %VER%"
echo     "%PY%" scripts\release_preflight.py merged --version %VER%
echo     git push origin v%VER%
endlocal
