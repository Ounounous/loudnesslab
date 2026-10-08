@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
set /p ETI=Etiqueta de la sesion (ej. soundtrack_lounge, spotify_pop): 
set /p NOTAS=Notas (playlist, configuracion de normalizacion, crossfade...): 
set /p APP=App a seguir para la metadata (soundtrack / spotify): 
set /p HORAS=Horas a grabar (0 = hasta Ctrl+C): 
%PY% grabar.py --etiqueta "%ETI%" --notas "%NOTAS%" --horas %HORAS% --app "%APP%"
pause
