@echo off
REM Copyright 2026 Julien Bombled
REM
REM Licensed under the Apache License, Version 2.0 (the "License");
REM you may not use this file except in compliance with the License.
REM You may obtain a copy of the License at
REM
REM     http://www.apache.org/licenses/LICENSE-2.0
REM
REM One-click Datacron release (Windows): compute the next CalVer, bump
REM __init__.py, commit, tag v<version>, and push. The tag push triggers the
REM GitHub release workflow that builds the multi-OS binaries.

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
choice /c YN /m "Bump, commit, tag v%VER% and push"
if errorlevel 2 (echo Aborted, nothing changed. & exit /b 0)

"%PY%" scripts\bump_version.py || (echo Bump failed. & exit /b 1)
"%PY%" scripts\release_preflight.py bumped || (echo Bumped state is unsafe. & exit /b 1)
git add src\datacron\__init__.py server.json || (echo git add failed. & exit /b 1)
"%PY%" scripts\release_preflight.py staged || (echo Staged state is unsafe. & exit /b 1)
git commit -m "chore(version): %VER%" || (echo git commit failed. & exit /b 1)
git tag -a "v%VER%" -m "Datacron %VER%" || (echo git tag failed. & exit /b 1)
"%PY%" scripts\release_preflight.py committed --version "%VER%" --base-sha "%BASE%" || (
    echo Committed release state is unsafe; nothing was pushed.
    exit /b 1
)
REM The main ruleset requires the Quality gate to have passed on the exact SHA, which
REM a direct push cannot satisfy: it is refused, and before this the refusal arrived
REM only after the commit and tag already existed locally, leaving the operator to
REM undo both by hand. The bump goes to a side branch, its PR carries the SHA through
REM the gate, and the tag is pushed once main holds it.
git push origin "HEAD:refs/heads/release/v%VER%" || (
    echo Pushing the release branch failed; the local commit and tag are still here.
    echo   git reset --hard %BASE%  ^&^&  git tag -d v%VER%
    exit /b 1
)

echo.
echo   Pushed release/v%VER%. Finish the release with:
echo.
echo     gh pr create --base main --head release/v%VER% --fill
echo     gh pr merge --merge ^<number^>          (or merge it from the web UI)
echo     git fetch origin ^&^& git push origin "refs/tags/v%VER%:refs/tags/v%VER%"
echo.
echo   Tag v%VER% exists locally and is not pushed yet; push it once main carries
echo   the merge, so the tag lands on the commit the Quality gate approved.
endlocal
