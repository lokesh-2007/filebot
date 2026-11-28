@echo off
rem One-time setup without PowerShell: creates .venv and installs requirements.
cd /d "%~dp0"
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY ( py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11" )
if not defined PY ( python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)" >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo Python 3.11 or 3.12 ^(64-bit^) was not found. Install it from python.org and rerun.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 ( echo Could not create the virtual environment. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo pip install failed - read the error above. & pause & exit /b 1 )
echo.
echo Setup complete. Double-click FileBot.bat to start, then press Ctrl+Alt+Space.
pause
