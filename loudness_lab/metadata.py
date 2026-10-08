"""Qué canción está sonando, según el panel multimedia de Windows (SMTC).

Escribe pistas.csv con el mismo formato que grabar.py, así analizar.py lee igual las sesiones
de la app y las del script.
"""

import asyncio
import csv
import datetime as dt
import json
import logging
import os
import threading
import time

log = logging.getLogger("loudness.metadata")

ESTADOS = {0: "cerrado", 1: "abierto", 2: "cambiando", 3: "detenido", 4: "reproduciendo", 5: "pausado"}
CAMPOS = ["hora_pc", "frame_global", "archivo", "frame_en_archivo", "evento",
          "app", "artista", "titulo", "album", "estado", "posicion_s", "duracion_s"]


def ahora_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _importar_smtc():
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        return Manager
    except ImportError:
        pass
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        return Manager
    except ImportError:
        return None


def _segundos(x):
    if x is None:
        return None
    if hasattr(x, "total_seconds"):
        return x.total_seconds()
    if hasattr(x, "duration"):  # TimeSpan antiguo: unidades de 100 ns
        return x.duration / 1e7
    return None


async def _info_sesion(sesion):
    props = await sesion.try_get_media_properties_async()
    try:
        estado = ESTADOS.get(int(sesion.get_playback_info().playback_status), "?")
    except Exception:
        estado = "?"
    pos = dur = None
    try:
        tl = sesion.get_timeline_properties()
        pos = _segundos(tl.position)
        dur = _segundos(tl.end_time)
    except Exception:
        pass
    return {"app": sesion.source_app_user_model_id, "artista": props.artist or "",
            "titulo": props.title or "", "album": props.album_title or "", "estado": estado,
            "posicion_s": pos, "duracion_s": dur}


async def leer_media(mgr, filtro_app):
    """La sesión de la app elegida. Con un filtro, si esa app no está, devuelve None: no cae en
    otra app que esté sonando, porque registraría sus canciones como si fueran de la elegida."""
    sesiones = list(mgr.get_sessions())
    sesion = None
    if filtro_app:
        for s in sesiones:
            if filtro_app.lower() in (s.source_app_user_model_id or "").lower():
                sesion = s
                break
    else:
        sesion = mgr.get_current_session()
    if sesion is None:
        return None
    return await _info_sesion(sesion)


def listar_apps():
    """Apps que publican qué están reproduciendo. None si falta el paquete winrt."""
    Manager = _importar_smtc()
    if Manager is None:
        return None

    async def _listar():
        mgr = await Manager.request_async()
        return [await _info_sesion(s) for s in mgr.get_sessions()]

    return asyncio.run(_listar())


class HiloMetadata(threading.Thread):
    """Consulta la canción actual cada `intervalo` s y escribe una fila por cada cambio."""

    def __init__(self, ruta_csv, obtener_posicion, filtro_app, al_evento=None, intervalo=0.5):
        super().__init__(daemon=True, name="metadata")
        self.ruta = ruta_csv
        self.obtener_posicion = obtener_posicion  # devuelve (frame_global, archivo, frame_en_archivo)
        self.filtro_app = filtro_app
        self.al_evento = al_evento
        self.intervalo = intervalo
        self.detener = threading.Event()
        self.errores = 0

    def _registrar(self, w, f, evento, info):
        fg, arch, fa = self.obtener_posicion()
        fila = {"hora_pc": ahora_iso(), "frame_global": fg, "archivo": arch,
                "frame_en_archivo": fa, "evento": evento, **info}
        w.writerow(fila)
        f.flush()
        if self.al_evento:
            self.al_evento(fila)

    def run(self):
        Manager = _importar_smtc()
        nuevo = not os.path.exists(self.ruta)
        with open(self.ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            if Manager is None:
                log.error("Falta el paquete winrt: no se registrarán las canciones.")
                return
            loop = asyncio.new_event_loop()
            mgr = None
            ultimo = "inicio"
            while not self.detener.is_set():
                try:
                    if mgr is None:
                        mgr = loop.run_until_complete(Manager.request_async())
                    info = loop.run_until_complete(leer_media(mgr, self.filtro_app))
                except Exception as e:
                    self.errores += 1
                    if self.errores <= 5:
                        log.warning("Error leyendo la metadata de Windows: %s", e)
                    mgr, info = None, None
                clave = None if info is None else (info["app"], info["artista"], info["titulo"], info["estado"])
                if clave != ultimo:
                    if info is None:
                        evento, datos = "sin_sesion", {}
                    elif ultimo in (None, "inicio") or ultimo[:3] != clave[:3]:
                        evento, datos = "cambio_pista", info
                    else:
                        evento, datos = "cambio_estado", info
                    self._registrar(w, f, evento, datos)
                    ultimo = clave
                self.detener.wait(self.intervalo)
            loop.close()


class HiloSimulado(threading.Thread):
    """Solo para pruebas: en vez de leer Windows, toma los avisos de un archivo de texto (una
    línea JSON por canción con artista y título) a medida que alguien los escribe."""

    def __init__(self, ruta_csv, obtener_posicion, ruta_avisos, al_evento=None):
        super().__init__(daemon=True, name="metadata-simulada")
        self.ruta = ruta_csv
        self.obtener_posicion = obtener_posicion
        self.ruta_avisos = ruta_avisos
        self.al_evento = al_evento
        self.detener = threading.Event()

    def run(self):
        leidas = 0
        with open(self.ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            w.writeheader()
            while not self.detener.is_set():
                try:
                    with open(self.ruta_avisos, encoding="utf-8") as fa:
                        lineas = [x for x in fa.read().splitlines() if x.strip()]
                except OSError:
                    lineas = []
                for linea in lineas[leidas:]:
                    d = json.loads(linea)
                    fg, arch, fpos = self.obtener_posicion()
                    fila = {"hora_pc": ahora_iso(), "frame_global": fg, "archivo": arch,
                            "frame_en_archivo": fpos, "evento": d.get("evento", "cambio_pista"),
                            "app": "simulada", "artista": d.get("artista", ""), "titulo": d.get("titulo", ""),
                            "album": "", "estado": d.get("estado", "reproduciendo"),
                            "posicion_s": "", "duracion_s": ""}
                    w.writerow(fila)
                    f.flush()
                    if self.al_evento:
                        self.al_evento(fila)
                leidas = len(lineas)
                time.sleep(0.1)
