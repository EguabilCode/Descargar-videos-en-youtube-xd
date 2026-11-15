@echo off
rem Abre el Descargador de YouTube (doble clic).
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0descargador.py"
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0descargador.py"
    exit /b
)

where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0descargador.py"
    exit /b
)

echo No se encontro Python en este equipo.
echo Instalalo desde https://www.python.org/downloads/ y marca "Add python.exe to PATH".
pause
