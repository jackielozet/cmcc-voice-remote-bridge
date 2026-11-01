@echo off
rem Build the app with PyInstaller (onedir mode, includes the ASR model).
rem Output: dist\CMCC_RC_Keys\   -- copy the whole folder to another PC.
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

python -c "import sherpa_onnx" >nul 2>nul
if errorlevel 1 (
  echo Installing sherpa-onnx ...
  python -m pip install sherpa-onnx
)

python -m PyInstaller --noconfirm --clean CMCC_RC_Keys.spec
if errorlevel 1 (
  echo.
  echo Build FAILED.
  pause
  exit /b 1
)

echo.
echo Done: "%CD%\dist\CMCC_RC_Keys"
echo Copy that whole folder to another Windows 10/11 PC to run it.
echo.
pause
exit /b 0
