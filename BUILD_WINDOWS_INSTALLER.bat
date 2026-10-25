@echo off
setlocal
cd /d "%~dp0"
if exist ".build-venv\Scripts\python.exe" goto installerdeps
where py >NUL 2>NUL
if errorlevel 1 (
  echo Install 64-bit Python 3.14 and Inno Setup 6 or 7, then run this file again.
  pause
  exit /b 1
)
rem Prefer the newest supported Python; scripts\bootstrap.py rejects anything outside 3.12-3.14.
for %%v in (3.14 3.13 3.12) do (
  py -%%v -c "import sys" >NUL 2>NUL
  if not errorlevel 1 (
    py -%%v -m venv .build-venv
    goto created
  )
)
echo Python 3.14 is required for the build. Python 3.13 or 3.12 is also accepted.
pause
exit /b 1
:created
if errorlevel 1 (
  echo Could not create the build environment. Review the message above.
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
