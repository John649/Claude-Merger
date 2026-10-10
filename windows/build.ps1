# Build on Windows; PyInstaller does not cross-compile Windows executables.
[CmdletBinding()]
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Run this script on Windows." }

& $Python -c "import sys, tkinter; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'"
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.11+ with Tcl/Tk support first." }

$Venv = Join-Path $PSScriptRoot ".build-venv"
& $Python -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Creating the isolated build environment failed." }
$BuildPython = Join-Path $Venv "Scripts\python.exe"
# Runtime has no third-party packages. Only the optional builder needs PyInstaller.
& $BuildPython -m pip install --disable-pip-version-check "pyinstaller==6.16.0"
if ($LASTEXITCODE -ne 0) { throw "Installing PyInstaller failed." }

$Build = Join-Path $PSScriptRoot "build"
$Dist = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Path $Build -Force | Out-Null
& $BuildPython -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name ClaudeGraftWindows --paths $PSScriptRoot `
    --specpath $Build --workpath $Build --distpath $Dist `
    (Join-Path $PSScriptRoot "launch.py")
if ($LASTEXITCODE -ne 0) { throw "Building ClaudeGraftWindows.exe failed." }
$Executable = Join-Path $Dist "ClaudeGraftWindows.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw "Build output is missing." }
Write-Host "Built $Executable"
Write-Host "This executable is unsigned. Only use builds from a source you trust."
