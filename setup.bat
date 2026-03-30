@echo off
setlocal
cd /d "%~dp0"

title ChirpScan Setup
echo.
echo ============================================
echo   ChirpScan - Setup
echo ============================================
echo.

rem 1) Check Python
echo [1/4] Checking Python ...
set "PYTHON_CMD="
set "PYTHON_LABEL="
set "PY_VER="

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    set "PYTHON_LABEL=python"
)

if not defined PYTHON_CMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        for %%v in (3.14 3.13 3.12 3.11 3.10) do (
            if not defined PYTHON_CMD (
                py -%%v -c "import sys" >nul 2>nul
                if not errorlevel 1 (
                    set "PYTHON_CMD=py -%%v"
                    set "PYTHON_LABEL=py -%%v"
                )
            )
        )
    )
)

if not defined PYTHON_CMD (
    echo.
    echo [ERROR] Python 3.10+ not found.
    echo         Install Python 3.10+ and then re-run this script.
    echo         Recommended command:
    echo         winget install --id Python.Python.3.12
    echo         https://www.python.org/downloads/
    goto fail
)

for /f "delims=" %%v in ('call %PYTHON_CMD% --version 2^>^&1') do (
    if not defined PY_VER set "PY_VER=%%v"
)
set "PY_VER=%PY_VER:Python =%"

if not defined PY_VER (
    echo.
    echo [ERROR] Failed to query Python version from %PYTHON_LABEL%.
    goto fail
)

echo         Python %PY_VER%
call %PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.10 or newer is required.
    goto fail
)
echo         Version check ... OK
echo.

rem 2) Create virtual environment if missing
echo [2/4] Checking .venv ...
if exist ".venv\Scripts\python.exe" (
    echo         .venv already exists ... skip
) else (
    echo         Creating .venv ...
    call %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create .venv.
        goto fail
    )
    echo         .venv created ... OK
)
echo.

rem 3) Install dependencies
echo [3/4] Installing dependencies from requirements.txt ...
echo.
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies.
    goto fail
)

rem 4) Create cookies placeholder file
echo.
echo [4/4] Preparing .twikit_cookies.json ...
if exist ".twikit_cookies.json" (
    echo         .twikit_cookies.json already exists ... skip
) else (
    type nul > ".twikit_cookies.json"
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create .twikit_cookies.json.
        goto fail
    )
    echo         Placeholder created ... OK
)

echo.
echo ============================================
echo   Setup complete!
echo   You can now run start_web.bat
echo ============================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================
echo   Setup failed. Fix the error above and retry.
echo ============================================
echo.
pause
exit /b 1
