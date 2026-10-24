@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Open Auto Outbound.ps1"
if errorlevel 1 (
  echo The app could not start. See the message above.
  pause
  exit /b 1
)
