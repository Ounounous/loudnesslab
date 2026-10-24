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
import queue
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


def _reproduciendo(sesion):
    try:
        return int(sesion.get_playback_info().playback_status) == 4
    except Exception:
        return False


def elegir(items, filtro_app, nombre, sonando):
    """Con varias apps que calzan con el filtro gana la de nombre exacto y, si no hay, la que está
    sonando: «chrome» (YouTube en el navegador) también calza con las apps web instaladas desde
    Chrome, como «Chrome._crx_…» de Soundtrack, y no deben confundirse."""
    f = filtro_app.lower()
    calzan = [x for x in items if f in (nombre(x) or "").lower()]
    for x in calzan:
        if (nombre(x) or "").lower() == f:
            return x
    for x in calzan:
        if sonando(x):
            return x
    return calzan[0] if calzan else None


def elegir_sesion(sesiones, filtro_app):
    return elegir(sesiones, filtro_app, lambda s: s.source_app_user_model_id, _reproduciendo)


def elegir_info(apps, filtro_app):
    """Lo mismo sobre lo que devuelve listar_apps()."""
    return elegir(apps, filtro_app, lambda a: a["app"], lambda a: a["estado"] == "reproduciendo")


async def leer_media(mgr, filtro_app):
    """La sesión de la app elegida. Con un filtro, si esa app no está, devuelve None: no cae en
    otra app que esté sonando, porque registraría sus canciones como si fueran de la elegida."""
    if filtro_app:
        sesion = elegir_sesion(list(mgr.get_sessions()), filtro_app)
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
        self.marcas = queue.Queue()

    def marcar(self, evento, posicion, info=None):
        """Pide anotar «pausa», «reanuda» o «alerta» en la posición dada (la del momento de la orden: el
        hilo la escribe unas décimas después, cuando el audio ya pudo avanzar). `info`: más columnas
        (la alerta lleva su texto en «estado»)."""
        self.marcas.put((evento, ahora_iso(), posicion, info or {}))

    def _registrar(self, w, f, evento, info, posicion=None, hora=None):
        fg, arch, fa = posicion or self.obtener_posicion()
        fila = {"hora_pc": hora or ahora_iso(), "frame_global": fg, "archivo": arch,
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
            en_pausa = False
            while not self.detener.is_set():
                # Las marcas van primero: lo que se lea después ya es posterior a ellas.
                while True:
                    try:
                        evento, hora, pos, info = self.marcas.get_nowait()
                    except queue.Empty:
                        break
                    if evento not in ("pausa", "reanuda"):     # una alerta: solo queda anotada
                        fg, arch, fa = pos
                        w.writerow({"hora_pc": hora, "frame_global": fg, "archivo": arch, "frame_en_archivo": fa,
                                    "evento": evento, **info})
                        f.flush()
                        continue
                    self._registrar(w, f, evento, info, pos, hora)
                    en_pausa = evento == "pausa"
                    if not en_pausa:
                        ultimo = "inicio"   # al reanudar se vuelve a anotar qué canción suena
                if en_pausa:
                    self.detener.wait(self.intervalo)
                    continue
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
        self.marcas = queue.Queue()

    def marcar(self, evento, posicion, info=None):
        self.marcas.put((evento, ahora_iso(), posicion, info or {}))

    def run(self):
        leidas = 0
        en_pausa = False
        nuevo = not os.path.exists(self.ruta)
        with open(self.ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            while not self.detener.is_set():
                while not self.marcas.empty():
                    evento, hora, (fg, arch, fpos), info = self.marcas.get_nowait()
                    fila = {"hora_pc": hora, "frame_global": fg, "archivo": arch, "frame_en_archivo": fpos,
                            "evento": evento, **info}
                    w.writerow(fila)
                    f.flush()
                    if evento not in ("pausa", "reanuda"):
                        continue
                    en_pausa = evento == "pausa"
                    if self.al_evento:
                        self.al_evento(fila)
                try:
                    with open(self.ruta_avisos, encoding="utf-8") as fa:
                        lineas = [x for x in fa.read().splitlines() if x.strip()]
                except OSError:
                    lineas = []
                if en_pausa:   # los avisos que lleguen en pausa se descartan, como el audio
                    leidas = len(lineas)
                    time.sleep(0.1)
                    continue
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
