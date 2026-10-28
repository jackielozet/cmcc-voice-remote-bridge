@echo off
rem CMCC remote key mapper - launcher
rem Starts the GUI with pythonw so there is no console window.
setlocal
set "HERE=%~dp0"
set "PYW="

for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
) do (
  if exist "%%~P" if not defined PYW set "PYW=%%~P"
)

if not defined PYW (
  where pythonw >nul 2>nul && set "PYW=pythonw"
)

if not defined PYW (
  echo Python 3.11+ with tkinter not found.
  echo Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

start "" "%PYW%" "%HERE%remote_ui.py"
exit /b 0
