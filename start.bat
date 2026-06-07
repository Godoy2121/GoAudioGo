@echo off
echo.
echo  GoAudioGo   →   http://localhost:8000
echo  Ctrl+C para detener.
echo.

REM Abrir el navegador automaticamente despues de 2 segundos
start "" timeout /t 2 /nobreak >nul & start http://localhost:8000

cd /d "%~dp0backend"
python main.py
