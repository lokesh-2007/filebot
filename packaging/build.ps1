<#
.SYNOPSIS
  Build the FileBot Windows installer: PyInstaller freeze, stage models + llama-server,
  self-check the staged folder, then compile the Inno Setup installer.

.EXAMPLE
  .\packaging\build.ps1 -LlamaServerDir C:\llama.cpp\build\bin -ModelPath C:\models\qwen2.5-3b-instruct-q4_k_m.gguf

.NOTES
  Not run in the authoring environment (it has no Windows). Treat the first run as a test.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$LlamaServerDir,   # folder with llama-server.exe and its DLLs
    [Parameter(Mandatory)] [string]$ModelPath,        # the chat GGUF
    [string]$ModelFileName = "qwen2.5-3b-instruct-q4_k_m.gguf",  # must match models.llm.model_path
    [string]$EmbeddingDir,                            # default: models\bge-small-en-v1.5-onnx (fetched if missing)
    [switch]$Deep,                                    # also load the models and query llama-server
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

if (-not (Test-Path (Join-Path $LlamaServerDir "llama-server.exe"))) { throw "llama-server.exe not found in $LlamaServerDir" }
if (-not (Test-Path $ModelPath)) { throw "Model file not found: $ModelPath" }
if (-not $EmbeddingDir) { $EmbeddingDir = Join-Path $root "models\bge-small-en-v1.5-onnx" }

& $python -m pip install -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
if (-not (Test-Path (Join-Path $EmbeddingDir "model_optimized.onnx"))) {
    & $python scripts\fetch_models.py
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch the embedding model" }
}

& $python -m PyInstaller packaging\filebot.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$internal = Join-Path $root "dist\FileBot\_internal"
$bin = Join-Path $internal "bin"
$models = Join-Path $internal "models"
New-Item -ItemType Directory -Force $bin, $models | Out-Null
Copy-Item (Join-Path $LlamaServerDir "*") $bin -Recurse -Force
Copy-Item $ModelPath (Join-Path $models $ModelFileName) -Force
Copy-Item $EmbeddingDir (Join-Path $models "bge-small-en-v1.5-onnx") -Recurse -Force

$out = Join-Path $env:TEMP "filebot-selfcheck.json"
$arguments = @("--selfcheck", "--require-assets", "--selfcheck-out=$out")
if ($Deep) { $arguments += "--deep" }
$process = Start-Process (Join-Path $root "dist\FileBot\FileBot.exe") -ArgumentList $arguments -Wait -PassThru
if (Test-Path $out) { Get-Content $out }
if ($process.ExitCode -ne 0) { throw "Selfcheck of the staged build failed (see the report above)" }

if ($SkipInstaller) { Write-Host "Staged build is in dist\FileBot"; exit 0 }
$version = (& $python -c "from filebot_version import __version__; print(__version__)").Trim()
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6.3+ (ISCC.exe) not found. Install it from https://jrsoftware.org/isinfo.php" }
& $iscc "/DAppVersion=$version" packaging\filebot.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Installer written to dist-installer"
