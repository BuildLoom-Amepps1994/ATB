@echo off
setlocal
cd /d "%~dp0"
if exist ".build-venv\Scripts\python.exe" goto installerdeps
where py >NUL 2>NUL
if errorlevel 1 (
  echo Install Python 3.12 and Inno Setup 6 or 7, then run this file again.
  pause
  exit /b 1
)
py -3.12 -m venv .build-venv
if errorlevel 1 (
  echo Python 3.12 is required for the verified build.
  pause
  exit /b 1
)
:installerdeps
".build-venv\Scripts\python.exe" scripts\bootstrap.py --build
if errorlevel 1 goto installerfailed
".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath dist\editable-runtime CryptoPilotEditable.spec
if errorlevel 1 goto installerfailed
set "CRYPTOPILOT_STAGE=dist\editable-%RANDOM%-%RANDOM%"
".build-venv\Scripts\python.exe" scripts\build_installer.py --runtime-dir dist\editable-runtime\CryptoPilotRuntime --stage "%CRYPTOPILOT_STAGE%" --output dist\installer
if errorlevel 1 goto installerfailed
echo Installer created in dist\installer. No application or trading was started.
pause
exit /b 0
:installerfailed
echo Installer build failed. Review the message above. Inno Setup must be installed or INNO_SETUP_COMPILER set.
pause
exit /b 1
