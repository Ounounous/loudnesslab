"""Cómo se encuentran la ventana y el motor.

El motor escucha en un puerto local y anota en motor_activo.json su proceso, el puerto y una
clave. La ventana lee ese archivo para conectarse; así una ventana nueva puede volver a
conectarse a una grabación que ya estaba en curso.
"""

import json
import os
from multiprocessing.connection import Client

from .sistema import carpeta_app, proceso_vivo


def ruta_activo():
    return carpeta_app() / "motor_activo.json"


def ruta_error():
    return carpeta_app() / "motor_error.txt"


def escribir_activo(info):
    tmp = ruta_activo().with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ruta_activo())


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
