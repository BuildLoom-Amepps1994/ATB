@echo off
setlocal
cd /d "%~dp0"
set "CRYPTOPILOT_INSTALL=%LOCALAPPDATA%\Programs\CryptoPilot"
if not "%~1"=="" set "CRYPTOPILOT_INSTALL=%~1"
if not exist "%CRYPTOPILOT_INSTALL%\runtime\CryptoPilotRuntime.exe" (
  echo CryptoPilot installation was not found.
  echo Run this file with the installation folder as its first argument if you chose a custom location.
  pause
  exit /b 1
)
echo Close CryptoPilot before updating. Your portfolio and settings are stored separately.
"%CRYPTOPILOT_INSTALL%\runtime\CryptoPilotRuntime.exe" --update-source "%~dp0."
set "CRYPTOPILOT_UPDATE_EXIT=%ERRORLEVEL%"
if not "%CRYPTOPILOT_UPDATE_EXIT%"=="0" echo No update was confirmed. Read the message above before retrying.
pause
exit /b %CRYPTOPILOT_UPDATE_EXIT%
