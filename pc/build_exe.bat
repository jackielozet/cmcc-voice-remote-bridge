@echo off
rem Build a single-file exe with PyInstaller.
rem Output: dist\CMCC_RC_Keys.exe
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  pause
  exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo Installing PyInstaller ...
  python -m pip install pyinstaller
)

python -m PyInstaller --noconfirm --clean CMCC_RC_Keys.spec
if errorlevel 1 (
  echo.
  echo Build FAILED.
  pause
  exit /b 1
)

echo.
echo Done: "%CD%\dist\CMCC_RC_Keys.exe"
echo.
pause
exit /b 0
