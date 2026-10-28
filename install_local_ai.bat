@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\install_local_ai.py
if errorlevel 1 (
  echo Model download failed. You can retry this script to resume.
  pause
  exit /b 1
)
echo Local vision model installed.
pause
