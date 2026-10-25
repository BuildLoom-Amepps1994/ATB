@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >NUL 2>NUL
if not errorlevel 1 (
  py -3.12 -c "import sys" >NUL 2>NUL
  if not errorlevel 1 (
    py -3.12 -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
) else (
  where python >NUL 2>NUL
  if errorlevel 1 (
    echo Install Python 3.12 from python.org, enable Add to PATH, then run again.
    pause
    exit /b 1
  )
  python -m venv .venv
)
if errorlevel 1 (
  echo Failed to create the Python environment.
  pause
  exit /b 1
)
:install
".venv\Scripts\python.exe" scripts\bootstrap.py
if errorlevel 1 (
  pause
  exit /b 1
)
echo Starting CryptoPilot. Keep this window open while the bot is running.
".venv\Scripts\python.exe" launcher.py
set "CRYPTOPILOT_EXIT=%ERRORLEVEL%"
pause
exit /b %CRYPTOPILOT_EXIT%
