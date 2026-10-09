@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Construye dist\LoudnessLab\LoudnessLab.exe y deja un acceso directo en el escritorio.
rem La primera vez crea el entorno .venv con todo lo necesario.

if not exist .venv\Scripts\python.exe (
    echo Creando el entorno de Python...
    py -3 -m venv .venv || (echo No encuentro Python 3. Instalalo desde python.org. & pause & exit /b 1)
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
)

.venv\Scripts\pyinstaller.exe LoudnessLab.spec --noconfirm || (echo Fallo la construccion. & pause & exit /b 1)

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Loudness Lab.lnk');" ^
  "$s.TargetPath = '%~dp0dist\LoudnessLab\LoudnessLab.exe';" ^
  "$s.WorkingDirectory = '%~dp0dist\LoudnessLab';" ^
  "$s.IconLocation = '%~dp0dist\LoudnessLab\LoudnessLab.exe,0';" ^
  "$s.Description = 'Graba y mide el loudness de lo que suena en el PC';" ^
  "$s.Save()"

echo.
echo Listo: dist\LoudnessLab\LoudnessLab.exe y el acceso directo "Loudness Lab" en el escritorio.
pause
