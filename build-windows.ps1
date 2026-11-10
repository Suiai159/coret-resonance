[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup-windows.bat first.' }
& $Python (Join-Path $PSScriptRoot 'tools\build_windows.py')
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed. See the output above.' }
