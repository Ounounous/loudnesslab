"""
grabar.py - Graba la salida de audio de Windows (WASAPI loopback) durante horas o días.

- Un archivo WAV 32-bit float por hora (corte exacto por muestra, sin perder audio).
- Registra cada cambio de canción (artista/título) desde el panel multimedia de Windows,
  con hora real y posición exacta en muestras.
- Registra cada minuto la relación reloj del PC vs. muestras grabadas (para medir desfase).
- Reproduce silencio en la misma salida para que WASAPI no deje de entregar audio
  cuando la música se pausa (si no, el conteo de muestras se desfasaría del tiempo real).

Uso:
    python grabar.py --listar                      # ver dispositivos de salida
    python grabar.py --probar-metadata             # ver qué canción detecta Windows (Ctrl+C para salir)
    python grabar.py --etiqueta soundtrack_lounge  # grabar hasta Ctrl+C
    python grabar.py --etiqueta spotify_pop --horas 24
"""

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import queue
import shutil
import sys
import threading
import time

import numpy as np

SEG_POR_ARCHIVO = 3600  # 1 hora


def ahora_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# ----------------------------------------------------------------------------
# Metadata: Windows Global System Media Transport Controls (SMTC)
# ----------------------------------------------------------------------------
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


ESTADOS = {0: "cerrado", 1: "abierto", 2: "cambiando", 3: "detenido", 4: "reproduciendo", 5: "pausado"}


def _segundos(x):
    if x is None:
        return None
    if hasattr(x, "total_seconds"):
        return x.total_seconds()
    if hasattr(x, "duration"):  # TimeSpan antiguo: unidades de 100 ns
        return x.duration / 1e7
    return None


async def leer_media(Manager, filtro_app):
    mgr = await Manager.request_async()
    sesiones = list(mgr.get_sessions())
    sesion = None
    if filtro_app:
        for s in sesiones:
            if filtro_app.lower() in (s.source_app_user_model_id or "").lower():
                sesion = s
                break
    if sesion is None:
        sesion = mgr.get_current_session()
    if sesion is None:
        return None, [s.source_app_user_model_id for s in sesiones]
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
    return {
        "app": sesion.source_app_user_model_id,
        "artista": props.artist or "",
        "titulo": props.title or "",
        "album": props.album_title or "",
        "estado": estado,
        "posicion_s": pos,
        "duracion_s": dur,
    }, [s.source_app_user_model_id for s in sesiones]


def probar_metadata(filtro_app):
    Manager = _importar_smtc()
    if Manager is None:
        print("No está instalado el paquete winrt. Ejecuta: pip install -r requirements.txt")
        return
    print("Leyendo el panel multimedia de Windows cada 1 s (Ctrl+C para salir)...\n")
    ultimo = None
    try:
        while True:
            info, apps = asyncio.run(leer_media(Manager, filtro_app))
            clave = None if info is None else (info["app"], info["artista"], info["titulo"], info["estado"])
            if clave != ultimo:
                print(f"[{ahora_iso()}] apps con sesión: {apps}")
                print(f"    -> {info}\n")
                ultimo = clave
            time.sleep(1)
    except KeyboardInterrupt:
        pass


