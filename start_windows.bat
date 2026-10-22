@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
 .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) and sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul
 if not errorlevel 1 goto venv_ready
 echo Veraltete oder nicht unterstuetzte Python-Umgebung erkannt. Virtuelle Umgebung wird neu aufgebaut.
)
call :require_system_python
if errorlevel 1 exit /b 1
if exist .venv\Scripts\python.exe rmdir /s /q .venv
rem Nicht verwenden: py -3 -m venv .venv (koennte Python 3.12 oder 3.14 waehlen).
py -%PYSEL% -m venv .venv
if errorlevel 1 (
 echo Virtuelle Python-Umgebung konnte nicht erstellt werden.
 pause
 exit /b 1
)
:venv_ready
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
 echo Installation fehlgeschlagen. Internetverbindung pruefen.
 pause
 exit /b 1
)
.venv\Scripts\python.exe -c "from PIL import Image, ImageTk" >nul
if errorlevel 1 (
 echo Beschaedigte Python-Umgebung erkannt. Virtuelle Umgebung wird neu aufgebaut.
 call :require_system_python
 if errorlevel 1 exit /b 1
 rmdir /s /q .venv
 py -%PYSEL% -m venv .venv
 .venv\Scripts\python.exe -m pip install --upgrade pip
 .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
 if errorlevel 1 (
  echo Reparatur fehlgeschlagen. Internetverbindung und Python-Installation pruefen.
  pause
  exit /b 1
 )
 .venv\Scripts\python.exe -c "from PIL import Image, ImageTk" >nul
 if errorlevel 1 (
  echo Pillow konnte nicht korrekt geladen werden.
  pause
  exit /b 1
 )
)
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
goto :eof

:require_system_python
where py >nul 2>nul
if errorlevel 1 (
 echo Python 3.11 bis 3.13 wird benoetigt. Installation: https://www.python.org/downloads/
 pause
 exit /b 1
)
set "PYSEL="
for %%V in (3.13 3.12 3.11) do (
 if not defined PYSEL (
  py -%%V -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '%%V'.split('.'))) and sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul
  if not errorlevel 1 set "PYSEL=%%V"
 )
)
if not defined PYSEL (
 echo Python 3.11 bis 3.13 wird benoetigt. Python 3.14 und Vorabversionen werden nicht unterstuetzt.
 pause
 exit /b 1
)
exit /b 0
