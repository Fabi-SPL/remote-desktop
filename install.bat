@echo off
title Lucid Remote Desktop -- Install
cd /d "%~dp0"

echo.
echo  Lucid Remote Desktop -- First-time Setup
echo  ==========================================
echo.

REM Use whichever python is available
set PYTHON=python
where python >nul 2>&1 || (echo python not found on PATH & pause & exit /b 1)

echo  Creating virtual environment in venv\...
%PYTHON% -m venv venv

echo  Installing dependencies...
venv\Scripts\pip install -r host\requirements.txt

echo.
echo  Done! Run start.bat to launch.
echo.
echo  Optional: install VB-Cable for mic passthrough
echo  https://vb-audio.com/Cable/
echo.
pause
