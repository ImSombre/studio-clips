@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Il faut d'abord installer Studio Clips.
  pause
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" lanceur.py
