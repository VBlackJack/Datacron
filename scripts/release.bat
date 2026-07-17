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

echo.
echo   Next Datacron release: %VER%
echo.
choice /c YN /m "Bump, commit, tag v%VER% and push"
if errorlevel 2 (echo Aborted, nothing changed. & exit /b 0)

"%PY%" scripts\bump_version.py || (echo Bump failed. & exit /b 1)
git add src\datacron\__init__.py || (echo git add failed. & exit /b 1)
git commit -m "chore(version): %VER%" || (echo git commit failed. & exit /b 1)
git tag -a "v%VER%" -m "Datacron %VER%" || (echo git tag failed. & exit /b 1)
git push origin HEAD || (echo git push branch failed. & exit /b 1)
git push origin "v%VER%" || (echo git push tag failed. & exit /b 1)

echo.
echo   Released v%VER% - the GitHub release workflow will build the binaries.
endlocal
