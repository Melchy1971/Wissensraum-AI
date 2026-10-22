@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) and sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Die virtuelle Umgebung verwendet keine unterstuetzte Python-Version. .venv entfernen und erneut starten.
    pause
    exit /b 1
  )
  goto venv_ready
)
call :require_system_python
if errorlevel 1 exit /b 1
py -3 -m venv .venv
:venv_ready
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
goto :eof

:require_system_python
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 bis 3.13 wird benoetigt. Installation: https://www.python.org/downloads/
  pause
  exit /b 1
)
py -3 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) and sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 bis 3.13 wird benoetigt. Vorabversionen werden nicht unterstuetzt.
  pause
  exit /b 1
)
exit /b 0
