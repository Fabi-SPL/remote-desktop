@echo off
echo Killing anything on port 8080...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080 "') do (
    echo   Killing PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done.
pause
