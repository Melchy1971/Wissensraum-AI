@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
 echo Python 3.11 oder neuer wird benoetigt. Installation: https://www.python.org/downloads/
 pause
 exit /b 1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
 echo Installation fehlgeschlagen. Internetverbindung pruefen.
 pause
 exit /b 1
)
.venv\Scripts\python.exe -c "from PIL import Image, ImageTk" >nul 2>nul
if errorlevel 1 (
 echo Beschaedigte Python-Umgebung erkannt. Virtuelle Umgebung wird neu aufgebaut.
 rmdir /s /q .venv
 py -3 -m venv .venv
 .venv\Scripts\python.exe -m pip install --upgrade pip
 .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
 if errorlevel 1 (
  echo Reparatur fehlgeschlagen. Internetverbindung und Python-Installation pruefen.
  pause
  exit /b 1
 )
 .venv\Scripts\python.exe -c "from PIL import Image, ImageTk" >nul 2>nul
 if errorlevel 1 (
  echo Pillow konnte nicht korrekt geladen werden.
  pause
  exit /b 1
 )
)
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
