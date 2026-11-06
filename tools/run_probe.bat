@echo off
chcp 936 >nul
cd /d "%~dp0"
echo.
echo === audio channel probe ===
echo Close CMCC_RC_Keys first (it holds the COM port), then press any key.
echo.
pause >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0audio_channel_probe.ps1" -Port COM3 -Secs 25
echo.
echo Done. Send the probe_*.log file back.
pause
