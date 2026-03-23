@echo off
setlocal
cd /d "%~dp0"

title ChirpScan Web UI

rem Check virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] .venv is missing.
    echo         Please run setup.bat first.
    echo.
    pause
    exit /b 1
)

rem Start web server
echo.
echo Starting Web UI on http://127.0.0.1:8000
echo.
".venv\Scripts\python.exe" -m src.web_main

if errorlevel 1 (
    echo.
    echo [ERROR] Web UI failed to start. Check output above.
    echo.
    pause
    exit /b 1
)

echo.
echo Web UI stopped. Press any key to close...
pause >nul
