@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Install first: python "%~dp0install.py"
  exit /b 2
)
"%~dp0.venv\Scripts\python.exe" -m sdd_runtime.cli %*
exit /b %errorlevel%
