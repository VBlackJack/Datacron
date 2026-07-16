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

<#
.SYNOPSIS
    Build the standalone Datacron executable (Windows) with PyInstaller.
.DESCRIPTION
    Produces a single-file `datacron.exe` that bundles the CLI, its Python
    runtime, and the packaged data files (reliability evidence and
    contradiction data). Run from an environment where the `[build]` optional
    dependency (PyInstaller) is installed, e.g. `pip install -e ".[build]"`.
.PARAMETER Python
    Path to the Python interpreter to build with. Defaults to the repository
    virtual environment.
.PARAMETER OutputDir
    Directory that receives the built executable. Defaults to `dist`.
.PARAMETER Clean
    Remove previous build and output artifacts before building.
.EXAMPLE
    ./scripts/build_installer.ps1 -Clean
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Python interpreter used to run PyInstaller.
    [Parameter(Mandatory = $false)]
    [string]$Python = ".venv\Scripts\python.exe",

    # Output directory for the built executable.
    [Parameter(Mandatory = $false)]
    [string]$OutputDir = "dist",

    # Whether to remove prior build artifacts first.
    [Parameter(Mandatory = $false)]
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# Resolve paths relative to the repository root (this script lives in scripts/).
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Entry = Join-Path $RepoRoot "packaging\datacron_launcher.py"
$WorkPath = Join-Path $RepoRoot "build\pyinstaller"
$DistPath = Join-Path $RepoRoot $OutputDir
$ExeName = "datacron"

try {
    if (-not (Test-Path $Python)) {
        Write-Warning "Interpreter '$Python' not found; falling back to 'python' on PATH."
        $Python = "python"
    }

    if (-not (Test-Path $Entry)) {
        throw "Entry script not found: $Entry"
    }

    Write-Verbose "Verifying PyInstaller is available."
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed. Run: $Python -m pip install -e `".[build]`""
    }

    if ($Clean) {
        foreach ($path in @($WorkPath, $DistPath)) {
            if ((Test-Path $path) -and $PSCmdlet.ShouldProcess($path, "Remove")) {
                Write-Verbose "Removing $path"
                Remove-Item $path -Recurse -Force
            }
        }
    }

    $arguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", $ExeName,
        "--collect-data", "datacron",
        "--collect-submodules", "datacron",
        "--collect-submodules", "pydantic",
        "--hidden-import", "pydantic_settings",
        "--hidden-import", "truststore",
        "--distpath", $DistPath,
        "--workpath", $WorkPath,
        "--specpath", $WorkPath,
        $Entry
    )

    if ($PSCmdlet.ShouldProcess($Entry, "Build datacron.exe")) {
        Write-Verbose "Running PyInstaller."
        & $Python @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller build failed with exit code $LASTEXITCODE."
        }

        $exePath = Join-Path $DistPath "$ExeName.exe"
        if (Test-Path $exePath) {
            Write-Output "Built standalone executable: $exePath"
        } else {
            throw "Build reported success but $exePath is missing."
        }
    }
}
catch {
    Write-Error "Build failed: $_"
    exit 1
}
