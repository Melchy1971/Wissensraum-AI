@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 oder neuer wird benoetigt. Installation: https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-pst.txt
if errorlevel 1 (
  echo.
  echo Installation fehlgeschlagen. Fuer libratom werden unter Windows C++ Build Tools benoetigt.
  pause
  exit /b 1
)
echo.
echo PST-Unterstuetzung wurde installiert.
pause