class HiloMetadata(threading.Thread):
    """Consulta la canción actual cada `intervalo` s y escribe una fila por cada cambio."""

    def __init__(self, ruta_csv, obtener_posicion, filtro_app, intervalo=0.5):
        super().__init__(daemon=True)
        self.ruta = ruta_csv
        self.obtener_posicion = obtener_posicion  # devuelve (frame_global, archivo, frame_en_archivo)
        self.filtro_app = filtro_app
        self.intervalo = intervalo
        self.detener = threading.Event()
        self.Manager = _importar_smtc()
        self.errores = 0

    def run(self):
        campos = ["hora_pc", "frame_global", "archivo", "frame_en_archivo", "evento",
                  "app", "artista", "titulo", "album", "estado", "posicion_s", "duracion_s"]
        nuevo = not os.path.exists(self.ruta)
        with open(self.ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            if nuevo:
                w.writeheader()
            if self.Manager is None:
                print("[metadata] winrt no instalado: NO se registrarán las canciones.")
                return
            loop = asyncio.new_event_loop()
            ultimo = None
            while not self.detener.is_set():
                try:
                    info, _ = loop.run_until_complete(leer_media(self.Manager, self.filtro_app))
                except Exception as e:
                    self.errores += 1
                    if self.errores <= 5:
                        print(f"[metadata] error: {e}")
                    info = None
                clave = None if info is None else (info["app"], info["artista"], info["titulo"], info["estado"])
                if clave != ultimo:
                    fg, arch, fa = self.obtener_posicion()
                    if info is None:
                        evento, info = "sin_sesion", {}
                    elif ultimo is None or ultimo[:3] != clave[:3]:
                        evento = "cambio_pista"
                    else:
                        evento = "cambio_estado"
                    w.writerow({"hora_pc": ahora_iso(), "frame_global": fg, "archivo": arch,
                                "frame_en_archivo": fa, "evento": evento, **info})
                    f.flush()
                    if evento == "cambio_pista":
                        print(f"[{dt.datetime.now():%H:%M:%S}] {info.get('artista')} - {info.get('titulo')}")
                    ultimo = clave
                self.detener.wait(self.intervalo)
            loop.close()


# ----------------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------------
def abrir_pyaudio():
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        print("Falta PyAudioWPatch. Ejecuta: pip install -r requirements.txt")
        sys.exit(1)
    return pyaudio


def buscar_loopback(p, pyaudio, nombre=None):
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    salida = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    objetivo = nombre or salida["name"]
    for lb in p.get_loopback_device_info_generator():
        if objetivo.lower() in lb["name"].lower():
            # buscar el dispositivo de salida real (para el keep-alive de silencio)
            real = salida
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if (d["hostApi"] == wasapi["index"] and d["maxOutputChannels"] > 0
                        and not d.get("isLoopbackDevice") and d["name"] in lb["name"]):
                    real = d
                    break
            return lb, real
    raise SystemExit(f"No encontré un dispositivo loopback que contenga '{objetivo}'. Usa --listar.")


def listar():
    pyaudio = abrir_pyaudio()
    p = pyaudio.PyAudio()
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    print("Salida por defecto:", p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"])
    print("\nDispositivos loopback disponibles:")
    for lb in p.get_loopback_device_info_generator():
        print(f"  - {lb['name']}  ({int(lb['defaultSampleRate'])} Hz, {lb['maxInputChannels']} canales)")
    p.terminate()


class Grabador:
    def __init__(self, carpeta, sr, canales):
        import soundfile as sf
        self.sf = sf
        self.carpeta = carpeta
        self.sr = sr
        self.canales = canales
        self.frames_por_archivo = sr * SEG_POR_ARCHIVO
        self.frame_global = 0
        self.frame_en_archivo = 0
        self.n_archivo = 0
        self.archivo = None
        self.nombre_archivo = ""
        self.archivos = []
        self.lock = threading.Lock()

    def _abrir_nuevo(self):
        if self.archivo is not None:
            self.archivo.close()
        self.n_archivo += 1
        self.nombre_archivo = f"audio_{self.n_archivo:04d}_{dt.datetime.now():%Y%m%d_%H%M%S}.wav"
        self.archivo = self.sf.SoundFile(os.path.join(self.carpeta, self.nombre_archivo), "w",
                                         samplerate=self.sr, channels=self.canales,
                                         subtype="FLOAT", format="WAV")
        self.frame_en_archivo = 0
        self.archivos.append({"archivo": self.nombre_archivo, "frame_global_inicio": self.frame_global,
                              "hora_pc_inicio": ahora_iso()})

    def escribir(self, bloque):
        while len(bloque):
            if self.archivo is None or self.frame_en_archivo >= self.frames_por_archivo:
                self._abrir_nuevo()
            cabe = self.frames_por_archivo - self.frame_en_archivo
            parte, bloque = bloque[:cabe], bloque[cabe:]
            self.archivo.write(parte)
            with self.lock:
                self.frame_en_archivo += len(parte)
                self.frame_global += len(parte)

    def posicion(self):
        with self.lock:
            return self.frame_global, self.nombre_archivo, self.frame_en_archivo

    def cerrar(self):
        if self.archivo is not None:
            self.archivo.close()
            self.archivo = None


def grabar(args):
    pyaudio = abrir_pyaudio()
    p = pyaudio.PyAudio()
    lb, salida_real = buscar_loopback(p, pyaudio, args.dispositivo)
    sr = int(lb["defaultSampleRate"])
    canales = int(lb["maxInputChannels"])

    marca = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    carpeta = os.path.join(args.destino, f"{args.etiqueta}_{marca}")
    os.makedirs(carpeta, exist_ok=True)

    gb_hora = sr * canales * 4 * 3600 / 1e9
    libre = shutil.disk_usage(carpeta).free / 1e9
    print(f"Dispositivo: {lb['name']}  |  {sr} Hz, {canales} canales, 32-bit float")
    print(f"Carpeta: {carpeta}")
    print(f"Espacio: ~{gb_hora:.2f} GB por hora; libre {libre:.0f} GB (~{libre / gb_hora:.0f} h)")
    if args.horas and args.horas * gb_hora > libre * 0.95:
        print("ADVERTENCIA: no alcanza el espacio para la duración pedida.")

    grabador = Grabador(carpeta, sr, canales)
    cola = queue.Queue()
    contadores = {"overflow": 0}

    def cb_entrada(in_data, frame_count, time_info, status):
        if status:
            contadores["overflow"] += 1
        cola.put(in_data)
        return (None, pyaudio.paContinue)


    def cb_salida(in_data, frame_count, time_info, status):
        return (bytes(4 * salida_real["maxOutputChannels"] * frame_count), pyaudio.paContinue)

    sesion = {
        "etiqueta": args.etiqueta, "notas": args.notas, "inicio_pc": ahora_iso(),
        "dispositivo": lb["name"], "sample_rate": sr, "canales": canales, "formato": "WAV float32",
        "segundos_por_archivo": SEG_POR_ARCHIVO, "filtro_app": args.app,
    }

    def guardar_sesion(fin=False):
        s = dict(sesion, archivos=grabador.archivos, frames_totales=grabador.frame_global,
                 segundos_totales=grabador.frame_global / sr, overflows=contadores["overflow"])
        if fin:
            s["fin_pc"] = ahora_iso()
        with open(os.path.join(carpeta, "sesion.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)

    # Keep-alive: silencio en la salida para que el loopback no se detenga en pausas
    stream_silencio = None
    if not args.sin_keepalive:
        try:
            stream_silencio = p.open(format=pyaudio.paFloat32, channels=salida_real["maxOutputChannels"],
                                     rate=int(salida_real["defaultSampleRate"]), output=True,
                                     output_device_index=salida_real["index"], stream_callback=cb_salida)
        except Exception as e:
            print(f"Aviso: no pude abrir el keep-alive de silencio ({e}). Si la música se pausa, "
                  f"el tiempo de las muestras se desfasará del reloj.")

    stream = p.open(format=pyaudio.paFloat32, channels=canales, rate=sr, input=True,
                    input_device_index=lb["index"], frames_per_buffer=1024, stream_callback=cb_entrada)

    meta = HiloMetadata(os.path.join(carpeta, "pistas.csv"), grabador.posicion, args.app)
    meta.start()

    f_reloj = open(os.path.join(carpeta, "reloj.csv"), "a", newline="", encoding="utf-8")
    w_reloj = csv.writer(f_reloj)
    w_reloj.writerow(["hora_pc", "epoch_s", "frame_global", "segundos_audio", "desfase_s"])

    t0 = time.time()
    fin = t0 + args.horas * 3600 if args.horas else None
    ult_reloj = 0
    print("\nGrabando... (Ctrl+C para terminar)\n")
    try:
        while True:
            try:
                datos = cola.get(timeout=1)
            except queue.Empty:
                datos = None
            if datos:
                bloque = np.frombuffer(datos, dtype=np.float32).reshape(-1, canales)
                grabador.escribir(bloque)
            ahora = time.time()
            if ahora - ult_reloj >= 60:
                fg = grabador.frame_global
                w_reloj.writerow([ahora_iso(), f"{ahora:.3f}", fg, f"{fg / sr:.3f}",
                                  f"{(ahora - t0) - fg / sr:.3f}"])
                f_reloj.flush()
                if grabador.archivo is not None:
                    grabador.archivo.flush()
                guardar_sesion()
                h = fg / sr / 3600
                print(f"[{dt.datetime.now():%H:%M:%S}] {h:.2f} h grabadas | desfase reloj "
                      f"{(ahora - t0) - fg / sr:+.2f} s | overflows {contadores['overflow']}")
                ult_reloj = ahora
            if fin and ahora >= fin:
                break
    except KeyboardInterrupt:
        print("\nDeteniendo...")
    finally:
        stream.stop_stream()
        stream.close()
        if stream_silencio is not None:
            stream_silencio.stop_stream()
            stream_silencio.close()
        while not cola.empty():
            grabador.escribir(np.frombuffer(cola.get(), dtype=np.float32).reshape(-1, canales))
        meta.detener.set()
        meta.join(timeout=3)
        grabador.cerrar()
        f_reloj.close()
        guardar_sesion(fin=True)
        p.terminate()
        print(f"Listo. {grabador.frame_global / sr / 3600:.2f} h en {carpeta}")


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Graba la salida de audio de Windows con metadata de pistas.")
    ap.add_argument("--etiqueta", default="sesion", help="nombre corto, ej. soundtrack_lounge")
    ap.add_argument("--notas", default="", help="texto libre: playlist, configuración, etc.")
    ap.add_argument("--horas", type=float, default=0, help="duración; 0 = hasta Ctrl+C")
    ap.add_argument("--destino", default="grabaciones", help="carpeta donde guardar")
    ap.add_argument("--dispositivo", default=None, help="parte del nombre de la salida (por defecto la predeterminada)")
    ap.add_argument("--app", default="soundtrack", help="filtro del nombre de la app para la metadata ('' = la sesión activa)")
    ap.add_argument("--sin-keepalive", action="store_true", help="no reproducir silencio de fondo")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--probar-metadata", action="store_true")
    args = ap.parse_args()

    if args.listar:
        listar()
    elif args.probar_metadata:
        probar_metadata(args.app)
    else:
        grabar(args)


if __name__ == "__main__":
    main()
