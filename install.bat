@echo off
setlocal

echo.
echo  ╔══════════════════════════════╗
echo  ║      GoAudioGo - Setup       ║
echo  ╚══════════════════════════════╝
echo.

REM ── Python ─────────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado.
    echo         Instala Python 3.9+ desde https://python.org/downloads
    echo         Asegurate de marcar "Add to PATH" durante la instalacion.
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do set PY_VER=%%i
echo [OK] %PY_VER% encontrado.

REM ── ffmpeg ──────────────────────────────────────────────────────────────────
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [AVISO] ffmpeg no encontrado. Es OBLIGATORIO para convertir a MP3.
    echo.
    echo   Opciones de instalacion:
    echo     1. winget install ffmpeg          ^(recomendado^)
    echo     2. choco install ffmpeg
    echo     3. Descarga manual: https://ffmpeg.org/download.html
    echo.
    echo   Despues de instalar ffmpeg, vuelve a ejecutar este script.
    pause & exit /b 1
)
echo [OK] ffmpeg encontrado.

REM ── pip deps ────────────────────────────────────────────────────────────────
echo.
echo Instalando dependencias Python...
pip install -r backend\requirements.txt --quiet --no-warn-script-location
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de dependencias.
    pause & exit /b 1
)
echo [OK] Dependencias instaladas.

echo.
echo ════════════════════════════════════════
echo  Instalacion completada con exito.
echo  Ejecuta start.bat para arrancar la app.
echo ════════════════════════════════════════
echo.
pause
