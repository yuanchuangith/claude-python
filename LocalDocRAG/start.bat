@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
if errorlevel 1 (
  echo LocalDocRAG startup failed. Read the message above.
  pause
  exit /b 1
)
