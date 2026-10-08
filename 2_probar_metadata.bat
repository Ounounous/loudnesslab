@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
echo Pon musica en Soundtrack y cambia de cancion un par de veces.
echo Si aparecen artista y titulo, la metadata funciona. Ctrl+C para salir.
echo.
%PY% grabar.py --probar-metadata --app ""
pause
