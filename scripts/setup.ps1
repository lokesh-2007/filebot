[CmdletBinding()]
param(
    [switch]$DownloadModel
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $pythonLauncher) {
    throw "Install 64-bit Python 3.11 or 3.12 from https://www.python.org/downloads/windows/ and enable the Python launcher."
}

$pythonVersion = $null
foreach ($candidate in @("-3.11", "-3.12")) {
    & $pythonLauncher.Source $candidate -c "import struct,sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
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
& $python (Join-Path $projectRoot "scripts\prefetch_source_grammars.py")
if ($LASTEXITCODE -ne 0) { throw "Could not cache source parsers for offline indexing." }
& $python (Join-Path $projectRoot "scripts\prefetch_tokenizer.py")
if ($LASTEXITCODE -ne 0) { throw "Could not cache the prompt tokenizer required for local context checks." }

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($null -eq $ollama) {
    Write-Warning "Install Ollama for Windows, then run: ollama pull qwen2.5-coder:3b"
} else {
    & $ollama.Source show "qwen2.5-coder:3b" *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($DownloadModel) {
            & $ollama.Source pull "qwen2.5-coder:3b"
            if ($LASTEXITCODE -ne 0) { throw "Could not download qwen2.5-coder:3b." }
        } else {
            Write-Host "To download the local model (about 2 GB), run: ollama pull qwen2.5-coder:3b"
            Write-Host "Or rerun this script with -DownloadModel."
        }
    }
}

Write-Host "FileBot setup is ready. Start it with: .\scripts\start.ps1"
