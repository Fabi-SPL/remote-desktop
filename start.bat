@echo off
title Keled Remote Desktop
cd /d "%~dp0"

REM Bootstrap venv if not installed yet
if not exist "venv\Scripts\python.exe" (
    echo  venv not found -- running install first...
    call install.bat
)

REM Optional: set KELED_REMOTE_HOST to your own Tailscale IP or hostname.
REM Leave it unset and the server prints its detected Tailscale IP at startup.
if "%KELED_REMOTE_HOST%"=="" set KELED_REMOTE_HOST=localhost

echo.
echo  Keled Remote Desktop
echo  ====================
echo  Local:   http://localhost:8080
echo  Remote:  http://%KELED_REMOTE_HOST%:8080
echo.
echo  Options: --fps 60  --bitrate 12M
echo.

cd host
..\venv\Scripts\python server.py %*

pause
