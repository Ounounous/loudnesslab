"""Utilidades de Windows: carpeta de la app, suspensión, disco, procesos."""

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def carpeta_app():
    """%APPDATA%\\LoudnessLab: configuración y estado del motor (no las grabaciones)."""
    p = Path(os.environ.get("APPDATA") or Path.home()) / "LoudnessLab"
    p.mkdir(parents=True, exist_ok=True)
    return p


def carpeta_salida_por_defecto():
    if Path("Q:/").exists():
        return r"Q:\LoudnessLab\grabaciones"
    return str(Path.home() / "Documents" / "LoudnessLab" / "grabaciones")


def impedir_suspension(activar=True):
    """Mientras graba, el PC no se suspende. Vale para el hilo que lo llama: hay que llamarlo
    desde un hilo que viva toda la grabación."""
    try:
        estado = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if activar else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(estado))
    except Exception:
        return False


def espacio_libre_gb(ruta):
    p = Path(ruta)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return float("nan")


def proceso_vivo(pid):
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        codigo = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(codigo))
        k32.CloseHandle(h)
        return bool(ok) and codigo.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def limpiar_nombre(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (s or "").strip()).strip(" .")
    return s or "sesion"


def comando_propio(*args):
    """Comando para relanzar esta misma app con otros argumentos (el motor, por ejemplo).
    Empaquetada es el .exe; en desarrollo, pythonw con LoudnessLab.py para no abrir una consola."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    raiz = Path(__file__).resolve().parent.parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else Path(sys.executable)), str(raiz / "LoudnessLab.py"), *args]


def lanzar_desacoplado(cmd):
    """Lanza un proceso que sobrevive al cierre de la ventana que lo lanzó."""
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=str(Path(cmd[0]).parent))
    try:
        # Si la ventana corre dentro de un "job" que mata a sus hijos al cerrarse (algunas
        # terminales lo hacen), el motor se escapa de él para no morir con la ventana.
        return subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        return subprocess.Popen(cmd, creationflags=base, **kw)


def abrir(ruta):
    os.startfile(str(ruta))
