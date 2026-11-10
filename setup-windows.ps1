[CmdletBinding()]
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ($Python) {
        $Launcher = $Python
        $LauncherArgs = @()
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $Launcher = 'py'
        $LauncherArgs = @('-3.12')
    } else {
        $Launcher = 'python'
        $LauncherArgs = @()
    }
    & $Launcher @LauncherArgs -c "import struct,sys; assert (3,10) <= sys.version_info[:2] <= (3,12) and struct.calcsize('P') == 8, 'Use 64-bit Python 3.10-3.12 (3.12 recommended)'"
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64, or pass -Python C:\path\to\python.exe.' }
    & $Launcher @LauncherArgs -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
& $VenvPython -c "import struct,sys; assert (3,10) <= sys.version_info[:2] <= (3,12) and struct.calcsize('P') == 8, 'Recreate .venv with Python 3.12 x64'"
if ($LASTEXITCODE -ne 0) { throw 'Unsupported .venv Python version or architecture.' }
& $VenvPython -m pip install -r (Join-Path $Root 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Ready. Run start-source.bat, or build-windows.bat to create the EXE.'
