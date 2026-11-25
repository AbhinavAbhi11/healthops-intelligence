@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install Python 3.10 or newer and try again.
  pause
  exit /b 1
)
echo Starting HealthOps Intelligence in local-development mode.
python server.py
pause
