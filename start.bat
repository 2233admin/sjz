@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo [1/3] Creating virtual environment...
python -m venv .venv
if errorlevel 1 goto nopython

echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto nodeps
echo [3/3] Ready.

:run
".venv\Scripts\python.exe" run.py
goto end

:nopython
echo.
echo Python not found. Install Python 3.9+ and tick Add Python to PATH.
goto end

:nodeps
echo.
echo Dependency install failed. Check your network and retry.
goto end

:end
echo.
pause
