@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
echo Open http://127.0.0.1:5000 after the server starts.
echo First login: open data\first-run-admin.txt for your random password.
".venv\Scripts\python.exe" run_server_with_extensions.py
pause
