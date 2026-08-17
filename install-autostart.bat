@echo off
title Lucid Remote Desktop -- Install Autostart

REM ── Self-elevate to admin ──────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo.
echo  Lucid Remote Desktop -- Autostart Installer
echo  ===========================================
echo.
echo  This will:
echo    1. Generate a hidden launcher (start-hidden.vbs)
echo    2. Register a Task Scheduler entry that runs at logon
echo    3. The server starts automatically when you log into Windows
echo.
echo  Remove later with: uninstall-autostart.bat
echo.

REM ── Generate hidden VBS launcher ───────────────────────────────────────
set VBS=%~dp0start-hidden.vbs
> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>>"%VBS%" echo sh.CurrentDirectory = "%~dp0host"
>>"%VBS%" echo sh.Run """%~dp0venv\Scripts\python.exe"" ""%~dp0host\server.py""", 0, False

echo  [+] Created %VBS%

REM ── Register scheduled task ────────────────────────────────────────────
schtasks /Delete /TN "LucidRemoteDesktop" /F >nul 2>&1
schtasks /Create /TN "LucidRemoteDesktop" ^
  /TR "wscript.exe \"%VBS%\"" ^
  /SC ONLOGON ^
  /RL HIGHEST ^
  /F

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Failed to register the scheduled task.
    pause
    exit /b 1
)

echo  [+] Registered task LucidRemoteDesktop  (runs at logon, highest privileges)
echo.
echo  Done. Test it now:
echo     schtasks /Run /TN LucidRemoteDesktop
echo.
echo  Then visit  https://%LUCID_REMOTE_HOST%:8080  from your client device.
echo  (set LUCID_REMOTE_HOST first, or read the IP the server prints at startup)
echo.
pause
