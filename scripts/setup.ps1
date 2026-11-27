[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $pythonLauncher) {
    throw "Install 64-bit Python 3.11 or 3.12 from https://www.python.org/downloads/windows/ and enable the Python launcher."
}

$pythonVersion = $null
foreach ($candidate in @("-3.11", "-3.12")) {
    $candidateIsSupported = $false
    try {
        & $pythonLauncher.Source $candidate -c "import struct,sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" *> $null
        $candidateIsSupported = ($LASTEXITCODE -eq 0)
    } catch {
        $candidateIsSupported = $false
    }
    if ($candidateIsSupported) {
        $pythonVersion = $candidate
        break
    }
}
if ($null -eq $pythonVersion) {
    throw "FileBot requires 64-bit Python 3.11 or 3.12."
}

$venv = Join-Path $projectRoot ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $pythonLauncher.Source $pythonVersion -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the FileBot virtual environment." }
}

$python = Join-Path $venv "Scripts\python.exe"
& $python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv does not use Python 3.11 or 3.12. Remove that virtual environment and rerun setup."
}
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }
& $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install FileBot requirements." }

$server = Join-Path $projectRoot "bin\llama-server.exe"
$model = Join-Path $projectRoot "models\qwen2.5-3b-instruct-q4_k_m.gguf"
if (-not (Test-Path $server)) {
    Write-Warning "Place llama.cpp's llama-server.exe (a Windows release build) at $server."
}
if (-not (Test-Path $model)) {
    Write-Warning "Place the GGUF model at $model (see README, 'Local model'). Packaged builds bundle both files."
}

Write-Host "FileBot setup is ready. Start it with: .\scripts\start.ps1"
