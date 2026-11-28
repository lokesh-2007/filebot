@echo off
rem Starts FileBot (auto mode) without PowerShell, so no execution-policy errors.
rem Use FileBot.bat --demo for the sandboxed sample-files demo, --mode=lite|full to force a mode.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" app.pyw %*
) else (
    echo FileBot is not set up yet. Run Setup.bat first.
    pause
)
