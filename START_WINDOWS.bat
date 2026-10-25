@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >NUL 2>NUL
if errorlevel 1 goto nolauncher
rem Prefer the newest supported Python; scripts\bootstrap.py rejects anything outside 3.12-3.14.
for %%v in (3.14 3.13 3.12) do (
  py -%%v -c "import sys" >NUL 2>NUL
  if not errorlevel 1 (
    py -%%v -m venv .venv
    goto created
  )
)
py -3 -m venv .venv
goto created
:nolauncher
where python >NUL 2>NUL
if errorlevel 1 (
  echo Install 64-bit Python 3.14 from python.org, enable Add to PATH, then run again.
  pause
  exit /b 1
)
python -m venv .venv
:created
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
