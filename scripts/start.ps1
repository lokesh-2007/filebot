[CmdletBinding()]
param(
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$app = Join-Path $projectRoot "app.pyw"
if (-not (Test-Path $python)) {
    throw "FileBot is not set up yet. Run .\scripts\setup.ps1 first."
}

if ($Console) {
    & $python $app
    exit $LASTEXITCODE
}

if (-not (Test-Path $pythonw)) {
    throw "pythonw.exe was not found in the FileBot virtual environment."
}
$quotedApp = '"' + $app + '"'
Start-Process -FilePath $pythonw -ArgumentList $quotedApp -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Host "FileBot is starting in the desktop session. Use the configured global hotkey to open it."

