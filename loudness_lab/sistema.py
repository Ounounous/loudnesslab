"""Utilidades de Windows: carpeta de la app, suspensión, disco, procesos."""

import ctypes
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

_candados = []   # los mutex tomados por este proceso: quedan tomados hasta que termina


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


def candado_motor():
    """Toma el mutex «hay un motor grabando». False si ya lo tiene otro proceso: así nunca graban dos
    motores a la vez, aunque la ventana y el guardián lancen uno en el mismo instante. Windows lo suelta
    solo cuando el proceso termina, de cualquier forma que termine. El nombre depende de la carpeta de
    la app, para que una prueba con otra carpeta no choque con la grabación real."""
    clave = hashlib.md5(str(carpeta_app()).lower().encode("utf-8")).hexdigest()[:12]
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, f"Local\\LoudnessLabMotor_{clave}")
        if not h:
            return True        # no se pudo crear: mejor grabar que bloquearse
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return False
        _candados.append(h)
        return True
    except Exception:
        return True


def correr_oculto(cmd, timeout=20):
    """Corre un programa de consola sin que aparezca su ventana (la app no tiene consola)."""
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)


def ultimo_arranque():
    """Cuándo arrancó Windows por última vez (epoch)."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        return dt.datetime.now().timestamp() - k32.GetTickCount64() / 1000
    except Exception:
        return None


_PROCESOS = {"startmenuexperiencehost.exe": "desde el menú Inicio", "shutdown.exe": "con el comando shutdown",
             "mousocoreworker.exe": "por Windows Update", "trustedinstaller.exe": "por Windows Update",
             "musnotificationux.exe": "por Windows Update", "explorer.exe": "desde Windows"}


def motivo_corte(desde_epoch):
    """Por qué se cortó la grabación, según el registro de eventos de Windows desde poco antes del
    corte: un reinicio o apagado pedido (y por quién), un apagado inesperado (luz, batería, cuelgue)
    o una suspensión. Si Windows no registró nada, el programa se cerró solo o lo cerraron."""
    desde = dt.datetime.fromtimestamp(desde_epoch - 120, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    consulta = (f"*[System[(EventID=1074 or EventID=41 or EventID=6008 or EventID=42) "
                f"and TimeCreated[@SystemTime>='{desde}']]]")
    try:
        r = correr_oculto(["wevtutil", "qe", "System", f"/q:{consulta}", "/f:xml", "/c:20"])
        texto = r.stdout.decode("utf-8", errors="replace")
        ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
        eventos = []
        for ev in ET.fromstring(f"<r>{texto}</r>"):
            eid = int(ev.find("e:System/e:EventID", ns).text)
            hora = ev.find("e:System/e:TimeCreated", ns).get("SystemTime", "")
            datos = {d.get("Name"): (d.text or "") for d in ev.findall("e:EventData/e:Data", ns)}
            eventos.append((hora, eid, datos))
        eventos.sort()
        for _, eid, d in eventos:
            if eid == 1074:
                tipo = (d.get("param5") or "reinicio o apagado").strip().lower()
                # «C:\...\StartMenuExperienceHost.exe (NOMBRE-DEL-PC)»
                proceso = os.path.basename(re.sub(r"\s*\(.*\)\s*$", "", d.get("param1") or "")).lower()
                quien = _PROCESOS.get(proceso, f"por {proceso}" if proceso else "")
                usuario = (d.get("param7") or "").split("\\")[-1]
                return " ".join(x for x in (f"{tipo} de Windows", quien, f"(usuario {usuario})" if usuario else "") if x)
            if eid in (41, 6008):
                return "apagado inesperado de Windows (corte de luz, batería agotada o el PC se colgó)"
            if eid == 42:
                return "suspensión del PC"
    except Exception:
        pass
    arranque = ultimo_arranque()
    if arranque and arranque > desde_epoch:
        return "Windows se reinició"
    return "el programa se cerró (una caída o lo cerraron desde el Administrador de tareas)"
