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


def comando_motor(*args):
    """Como comando_propio, pero empaquetada usa LoudnessLab-motor.exe (el mismo programa con otro nombre), para
    que en el Administrador de tareas el motor no se confunda con la ventana y no se cierre por error."""
    if getattr(sys, "frozen", False):
        motor = Path(sys.executable).with_name("LoudnessLab-motor.exe")
        if motor.exists():
            return [str(motor), *args]
    return comando_propio(*args)


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


# ------------------------------------------------------------------------------ avisos de Windows al motor
WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
WM_QUERYENDSESSION, WM_ENDSESSION, WM_POWERBROADCAST = 0x0011, 0x0016, 0x0218
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x0004, 0x0007, 0x0012
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def _user32():
    u = ctypes.windll.user32
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = _LRESULT
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    u.RegisterClassW.restype = wintypes.ATOM
    u.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    u.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = _LRESULT
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    u.DestroyWindow.argtypes = [wintypes.HWND]
    return u


class VigiaWindows:
    """Una ventana oculta, en su propio hilo, para que el motor (que no tiene ventana) reciba los avisos de
    Windows:
      - apagado, reinicio o cierre de sesión: si `bloquear()`, Windows muestra `motivo()` en su pantalla de
        apagado («Loudness Lab está grabando…») y deja elegir entre cancelar o apagar de todos modos; si se
        apaga, llama a `al_apagar()` y espera a que vuelva (el motor cierra limpio: el corte queda con la hora
        exacta) antes de dejar que Windows siga;
      - suspensión y vuelta: `al_suspender()` y `al_despertar()` (el motor anota el tramo como un corte)."""

    CLASE = "LoudnessLabVigia"

    def __init__(self, motivo, al_apagar, al_suspender=None, al_despertar=None, bloquear=lambda: True):
        import threading
        self.motivo, self.al_apagar, self.bloquear = motivo, al_apagar, bloquear
        self.al_suspender, self.al_despertar = al_suspender, al_despertar
        self.hwnd = None
        self.lista = threading.Event()
        self.hilo = threading.Thread(target=self._correr, daemon=True, name="vigia-windows")

    def iniciar(self, espera_s=3.0):
        self.hilo.start()
        self.lista.wait(espera_s)
        return bool(self.hwnd)

    def _proc(self, hwnd, msg, wp, lp):
        u = self._u
        try:
            if msg == WM_QUERYENDSESSION:
                if self.bloquear():
                    u.ShutdownBlockReasonCreate(hwnd, self.motivo())
                    return 0          # FALSE: Windows muestra el motivo y deja «Apagar de todos modos»
                return 1
            if msg == WM_ENDSESSION:
                u.ShutdownBlockReasonDestroy(hwnd)
                if wp:                 # la sesión termina de verdad: cerrar limpio antes de volver
                    self.al_apagar()
                return 0
            if msg == WM_POWERBROADCAST:
                if wp == PBT_APMSUSPEND and self.al_suspender:
                    self.al_suspender()
                elif wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND) and self.al_despertar:
                    self.al_despertar()
                return 1
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
        except Exception:
            pass                       # un error aquí no puede tumbar al motor
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    def _correr(self):
        try:
            self._u = u = _user32()
            self._wndproc = _WNDPROC(self._proc)      # (se guarda: si se libera, Windows llamaría a la nada)
            inst = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = inst
            wc.lpszClassName = self.CLASE
            u.RegisterClassW(ctypes.byref(wc))         # (si ya estaba registrada, CreateWindow igual la encuentra)
            self.hwnd = u.CreateWindowExW(0, self.CLASE, "Loudness Lab (motor de grabación)", 0, 0, 0, 0, 0,
                                          None, None, inst, None)
        finally:
            self.lista.set()
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def detener(self):
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self.hilo.join(2)


def notificar(titulo, texto):
    """Una notificación de Windows (aparece aunque la ventana de Loudness Lab esté cerrada). La muestra
    PowerShell con las notificaciones de Windows, sin abrir ninguna ventana y sin esperar."""
    import base64
    from xml.sax.saxutils import escape
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(titulo)}</text>"
           f"<text>{escape(texto)}</text></binding></visual></toast>")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
          " > $null; [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]"
          " > $null; $x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml(\"" + xml.replace('"', "'") + "\");"
          " [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                          base64.b64encode(ps.encode("utf-16-le")).decode()],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW)
        return True
    except OSError:
        return False
