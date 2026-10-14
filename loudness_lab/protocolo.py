"""Cómo se encuentran la ventana y el motor.

El motor escucha en un puerto local y anota en motor_activo.json su proceso, el puerto y una
clave. La ventana lee ese archivo para conectarse; así una ventana nueva puede volver a
conectarse a una grabación que ya estaba en curso.

sesion_abierta.json dice qué sesión está abierta, y si estaba grabando o en pausa. Lo escribe el
motor y solo se borra al detener la sesión (o al terminar su duración): mientras exista, la sesión
sigue viva aunque el motor no, y el próximo motor que arranque (lanzado por la ventana o por el
guardián de Windows) la retoma.
"""

import json
import os
import time
from multiprocessing.connection import Client

from .sistema import carpeta_app, proceso_vivo


def ruta_activo():
    return carpeta_app() / "motor_activo.json"


def ruta_error():
    return carpeta_app() / "motor_error.txt"


def ruta_abierta():
    return carpeta_app() / "sesion_abierta.json"


def _escribir(ruta, info):
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ruta)


def escribir_activo(info):
    _escribir(ruta_activo(), info)


def _misma(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def escribir_abierta(carpeta, estado, extra=None):
    """estado: «grabando» o «pausada», el que debe retomar el próximo motor."""
    _escribir(ruta_abierta(), {"carpeta": carpeta, "estado": estado, "extra": extra or {},
                               "pid": os.getpid(), "actualizado": time.time()})


def leer_abierta():
    try:
        d = json.loads(ruta_abierta().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("carpeta") else None
    except (OSError, ValueError):
        return None


def borrar_abierta(carpeta=None):
    """Borra el aviso de sesión abierta; con `carpeta`, solo si es de esa sesión."""
    d = leer_abierta()
    if d and (carpeta is None or _misma(d["carpeta"], carpeta)):
        try:
            ruta_abierta().unlink()
        except OSError:
            pass


def sesion_abierta():
    """La sesión abierta que hay que retomar, o None. Si el aviso quedó de una sesión que ya terminó
    (o que ya no existe), lo limpia. Si su carpeta no está al alcance por ahora, no la borra."""
    d = leer_abierta()
    if not d:
        return None
    carpeta = d["carpeta"]
    if not os.path.isdir(os.path.dirname(os.path.normpath(carpeta))):
        return None      # la unidad de la carpeta todavía no está disponible
    try:
        with open(os.path.join(carpeta, "sesion.json"), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = None
    if not isinstance(s, dict) or s.get("fin_pc"):
        borrar_abierta(carpeta)
        return None
    return d


def leer_activo():
    try:
        return json.loads(ruta_activo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def borrar_activo(pid):
    """Borra el aviso solo si es de este proceso: no pisa el de otro motor."""
    info = leer_activo()
    if info and info.get("pid") == pid:
        try:
            ruta_activo().unlink()
        except OSError:
            pass


def motor_vivo():
    """El motor en curso, o None. Si el aviso quedó de un motor que ya murió, lo limpia."""
    info = leer_activo()
    if not info:
        return None
    if proceso_vivo(info.get("pid", 0)):
        return info
    try:
        ruta_activo().unlink()
    except OSError:
        pass
    return None


def conectar(info):
    return Client(("127.0.0.1", int(info["puerto"])), authkey=bytes.fromhex(info["clave"]))
