@echo off
setlocal
cd /d "%~dp0"
if exist ".build-venv\Scripts\python.exe" goto buildinstall
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m venv .build-venv
) else (
  where py >NUL 2>NUL
  if not errorlevel 1 (
    py -3.12 -c "import sys" >NUL 2>NUL
    if not errorlevel 1 (
      py -3.12 -m venv .build-venv
    ) else (
      py -3 -m venv .build-venv
    )
  ) else (
    python -m venv .build-venv
  )
)
if errorlevel 1 (
  echo Could not create the build environment. Install Python 3.12, then run again.
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
