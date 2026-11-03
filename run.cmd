@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. See README.md
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\start_local.py" --open-browser
if errorlevel 1 (
  echo Startup failed. See data\startup-error.log and data\server-error.log
  pause
  exit /b 1
)
