@echo off
rem Same as FileBot.bat but keeps a console open so you can read errors.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.pyw %*
) else (
    echo FileBot is not set up yet. Run Setup.bat first.
)
pause
