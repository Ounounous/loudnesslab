@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo No encuentro Python. Instalalo desde python.org marcando "Add to PATH". & pause & exit /b)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
echo.
echo Listo. Dispositivos de salida disponibles:
%PY% grabar.py --listar
pause
