@echo off
title Lucid Remote Desktop -- Uninstall Autostart
cd /d "%~dp0"

echo.
echo  Removing Lucid Remote Desktop autostart...
echo.

set TASKNAME=LucidRemoteDesktop

schtasks /End    /TN "%TASKNAME%" 2>nul
schtasks /Delete /TN "%TASKNAME%" /F

if exist "start-hidden.vbs" del "start-hidden.vbs"

echo.
echo  Done. Server will no longer auto-start at logon.
echo  You can still launch it manually with start.bat
echo.
pause
