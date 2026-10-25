@echo off
setlocal
cd /d "%~dp0"
if exist ".build-venv\Scripts\python.exe" goto buildinstall
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m venv .build-venv
  goto created
)
where py >NUL 2>NUL
if errorlevel 1 (
  python -m venv .build-venv
  goto created
)
rem Prefer the newest supported Python; scripts\bootstrap.py rejects anything outside 3.12-3.14.
for %%v in (3.14 3.13 3.12) do (
  py -%%v -c "import sys" >NUL 2>NUL
  if not errorlevel 1 (
    py -%%v -m venv .build-venv
    goto created
  )
)
py -3 -m venv .build-venv
:created
if errorlevel 1 (
  echo Could not create the build environment. Install 64-bit Python 3.14, then run again.
  pause
  exit /b 1
)
:buildinstall
".build-venv\Scripts\python.exe" scripts\bootstrap.py --build
if errorlevel 1 (
  pause
  exit /b 1
)
".build-venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm CryptoPilot.spec
if errorlevel 1 (
  echo EXE packaging failed. Review the errors above.
  pause
  exit /b 1
)
echo.
echo Your Windows executable: dist\CryptoPilot\CryptoPilot.exe
echo Keep the entire dist\CryptoPilot folder together.
pause
