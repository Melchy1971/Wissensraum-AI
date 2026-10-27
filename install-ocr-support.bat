@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) and sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Die virtuelle Umgebung verwendet keine unterstuetzte Python-Version. .venv entfernen und erneut starten.
    pause
    exit /b 1
  )
  goto venv_ready
)
call :require_system_python
if errorlevel 1 exit /b 1
py -%PYSEL% -m venv .venv
if errorlevel 1 (
  echo Virtuelle Python-Umgebung konnte nicht erstellt werden.
  pause
  exit /b 1
)
:venv_ready
.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
if errorlevel 1 (
  echo.
  echo Installation der Python-Pakete fuer OCR fehlgeschlagen.
  pause
  exit /b 1
)
echo.
echo Python-Pakete fuer OCR wurden installiert.
set "TESS="
where tesseract >nul 2>nul && set "TESS=tesseract"
if not defined TESS if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESS=%ProgramFiles%\Tesseract-OCR\tesseract.exe"
if not defined TESS (
  echo.
  echo Das Tesseract-Programm fehlt noch. Es wird nicht automatisch installiert.
  echo Installation mit winget:  winget install --id UB-Mannheim.TesseractOCR
  echo oder Installer von https://github.com/UB-Mannheim/tesseract/wiki
  echo Im Installer unter "Additional language data" Deutsch auswaehlen.
  echo Danach dieses Skript erneut starten.
  pause
  exit /b 1
)
"%TESS%" --list-langs
.venv\Scripts\python.exe -c "import ocr; s=ocr.status(); print(s['message']); raise SystemExit(0 if s['available'] else 1)"
if errorlevel 1 (
  echo OCR ist noch nicht vollstaendig eingerichtet. Siehe Meldung oben.
  pause
  exit /b 1
)
echo.
echo OCR ist eingerichtet. Bilder und Scans werden beim Import jetzt gelesen.
echo Bereits in Quarantaene liegende Dateien: Import, "Quarantaene erneut verarbeiten".
pause
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
  echo Python 3.11 bis 3.13 wird benoetigt. Vorabversionen werden nicht unterstuetzt.
  pause
  exit /b 1
)
exit /b 0
