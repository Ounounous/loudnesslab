# -*- mode: python ; coding: utf-8 -*-
# Empaquetado de Loudness Lab con PyInstaller: genera dist\LoudnessLab\LoudnessLab.exe.
# Se construye con construir.bat (usa el entorno .venv del proyecto).
#
# Carpeta y no un solo .exe: arranca más rápido (no se descomprime en cada inicio) y los
# antivirus desconfían menos. El motor de grabación es el mismo .exe con --motor.

from PyInstaller.utils.hooks import collect_submodules

ocultos = collect_submodules("winrt") + ["pyaudiowpatch", "soundfile"]

a = Analysis(
    ["LoudnessLab.py"],
    pathex=["."],
    binaries=[],
    # la licencia del algoritmo de sonoridad copiado de MoSQITo (loudness_lab/zwicker.py, Apache 2.0)
    datas=[("loudness_lab/LICENCIA_MOSQITO.txt", "loudness_lab")],
    hiddenimports=ocultos,
    excludes=["matplotlib", "tkinter", "IPython", "pyqtgraph.opengl", "pyqtgraph.examples",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
              "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick3D", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LoudnessLab",
    console=False,
    icon="icono.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LoudnessLab")
