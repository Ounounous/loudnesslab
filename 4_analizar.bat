@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
echo Analizando todas las sesiones en la carpeta grabaciones...
%PY% analizar.py "grabaciones\*"
echo.
echo Resultados: grabaciones\<sesion>\analisis\  y  comparacion\
pause
