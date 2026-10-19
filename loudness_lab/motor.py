"""Motor de grabación. Corre en un proceso aparte de la ventana.

Captura el audio por loopback, lee qué canción suena, mide en vivo, escribe todo a disco y
publica el estado a la ventana. Va aparte para que la grabación no dependa de la ventana: si
se cierra o se cuelga, la grabación sigue, y una ventana nueva se puede volver a conectar.

Sesión en disco (compatible con analizar.py):
  audio_0001_....wav ...  un WAV float32 por hora
  pistas.csv              cada cambio de canción, con su posición exacta en muestras
  reloj.csv               cada minuto, reloj del PC vs. muestras grabadas
  mediciones.f32          por bloque de 100 ms: energía K de cada canal, pico de muestra, true peak
  sesion.json             dispositivo, formato, condiciones del experimento y resumen en vivo
  registro.log            lo mismo que muestra el registro de la ventana
  latido.json             cada 5 s: hasta dónde llegó la grabación y a qué hora (para medir un corte)

Una sesión queda abierta hasta que se detiene (o se cumple su duración). Si el motor muere antes
(un reinicio, un corte de luz, una caída), la sesión no termina: el próximo motor la retoma con
--continuar, repara lo que quedó a medias y registra el tramo sin grabar como un «corte».
"""

import argparse
import csv
import datetime as dt
import json
import logging
import os
import platform
import queue
import secrets
import threading
import time
import traceback
from collections import deque
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path

import numpy as np

from . import VERSION, dispositivos, protocolo, recuperar, sesiones, sistema
from .bs1770 import MedidorBloques
from .metadata import CAMPOS, HiloMetadata, HiloSimulado
from .picos import SEG_ANTES, SEG_DESPUES, GuardaPicos
from .radio import HiloRadio
from .seguimiento import Seguidor, Serie

SEG_POR_ARCHIVO = 3600
DISCO_MINIMO_GB = 2.0   # bajo esto se detiene, para no dejar archivos a medias con el disco lleno
DISCO_AVISO_GB = 20.0
SIN_DATOS_S = 5.0       # el silencio de fondo garantiza un flujo continuo: si no llega nada, se cayó
LATIDO_S = 5.0          # cada cuánto se anota en latido.json hasta dónde llegó la grabación

log = logging.getLogger("loudness")


def ahora_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def iso_de(epoch):
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


def epoch_de(iso):
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def hms_txt(seg):
    seg = int(max(0, seg))
    return f"{seg // 3600}:{seg % 3600 // 60:02d}:{seg % 60:02d}"


# ----------------------------------------------------------------------------
# Escritura
# ----------------------------------------------------------------------------
class Escritor:
    """Un WAV float32 por hora, con corte exacto por muestra: no se pierde ni se repite audio."""

    def __init__(self, carpeta, sr, canales):
        import soundfile as sf
        self.sf = sf
        self.carpeta, self.sr, self.canales = carpeta, sr, canales
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

    def continuar(self, archivos, frame_global):
        """Sigue una sesión cortada: el audio nuevo va a un WAV nuevo, a continuación de los que hay."""
        self.archivos = [{k: a[k] for k in ("archivo", "frame_global_inicio", "hora_pc_inicio")} for a in archivos]
        self.frame_global = frame_global
        self.n_archivo = max((int(a["archivo"][6:10]) for a in archivos), default=0)
        self.nombre_archivo = archivos[-1]["archivo"] if archivos else ""
        self.frame_en_archivo = archivos[-1]["frames"] if archivos else 0

    def posicion(self):
        with self.lock:
            return self.frame_global, self.nombre_archivo, self.frame_en_archivo

    def flush(self):
        if self.archivo is not None:
            self.archivo.flush()

    def cerrar(self):
        if self.archivo is not None:
            self.archivo.close()
            self.archivo = None


# ----------------------------------------------------------------------------
# Escucha por los parlantes
# ----------------------------------------------------------------------------
class Anillo:
    """Buffer circular entre el motor (escribe) y la salida de audio (lee en su propio hilo)."""

    def __init__(self, capacidad, canales):
        self.b = np.zeros((capacidad, canales), dtype=np.float32)
        self.ini = 0
        self.n = 0
        self.lock = threading.Lock()

    def escribir(self, x):
        cap = len(self.b)
        with self.lock:
            if len(x) >= cap:
                x, self.ini, self.n = x[-cap:], 0, 0
            sobra = self.n + len(x) - cap
            if sobra > 0:  # la escucha se atrasó: se descarta lo más antiguo
                self.ini = (self.ini + sobra) % cap
                self.n -= sobra
            fin = (self.ini + self.n) % cap
            k = min(len(x), cap - fin)
            self.b[fin:fin + k] = x[:k]
            self.b[:len(x) - k] = x[k:]
            self.n += len(x)

    def leer(self, k):
        out = np.zeros((k, self.b.shape[1]), dtype=np.float32)
        cap = len(self.b)
        with self.lock:
            m = min(k, self.n)
            a = min(m, cap - self.ini)
            out[:a] = self.b[self.ini:self.ini + a]
            out[a:m] = self.b[:m - a]
            self.ini = (self.ini + m) % cap
            self.n -= m
        return out, m


class Remuestreo:
    """Cambio de frecuencia de muestreo por interpolación lineal, continuo entre llamadas.
    Basta para escuchar; la medición nunca pasa por aquí."""

    def __init__(self, sr_in, sr_out):
        self.paso = sr_in / sr_out
        self.t = 0.0
        self.ultimo = None

    def procesar(self, x):
        if self.ultimo is not None:
            x = np.vstack([self.ultimo, x])
        if len(x) < 2:
            self.ultimo = x
            return np.zeros((0, x.shape[1]), dtype=np.float32)
        pos = np.arange(self.t, len(x) - 1, self.paso)
        i = pos.astype(int)
        f = (pos - i)[:, None]
        y = x[i] * (1 - f) + x[i + 1] * f
        self.t = (pos[-1] + self.paso if len(pos) else self.t) - (len(x) - 1)
        self.ultimo = x[-1:]
        return y.astype(np.float32)


class Monitor:
    """Reproduce lo que se graba en otra salida, solo para escuchar. No toca la medición: el
    volumen se aplica a la copia que va a los parlantes, no a lo que se graba."""

    COLCHON_S = 0.15
    MAX_S = 0.5

    def __init__(self, motor):
        self.m = motor
        self.stream = None
        self.vol = 0.5
        self.nombre = ""
        self.anillo = None
        self.remuestreo = None
        self.cebando = True
        self.ch = 2

    def abrir(self, nombre, vol):
        self.cerrar()
        p, pa = self.m.p, self.m.pa
        d = dispositivos.buscar_salida(p, pa, nombre)
        if d is None:
            log.warning("No encuentro la salida '%s' para escuchar.", nombre)
            return False
        if d["name"] == self.m.nombre_dispositivo:
            log.warning("No se puede escuchar en el mismo dispositivo que se graba: entraría en la medición.")
            return False
        sr, ch = int(d["defaultSampleRate"]), int(d["maxOutputChannels"])
        self.ch = ch
        self.anillo = Anillo(int(sr * self.MAX_S), ch)
        self.remuestreo = Remuestreo(self.m.sr, sr) if sr != self.m.sr else None
        self.cebando = True
        self.vol = float(vol)
        colchon = int(self.COLCHON_S * sr)

        def cb(_i, n, _t, _s):
            if self.cebando and self.anillo.n < colchon:
                return (bytes(4 * ch * n), pa.paContinue)
            self.cebando = False
            x, leidas = self.anillo.leer(n)
            if leidas < n:  # se vació: vuelve a juntar colchón antes de seguir
                self.cebando = True
            return ((x * self.vol).astype(np.float32).tobytes(), pa.paContinue)

        try:
            self.stream = p.open(format=pa.paFloat32, channels=ch, rate=sr, output=True,
                                 output_device_index=d["index"], stream_callback=cb)
        except Exception as e:
            log.warning("No pude abrir %s para escuchar: %s", d["name"], e)
            self.stream = None
            return False
        self.nombre = d["name"]
        log.info("Escuchando en %s (volumen %d %%).", d["name"], round(self.vol * 100))
        return True

    def alimentar(self, bloque):
        if self.stream is None:
            return
        x = self.remuestreo.procesar(bloque) if self.remuestreo else bloque
        if x.shape[1] != self.ch:
            y = np.zeros((len(x), self.ch), dtype=np.float32)
            k = min(x.shape[1], self.ch)
            y[:, :k] = x[:, :k]
            x = y
        self.anillo.escribir(np.asarray(x, dtype=np.float32))

    def cerrar(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            log.info("Escucha apagada.")


# ----------------------------------------------------------------------------
# Canal con la ventana
# ----------------------------------------------------------------------------
class Publicador:
    """Conexiones con las ventanas. Cada una tiene su cola y su hilo de envío: si una ventana no
    lee, se descartan sus mensajes en vez de frenar la grabación."""

    def __init__(self):
        self.clientes = []
        self.lock = threading.Lock()
        self.nuevas = queue.Queue()     # aceptadas, esperando que el motor arme su snapshot
        self.comandos = queue.Queue()

    def escuchar(self, listener):
        def aceptar():
            while True:
                try:
                    self.nuevas.put(listener.accept())
                except AuthenticationError:
                    continue
                except OSError:
                    break
        threading.Thread(target=aceptar, daemon=True, name="aceptar").start()

    def registrar(self, conn, snapshot):
        cola = queue.Queue(maxsize=3000)
        cola.put(snapshot)
        cliente = {"cola": cola, "vivo": True}

        def emitir():
            while cliente["vivo"]:
                msg = cola.get()
                if msg is None:
                    break
                try:
                    conn.send(msg)
                except Exception:
                    break
            cliente["vivo"] = False
            try:
                conn.close()
            except Exception:
                pass

        def recibir():
            while cliente["vivo"]:
                try:
                    msg = conn.recv()
                except Exception:
                    break
                if isinstance(msg, dict):
                    self.comandos.put(msg)
            cliente["vivo"] = False
            try:
                cola.put_nowait(None)
            except queue.Full:
                pass

        threading.Thread(target=emitir, daemon=True, name="emitir").start()
        threading.Thread(target=recibir, daemon=True, name="recibir").start()
        with self.lock:
            self.clientes.append(cliente)

    def enviar(self, msg):
        with self.lock:
            self.clientes = [c for c in self.clientes if c["vivo"]]
            for c in self.clientes:
                try:
                    c["cola"].put_nowait(msg)
                except queue.Full:
                    pass

    def cerrar(self):
        with self.lock:
            for c in self.clientes:
                try:
                    c["cola"].put_nowait(None)
                except queue.Full:
                    pass


class HandlerVentana(logging.Handler):
    """Lleva el registro a la ventana y guarda las últimas líneas para quien se conecte después."""

    def __init__(self, motor):
        super().__init__(logging.INFO)
        self.m = motor

    def emit(self, record):
        try:
            item = {"tipo": "log", "linea": self.format(record), "nivel": record.levelname}
            self.m.log_reciente.append(item)
            self.m.publicador.enviar(item)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Motor
# ----------------------------------------------------------------------------
class Motor:
    def __init__(self, cfg, continuar=None, estado="grabando"):
        """`continuar`: la carpeta de una sesión abierta que se cortó; `cfg` trae entonces solo lo que
        no es del experimento (la escucha), y `estado` es el que tenía: «grabando» o «pausada»."""
        self.previa = None
        self.estado_deseado = estado
        if continuar:
            self.carpeta = os.path.normpath(continuar)
            previa = sesiones._json(os.path.join(self.carpeta, "sesion.json"))
            if previa is None:
                raise RuntimeError(f"No encuentro la sesión «{self.carpeta}»: falta sesion.json.")
            if previa.get("fin_pc"):
                raise RuntimeError(f"La sesión «{self.carpeta}» ya terminó: no se puede continuar.")
            self.previa = previa
            cfg = {**sesiones._config_de(previa, self.carpeta), **(cfg or {})}
            self.etiqueta = previa.get("etiqueta") or os.path.basename(self.carpeta)
        else:
            # El nombre de la sesión es el de su carpeta. Sin nombre, se usa la fecha para no chocar.
            self.etiqueta = sistema.limpiar_nombre(cfg.get("etiqueta") or f"sesion_{dt.datetime.now():%Y%m%d_%H%M%S}")
            base = Path(cfg.get("carpeta_salida") or sistema.carpeta_salida_por_defecto())
            self.carpeta = str(base / self.etiqueta)
            try:
                os.makedirs(self.carpeta, exist_ok=False)
            except FileExistsError:
                raise FileExistsError(f"Ya existe una sesión llamada «{self.etiqueta}» en {base}. Una sesión "
                                      "detenida no se puede continuar: elige otro nombre.") from None
        self.cfg = cfg
        self.abierta = False       # este motor ya anotó la sesión como abierta (sesion_abierta.json)
        self.cortes = []           # tramos sin grabar porque el motor se cortó (ver _preparar_continuacion)
        self.corte_pendiente = None
        self.reparaciones = []
        self._descartar = 0        # filas del medidor calentado que ya estaban medidas
        self.ult_latido = 0.0
        self.radio = None          # con una radio web, el lector de títulos de su stream (necesita el audio)
        self.cola_audio = queue.Queue()
        self.cola_eventos = queue.Queue()
        self.publicador = Publicador()
        self.log_reciente = deque(maxlen=500)
        self.detener = threading.Event()
        self.motivo_fin = ""
        self.terminado = False
        self.overflows = 0
        self.overflows_aviso = 0
        self.reaperturas = 0
        self.alertas = {}
        self.estado_app = ""
        self.sin_meta_desde = None
        self.curva_t, self.curva_st = Serie(3600), Serie(3600)
        self.curva_pend = []
        self.stats = {}
        self.desfase = 0.0
        self.libre_gb = float("nan")
        self.stream = self.stream_silencio = None
        self.silenciar_hasta = 0
        # Pausa: la captura sigue abierta pero el audio se descarta (ni se escribe ni se mide, y los
        # frames no avanzan). Cada pausa queda en pausas y como eventos «pausa»/«reanuda» en pistas.csv.
        self.pausado = False
        self.pausa_desde = None
        self.pausa_total = 0.0     # segundos pausados, sin contar la pausa en curso
        self.pausas = []

    def _en_pausa_s(self):
        return time.time() - self.pausa_desde if self.pausado else 0.0

    def _segundos_pausados(self):
        return self.pausa_total + self._en_pausa_s()

    def _segundos_cortes(self):
        return sum(c["duracion_s"] for c in self.cortes)

    def _pausas_gui(self):
        """[segundo de audio donde empezó, cuánto duró] de cada tramo sin grabar: las pausas (la actual,
        lo que lleva) y los cortes. Para el gráfico, ambos son un hueco."""
        huecos = [[p["t_s"], p["duracion_s"] if p["duracion_s"] is not None else self._en_pausa_s()]
                  for p in self.pausas]
        huecos += [[c["t_s"], c["duracion_s"]] for c in self.cortes]
        return sorted(huecos, key=lambda h: h[0])

    def _reloj_de(self, frame):
        """La hora (epoch) en que se grabó un frame: el audio no incluye pausas ni cortes."""
        t = frame / self.sr
        return self.t0 + t + sum(d for ts, d in self._pausas_gui() if ts < t)

    def _extra(self):
        """Lo que el próximo motor necesita además de la configuración del experimento."""
        extra = {"monitor": self.monitor.stream is not None, "monitor_vol": self.monitor.vol,
                 "monitor_dispositivo": self.cfg.get("monitor_dispositivo", "")}
        extra.update({k: v for k, v in self.cfg.items() if k.startswith("_")})   # (solo pruebas)
        return extra

    def _escribir_abierta(self):
        protocolo.escribir_abierta(self.carpeta, "pausada" if self.pausado else "grabando", self._extra())
        self.abierta = True

    def _latido(self, error=""):
        """Hasta dónde llegó la grabación y a qué hora. Si el motor muere, el próximo sabe desde cuándo
        no se grabó."""
        d = {"epoch": time.time(), "hora": ahora_iso(), "frame": self.escritor.frame_global,
             "desfase": self.desfase, "pausado": self.pausado, "pid": os.getpid(), "error": error}
        ruta = os.path.join(self.carpeta, "latido.json")
        try:
            with open(ruta + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(ruta + ".tmp", ruta)
        except OSError:
            pass
        self.ult_latido = time.time()

    # ------------------------------------------------------------------ arranque
    def ejecutar(self):
        import pyaudiowpatch as pa
        cfg = self.cfg
        self.pa = pa
        self.p = pa.PyAudio()
        nombre = cfg.get("dispositivo") or dispositivos.nombre_por_defecto()
        self.lb, self.render = dispositivos.buscar(self.p, pa, nombre)
        self.nombre_dispositivo = nombre
        self.sr = int(self.lb["defaultSampleRate"])
        self.ch = int(self.lb["maxInputChannels"])
        self.blk = self.sr // 10
        self.gb_hora = self.sr * self.ch * 4 * 3600 / 1e9
        if self.previa is not None:
            sr0, ch0 = int(self.previa.get("sample_rate") or 0), int(self.previa.get("canales") or 0)
            if (sr0, ch0) != (self.sr, self.ch):
                raise RuntimeError(f"«{nombre}» entrega ahora {self.sr} Hz y {self.ch} canales, y la sesión se "
                                   f"grabó con {sr0} Hz y {ch0}: así no se puede continuar la misma sesión.")

        self.escritor = Escritor(self.carpeta, self.sr, self.ch)
        self.medidor = MedidorBloques(self.sr, self.ch)
        self.seguidor = Seguidor(min_dur=float(cfg.get("min_dur", 30)), refinar_s=3.0,
                                 umbral_salto=float(cfg.get("umbral_salto", 3.0)))
        if self.previa is not None:
            self._preparar_continuacion()
        else:
            self.picos = GuardaPicos(self.carpeta, self.sr, float(cfg.get("min_dur", 30)))
        self.f_bloques = open(os.path.join(self.carpeta, "mediciones.f32"), "ab")
        ruta_reloj = os.path.join(self.carpeta, "reloj.csv")
        reloj_nuevo = not os.path.exists(ruta_reloj)
        self.f_reloj = open(ruta_reloj, "a", newline="", encoding="utf-8")
        self.w_reloj = csv.writer(self.f_reloj)
        if reloj_nuevo:
            self.w_reloj.writerow(["hora_pc", "epoch_s", "frame_global", "segundos_audio", "desfase_s"])

        self.sesion = {
            "app": f"Loudness Lab {VERSION}",
            "etiqueta": self.etiqueta, "notas": cfg.get("notas", ""),
            "config": {k: v for k, v in cfg.items() if not k.startswith("_") and k != "ultima_sesion"},
            "condiciones": {k: cfg.get(k, "") for k in ("fuente", "playlist", "normalizacion", "crossfade", "calidad")},
            "inicio_pc": ahora_iso(), "dispositivo": nombre, "sample_rate": self.sr, "canales": self.ch,
            "formato": "WAV float32", "segundos_por_archivo": SEG_POR_ARCHIVO, "filtro_app": cfg.get("app", ""),
            "umbrales": {k: cfg.get(k) for k in ("umbral_salto", "alerta_silencio_min", "min_dur")},
            "picos": {"carpeta": "picos", "indice": "picos.csv", "segundos_antes": SEG_ANTES,
                      "segundos_despues": SEG_DESPUES,
                      "nota": "un recorte por cada cambio de canción con |salto| sobre umbral_salto"},
            "checklist": cfg.get("checklist", {}), "prueba_cadena": cfg.get("prueba_cadena"),
            "titulos": ({"fuente": "stream de la radio (metadatos ICY), ubicados por alineación con el audio",
                         "stream_url": cfg.get("stream_url"), "ajuste_titulo_s": float(cfg.get("ajuste_titulo_s") or 0.0),
                         "registro": "radio.csv"}
                        if (cfg.get("stream_url") or "").strip() else
                        {"fuente": "panel multimedia de Windows", "app": cfg.get("app", "")}),
            "equipo": {"windows": platform.platform(), "nombre": platform.node()},
            "mediciones": {"archivo": "mediciones.f32", "tipo": "float32", "bloque_ms": 100,
                           "columnas": [f"z_canal{i}" for i in range(self.ch)] + ["sample_peak", "true_peak"],
                           "nota": "z = energía K-ponderada (media cuadrática) del canal; picos lineales"},
        }
        if self.previa is not None:   # lo que ya decía la sesión se conserva (inicio, condiciones, chequeo…)
            self.sesion = {**self.sesion, **{k: v for k, v in self.previa.items() if k in self.sesion},
                           "app": self.sesion["app"], "t0_epoch": self.t0}

        clave = secrets.token_hex(16)
        self.listener = Listener(("127.0.0.1", 0), authkey=bytes.fromhex(clave))
        self.publicador.escuchar(self.listener)
        protocolo.escribir_activo({"pid": os.getpid(), "puerto": self.listener.address[1], "clave": clave,
                                   "carpeta": self.carpeta, "etiqueta": self.etiqueta, "inicio": ahora_iso()})

        self._abrir_audio()
        self.monitor = Monitor(self)
        if cfg.get("monitor"):
            self.monitor.abrir(cfg.get("monitor_dispositivo", ""), cfg.get("monitor_vol", 0.5))
        if self.previa is not None:
            self._escribir_corte()

        pistas = os.path.join(self.carpeta, "pistas.csv")
        url = (cfg.get("stream_url") or "").strip()
        if cfg.get("_simular_avisos"):
            self.meta = HiloSimulado(pistas, self.escritor.posicion, cfg["_simular_avisos"], self.cola_eventos.put)
        elif url:
            # una radio web que no publica el título en Windows: se lee de su stream (ver radio.py)
            self.meta = HiloRadio(pistas, self.escritor.posicion, url, self.cola_eventos.put, self.sr,
                                  ajuste_s=float(cfg.get("ajuste_titulo_s") or 0.0))
            self.radio = self.meta
        else:
            self.meta = HiloMetadata(pistas, self.escritor.posicion, cfg.get("app", ""), self.cola_eventos.put)
        self.meta.start()
        if self.previa is not None and self.estado_deseado == "pausada":
            self._pausar()      # se cortó estando en pausa: sigue en pausa
        self._escribir_abierta()   # desde aquí, si el motor muere, el próximo retoma esta sesión

        if not sistema.impedir_suspension(True):
            log.warning("No pude impedir que el PC se suspenda: revisa la configuración de energía.")
        self.libre_gb = sistema.espacio_libre_gb(self.carpeta)
        log.info("Grabando %s (%d Hz, %d canales) en %s", nombre, self.sr, self.ch, self.carpeta)
        log.info("Espacio libre: %.0f GB (≈ %.0f h de audio). App a seguir: %s",
                 self.libre_gb, self.libre_gb / self.gb_hora, cfg.get("app") or "cualquiera")
        try:
            self._bucle()
        except BaseException as e:
            # Un error no termina la sesión: se cierra lo que se pueda y queda abierta para retomarla.
            try:
                self._cerrar(final=False, error=f"{type(e).__name__}: {e}")
            except Exception:
                log.exception("Al cerrar tras el error")
            raise
        self._cerrar(final=True)

    # ------------------------------------------------------------------ continuar
    def _preparar_continuacion(self):
        """Retoma una sesión cortada. Repara sus archivos, deja el seguimiento (canciones, saltos,
        estadísticas) como estaba con lo que hay en disco y calcula desde cuándo no se grabó. El tramo
        sin grabar se anota como «corte» en _escribir_corte, cuando el audio ya está de vuelta."""
        prev = self.previa
        archivos, total, bloques, self.reparaciones = recuperar.preparar(self.carpeta, self.sr, self.ch,
                                                                         prev.get("archivos"))
        self.escritor.continuar(archivos, total)
        self.picos = GuardaPicos(self.carpeta, self.sr, float(self.cfg.get("min_dur", 30)))
        seg, eventos, n = sesiones.repasar(self.carpeta, prev, self.cfg)
        self.seguidor = seg
        self.t0 = float(prev.get("t0_epoch") or (epoch_de(archivos[0]["hora_pc_inicio"]) if archivos else None)
                        or epoch_de(prev.get("inicio_pc")) or time.time())
        self.overflows = self.overflows_aviso = int(prev.get("overflows") or 0)
        self.reaperturas = int(prev.get("reaperturas") or 0)

        # pausas y cortes anteriores, tal como quedaron en pistas.csv (se escribe al instante)
        abierta = None
        for e in eventos:
            ev, fg = e["evento"], int(e["frame_global"])
            if ev == "pausa":
                abierta = {"frame": fg, "t_s": fg / self.sr, "hora_pausa": e["hora_pc"], "hora_reanuda": None,
                           "duracion_s": None}
            elif ev == "reanuda" and abierta is not None:
                ini, fin = epoch_de(abierta["hora_pausa"]), epoch_de(e["hora_pc"])
                abierta.update(hora_reanuda=e["hora_pc"], duracion_s=(fin - ini) if ini and fin else 0.0)
                self.pausas.append(abierta)
                abierta = None
        for c in sesiones.cortes_de(eventos, self.sr):
            self.cortes.append({"frame": int(round(c["t_s"] * self.sr)), **c})

        # hasta cuándo se grabó: el último latido, o mejor, el audio que alcanzó a quedar en disco
        lat = sesiones._json(os.path.join(self.carpeta, "latido.json")) or {}
        ultimo = lat.get("epoch")
        if not ultimo:
            fechas = [os.path.getmtime(os.path.join(self.carpeta, x)) for x in ("mediciones.f32", "registro.log")
                      if os.path.exists(os.path.join(self.carpeta, x))]
            ultimo = max(fechas) if fechas else time.time()
        pausado_total = sum(p["duracion_s"] for p in self.pausas) + self._segundos_cortes()
        if abierta is not None:
            # se cortó estando en pausa: la pausa dura hasta el corte
            muerte = max(ultimo, epoch_de(abierta["hora_pausa"]) or ultimo)
            abierta.update(hora_reanuda=iso_de(muerte), duracion_s=muerte - (epoch_de(abierta["hora_pausa"]) or muerte))
            self.pausas.append(abierta)
        else:
            estimada = self.t0 + pausado_total + total / self.sr + float(lat.get("desfase") or 0.0)
            muerte = min(max(estimada, ultimo), time.time())
        self.pausa_total = sum(p["duracion_s"] for p in self.pausas) + self._segundos_cortes()
        motivo = f"el programa falló ({lat['error']})" if lat.get("error") else sistema.motivo_corte(muerte)
        self.corte_pendiente = {"muerte": muerte, "motivo": motivo, "cerrar_pausa": abierta is not None}

        idx = np.arange(9, n, 10)   # el gráfico: un punto por segundo, como en vivo
        self.curva_t.agregar((idx + 1) / 10.0)
        self.curva_st.agregar(seg.st.v[idx])
        for item in sesiones._log(self.carpeta):
            self.log_reciente.append(item)
        self._recuperar_picos(seg.saltos_notorios, archivos)
        seg.saltos_notorios, seg.nuevas = [], []
        self.stats = seg.estadisticas()
        self.desfase = float(lat.get("desfase") or 0.0)
        # el medidor sigue con el estado que tenía, como si el audio fuera de corrido
        self.medidor, self._descartar = recuperar.medidor_calentado(self.carpeta, archivos, self.sr, self.ch, bloques)

    def _recuperar_picos(self, saltos, archivos):
        """Los recortes de saltos que se detectaron pero no alcanzaron a guardarse antes del corte se
        sacan ahora del audio en disco."""
        hechos = self.picos.guardados()
        for e in saltos:
            corte = e["corte"] * self.blk
            if round(corte / self.sr, 3) in hechos:
                continue
            f0 = max(0, corte - SEG_ANTES * self.sr)
            audio = recuperar.leer_audio(self.carpeta, archivos, f0, corte + SEG_DESPUES * self.sr)
            if len(audio):
                self.picos.alimentar(f0, audio)
                try:
                    fila = self.picos.guardar(e, self._reloj_de)
                    if fila:
                        log.info("Recorte del salto de %+.1f LU recuperado: picos\\%s", e["salto"], fila["archivo"])
                except Exception as ex:
                    log.warning("No pude recuperar el recorte del salto de %+.1f LU: %s", e["salto"], ex)
        self.picos.retrovisor.trozos.clear()

    def _escribir_corte(self):
        """Anota en pistas.csv el tramo sin grabar (corte → retoma, en el mismo frame) y se lo pasa
        al seguimiento: igual que una pausa, cierra la canción que sonaba y no hay salto a través de él."""
        c = self.corte_pendiente
        ahora = time.time()
        fg, arch, fa = self.escritor.posicion()
        filas = []
        if c["cerrar_pausa"]:
            filas.append({"evento": "reanuda", "hora_pc": iso_de(c["muerte"])})
        filas.append({"evento": "corte", "hora_pc": iso_de(c["muerte"]), "estado": c["motivo"]})
        filas.append({"evento": "retoma", "hora_pc": iso_de(ahora)})
        ruta = os.path.join(self.carpeta, "pistas.csv")
        nuevo = not os.path.exists(ruta)
        with open(ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            for fila in filas:
                fila.update(frame_global=fg, archivo=arch, frame_en_archivo=fa)
                w.writerow(fila)
                self.seguidor.evento(fila, fg // self.blk)
        dur = ahora - c["muerte"]
        self.cortes.append({"frame": fg, "t_s": fg / self.sr, "desde": iso_de(c["muerte"]), "hasta": iso_de(ahora),
                            "duracion_s": dur, "motivo": c["motivo"]})
        self.pausa_total += dur
        for r in self.reparaciones:
            log.info("Al retomar: %s.", r)
        log.warning("La grabación se cortó a las %s (%s) y se retomó a las %s: %s sin grabar. "
                    "Lo grabado antes del corte está completo.", dt.datetime.fromtimestamp(c["muerte"]).strftime("%H:%M:%S"),
                    c["motivo"], dt.datetime.fromtimestamp(ahora).strftime("%H:%M:%S"), hms_txt(dur))
        self.corte_pendiente = None

    def _abrir_audio(self):
        pa = self.pa

        def cb_entrada(in_data, frame_count, time_info, status):
            if status:
                self.overflows += 1
            self.cola_audio.put(in_data)
            return (None, pa.paContinue)

        if self.render is not None:
            ch_r = int(self.render["maxOutputChannels"])

            def cb_silencio(in_data, frame_count, time_info, status):
                return (bytes(4 * ch_r * frame_count), pa.paContinue)

            # Silencio de fondo en la misma salida: sin él, Windows deja de entregar audio en las
            # pausas y el conteo de muestras se desfasaría del reloj.
            try:
                self.stream_silencio = self.p.open(format=pa.paFloat32, channels=ch_r,
                                                   rate=int(self.render["defaultSampleRate"]), output=True,
                                                   output_device_index=self.render["index"],
                                                   stream_callback=cb_silencio)
            except Exception as e:
                log.warning("No pude abrir el silencio de fondo (%s): en las pausas el audio podría desfasarse.", e)
        self.stream = self.p.open(format=pa.paFloat32, channels=self.ch, rate=self.sr, input=True,
                                  input_device_index=self.lb["index"], frames_per_buffer=1024,
                                  stream_callback=cb_entrada)
        # Lo primero que entrega el cable es el resto de lo que sonó antes (ver dispositivos).
        self.silenciar_hasta = self.escritor.frame_global + int(dispositivos.ARRANQUE_SUCIO_S * self.sr)

    def _cerrar_audio(self):
        for s in (self.stream, self.stream_silencio):
            if s is not None:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
        self.stream = self.stream_silencio = None

    def _reabrir_audio(self):
        """La captura dejó de entregar audio (se desconectó el dispositivo, por ejemplo): se
        vuelve a abrir. Lo perdido entre medio queda a la vista en reloj.csv como desfase."""
        self.reaperturas += 1
        log.error("La captura dejó de entregar audio; reabriendo (intento %d).", self.reaperturas)
        monitor_on = self.monitor.stream is not None
        self.monitor.cerrar()
        self._cerrar_audio()
        try:
            self.p.terminate()
        except Exception:
            pass
        self.p = self.pa.PyAudio()
        try:
            lb, render = dispositivos.buscar(self.p, self.pa, self.nombre_dispositivo)
            if int(lb["defaultSampleRate"]) != self.sr or int(lb["maxInputChannels"]) != self.ch:
                raise RuntimeError("el dispositivo cambió de formato")
            self.lb, self.render = lb, render
            self._abrir_audio()
            log.info("Captura reabierta.")
            if monitor_on:
                self.monitor.abrir(self.cfg.get("monitor_dispositivo", ""), self.monitor.vol)
        except Exception as e:
            log.error("No pude reabrir la captura: %s. Reintento en unos segundos.", e)

    # ------------------------------------------------------------------ bucle
    def _bucle(self):
        cfg = self.cfg
        if self.previa is None:
            self.t0 = time.time()
            self.sesion["t0_epoch"] = self.t0
        self._latido()
        horas = float(cfg.get("horas") or 0)
        self.ult_dato = time.time()
        ult_estado = ult_seg = 0.0
        ult_min = time.time()
        self._cada_minuto(primera=True)
        while not self.detener.is_set():
            datos = self._sacar_audio(0.1)
            ahora = time.time()
            if datos:
                self.ult_dato = ahora
                self._procesar_audio(datos)
            elif ahora - self.ult_dato > SIN_DATOS_S:
                self._reabrir_audio()
                self.ult_dato = time.time()
            self._procesar_eventos()
            self._procesar_conexiones()
            self._procesar_comandos()
            self._publicar_pistas()
            self._guardar_picos()
            if ahora - ult_seg >= 1:
                self._cada_segundo()
                ult_seg = ahora
            if ahora - ult_estado >= 0.25:
                self.publicador.enviar(self._estado())
                ult_estado = ahora
            if ahora - ult_min >= 60:
                self._cada_minuto()
                ult_min = ahora
            if horas > 0 and self.escritor.frame_global / self.sr >= horas * 3600:   # horas de audio grabado
                self.motivo_fin = "se cumplió la duración configurada"
                break

    def _sacar_audio(self, timeout):
        try:
            datos = [self.cola_audio.get(timeout=timeout)]
        except queue.Empty:
            return []
        while True:
            try:
                datos.append(self.cola_audio.get_nowait())
            except queue.Empty:
                return datos

    def _procesar_audio(self, datos):
        bloque = np.frombuffer(b"".join(datos), dtype=np.float32).reshape(-1, self.ch)
        if self.pausado:   # el audio de una pausa no entra al experimento; la escucha sigue
            self.monitor.alimentar(bloque)
            return
        inicio = self.escritor.frame_global
        if inicio < self.silenciar_hasta:
            bloque = bloque.copy()
            bloque[:self.silenciar_hasta - inicio] = 0.0
        self.escritor.escribir(bloque)
        self.picos.alimentar(inicio, bloque)
        if self.radio is not None:
            self.radio.alimentar(inicio, bloque)
        self.monitor.alimentar(bloque)
        self._medir(*self.medidor.procesar(bloque))

    def _medir(self, zc, sp, tp):
        if self._descartar and len(sp):   # el medidor calentado entrega primero bloques ya medidos
            k = min(self._descartar, len(sp))
            zc, sp, tp = zc[k:], sp[k:], tp[k:]
            self._descartar -= k
        if not len(sp):
            return
        self.f_bloques.write(np.column_stack([zc, sp, tp]).astype(np.float32).tobytes())
        n0 = self.seguidor.n
        self.seguidor.agregar(zc.sum(axis=1), sp, tp)
        st = self.seguidor.st.v
        for i in range(n0, self.seguidor.n):
            if (i + 1) % 10 == 0:  # un punto por segundo para el gráfico
                t, v = (i + 1) / 10, float(st[i])
                self.curva_t.agregar([t])
                self.curva_st.agregar([v])
                self.curva_pend.append((t, v))

    def _procesar_eventos(self):
        while True:
            try:
                e = self.cola_eventos.get_nowait()
            except queue.Empty:
                break
            b = int(e.get("frame_global") or 0) // self.blk
            self.seguidor.evento(e, b)
            ev = e.get("evento")
            if ev in ("pausa", "reanuda"):
                continue
            if ev == "sin_sesion":
                self.estado_app = ""
                if self.sin_meta_desde is None:
                    self.sin_meta_desde = time.time()
                app = self.cfg.get("app") or "ninguna app"
                log.warning("No hay reproducción activa de %s en el panel multimedia de Windows.", app)
            else:
                self.sin_meta_desde = None
                self.estado_app = e.get("estado") or ""
                if ev == "cambio_pista":
                    log.info("Suena: %s – %s", e.get("artista") or "?", e.get("titulo") or "?")
                else:
                    log.info("La app pasó a: %s", self.estado_app)

    def _publicar_pistas(self):
        for p in self.seguidor.nuevas:
            self.publicador.enviar({"tipo": "pista", "pista": p})
            if not p["corta"]:
                txt = f"Terminó: {p['artista']} – {p['titulo']} · {p['lufs']:.1f} LUFS"
                if not np.isnan(p["salto"]):
                    txt += f" · salto {p['salto']:+.1f} LU"
                log.info(txt)
        self.seguidor.nuevas = []

    def _guardar_picos(self):
        """Un fallo al guardar un recorte no debe afectar a la grabación: solo se avisa."""
        for e in self.seguidor.saltos_notorios:
            try:
                fila = self.picos.guardar(e, self._reloj_de)
            except Exception as ex:
                log.warning("No pude guardar el recorte del salto de %+.1f LU: %s", e["salto"], ex)
                continue
            if fila:
                log.info("Salto de %+.1f LU: recorte guardado en picos\\%s", e["salto"], fila["archivo"])
        self.seguidor.saltos_notorios = []

    def _procesar_conexiones(self):
        while True:
            try:
                conn = self.publicador.nuevas.get_nowait()
            except queue.Empty:
                return
            self.publicador.registrar(conn, self._snapshot())

    def _procesar_comandos(self):
        while True:
            try:
                c = self.publicador.comandos.get_nowait()
            except queue.Empty:
                return
            cmd = c.get("cmd")
            if cmd == "detener":
                self.motivo_fin = "detenida desde la ventana"
                log.info("Deteniendo a pedido de la ventana...")
                self.detener.set()
            elif cmd == "pausar":
                self._pausar()
            elif cmd == "reanudar":
                self._reanudar()
            elif cmd == "monitor":
                if c.get("on"):
                    if self.monitor.stream is not None and c.get("dispositivo", "") == self.cfg.get("monitor_dispositivo", ""):
                        self.monitor.vol = float(c.get("vol", self.monitor.vol))
                    else:
                        self.cfg["monitor_dispositivo"] = c.get("dispositivo", "")
                        self.monitor.abrir(self.cfg["monitor_dispositivo"], float(c.get("vol", 0.5)))
                else:
                    self.monitor.cerrar()
                self._escribir_abierta()

    def _pausar(self):
        if self.pausado:
            return
        pos = self.escritor.posicion()   # el audio no avanza desde acá: es el punto exacto de la pausa
        self.pausado, self.pausa_desde = True, time.time()
        self.pausas.append({"frame": pos[0], "t_s": pos[0] / self.sr, "hora_pausa": ahora_iso(),
                            "hora_reanuda": None, "duracion_s": None})
        self.meta.marcar("pausa", pos)
        self.publicador.enviar({"tipo": "pausas", "pausas": self._pausas_gui()})
        if self.abierta:
            self._escribir_abierta()
            self._latido()
        log.info("Grabación en pausa: el audio no se registra hasta que reanudes.")

    def _reanudar(self):
        if not self.pausado:
            return
        dur = time.time() - self.pausa_desde
        self.pausa_total += dur
        self.pausado = False
        self.pausas[-1].update(hora_reanuda=ahora_iso(), duracion_s=dur)
        self.sin_meta_desde = None   # la metadata vuelve a leerse desde cero
        self.meta.marcar("reanuda", self.escritor.posicion())
        self.publicador.enviar({"tipo": "pausas", "pausas": self._pausas_gui()})
        self._escribir_abierta()
        self._latido()
        log.info("Grabación reanudada tras %s de pausa.", f"{int(dur // 60)}:{int(dur % 60):02d}")

    # ------------------------------------------------------------------ periódicas
    def _alerta(self, codigo, activa, texto):
        if activa:
            if codigo not in self.alertas:
                log.warning(texto)
            self.alertas[codigo] = texto
        elif codigo in self.alertas:
            del self.alertas[codigo]
            log.info("Se resolvió: %s", codigo)

    def _cada_segundo(self):
        # a disco cada segundo: si el motor muere, se pierde a lo más eso (y se vuelve a medir del audio)
        self.f_bloques.flush()
        self.f_reloj.flush()
        if time.time() - self.ult_latido >= LATIDO_S:
            self._latido()
        falla = self.cfg.get("_fallar_tras_s")   # (solo pruebas) un error a mitad de la grabación
        if falla and self.escritor.frame_global / self.sr >= float(falla):
            raise RuntimeError("falla simulada")
        if self.curva_pend:
            self.publicador.enviar({"tipo": "curva", "puntos": self.curva_pend})
            self.curva_pend = []
        self.stats = self.seguidor.estadisticas()
        s = self.escritor.frame_global / self.sr
        self.desfase = (time.time() - self.t0 - self._segundos_pausados()) - s
        if self.pausado:   # en pausa no hay audio que vigilar
            for codigo in ("silencio", "ruta", "metadata"):
                self._alerta(codigo, False, "")
            return
        sil = self.seguidor.silencio_bloques / 10
        lim = float(self.cfg.get("alerta_silencio_min") or 2) * 60
        self._alerta("silencio", s > 5 and sil >= lim, f"Sin música hace {sil / 60:.0f} min.")
        self._alerta("ruta", self.estado_app == "reproduciendo" and sil >= 10,
                     f"La app dice que está reproduciendo, pero no llega audio a {self.nombre_dispositivo}. "
                     "Revisa que su salida sea ese dispositivo (Sistema > Sonido > Mezclador de volumen).")
        if self.radio is not None:
            aviso = self.radio.aviso
            self._alerta("radio", bool(aviso), aviso)
        app = self.cfg.get("app")
        self._alerta("metadata", self.radio is None and bool(app) and self.sin_meta_desde is not None
                     and time.time() - self.sin_meta_desde > 30,
                     f"No encuentro '{app}' en el panel multimedia de Windows: no se están registrando las canciones.")

    def _cada_minuto(self, primera=False):
        fg = self.escritor.frame_global
        s = fg / self.sr
        self.w_reloj.writerow([ahora_iso(), f"{time.time():.3f}", fg, f"{s:.3f}", f"{self.desfase:.3f}"])
        self.f_reloj.flush()
        self.f_bloques.flush()
        self.escritor.flush()
        self._guardar_sesion()
        self.libre_gb = sistema.espacio_libre_gb(self.carpeta)
        self._alerta("disco", self.libre_gb < DISCO_AVISO_GB,
                     f"Quedan {self.libre_gb:.0f} GB libres (≈ {self.libre_gb / self.gb_hora:.0f} h de grabación).")
        nuevas = self.overflows - self.overflows_aviso
        self._alerta("perdidas", nuevas > 0, f"Se perdió audio en el último minuto ({nuevas} veces): "
                                             "cierra programas pesados.")
        self.overflows_aviso = self.overflows
        if self.libre_gb < DISCO_MINIMO_GB:
            self.motivo_fin = "poco espacio en disco"
            log.error("Queda muy poco espacio en disco: se detiene la grabación.")
            self.detener.set()
        if not primera:
            log.info("%.2f h grabadas · desfase %+.2f s · pérdidas %d%s", s / 3600, self.desfase, self.overflows,
                     " · en pausa" if self.pausado else "")

    # ------------------------------------------------------------------ estado
    def _estado(self):
        seg = self.seguidor
        return {
            "tipo": "estado", "grabando": not self.terminado,
            "segundos": self.escritor.frame_global / self.sr, "inicio_epoch": self.t0,
            "etiqueta": self.etiqueta, "carpeta": self.carpeta,
            "pausado": self.pausado, "pausa_s": self._en_pausa_s(), "pausado_total_s": self._segundos_pausados(),
            "momentary": float("nan") if self.pausado else seg.momentary(),
            "shortterm": float("nan") if self.pausado else seg.shortterm(),
            "pista": seg.pista_actual(), "estado_app": self.estado_app, "sesion": self.stats,
            "salud": {
                "libre_gb": self.libre_gb, "horas_restantes": self.libre_gb / self.gb_hora,
                "desfase_s": self.desfase, "perdidas": self.overflows, "reaperturas": self.reaperturas,
                "silencio_s": seg.silencio_bloques / 10, "dispositivo": self.nombre_dispositivo,
                "app": self.cfg.get("app", ""), "metadata_ok": self.sin_meta_desde is None,
                "monitor": self.monitor.stream is not None, "monitor_vol": self.monitor.vol,
                "monitor_dispositivo": self.monitor.nombre,
            },
            "alertas": list(self.alertas.values()),
        }

    def _snapshot(self):
        return {"tipo": "snapshot", "version": VERSION, "config": self.cfg, "carpeta": self.carpeta,
                "inicio_epoch": self.t0, "log": list(self.log_reciente),
                "pistas": list(self.seguidor.cerradas[-500:]), "pausas": self._pausas_gui(),
                "curva_t": self.curva_t.v.copy(), "curva_st": self.curva_st.v.copy(),
                "estado": self._estado()}

    def _guardar_sesion(self, fin=False):
        s = dict(self.sesion, archivos=self.escritor.archivos, frames_totales=self.escritor.frame_global,
                 segundos_totales=self.escritor.frame_global / self.sr, overflows=self.overflows,
                 reaperturas=self.reaperturas, resumen_en_vivo=self.stats, pausas=self.pausas,
                 segundos_en_pausa=self._segundos_pausados() - self._segundos_cortes(),
                 cortes=self.cortes, segundos_en_cortes=self._segundos_cortes())
        if fin:
            s["fin_pc"] = ahora_iso()
            s["motivo_fin"] = self.motivo_fin
        ruta = os.path.join(self.carpeta, "sesion.json")
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False, default=float)
        os.replace(tmp, ruta)

    # ------------------------------------------------------------------ cierre
    def _cerrar(self, final=True, error=""):
        """final: la sesión termina (Detener, duración cumplida, disco lleno). Si no (un error), se
        cierra todo limpio pero la sesión queda abierta, y el próximo motor la retoma."""
        self._cerrar_audio()
        while True:  # lo que quedó en cola también se guarda
            datos = self._sacar_audio(0)
            if not datos:
                break
            self._procesar_audio(datos)
        if final and self.pausado:   # se detuvo estando en pausa: la pausa queda con la duración que llevaba
            self.pausas[-1]["duracion_s"] = self._en_pausa_s()
            self.pausa_total += self._en_pausa_s()
            self.pausado = False
        self._medir(*self.medidor.terminar())
        self._procesar_eventos()
        if final:
            self.seguidor.terminar()
            self._publicar_pistas()
            self._guardar_picos()
        self.meta.detener.set()
        self.meta.join(timeout=3)
        self.monitor.cerrar()
        self.escritor.cerrar()
        self.f_reloj.close()
        self.f_bloques.close()
        self.stats = self.seguidor.estadisticas()
        sistema.impedir_suspension(False)
        self.terminado = True
        if not final:
            self._guardar_sesion()
            self._latido(error=error)
            log.error("El motor se detuvo por un error (%s). La sesión sigue abierta: se retomará sola.", error)
            self.publicador.cerrar()     # sin «fin»: la ventana lo toma como un corte y la retoma
            try:
                self.listener.close()
            except Exception:
                pass
            return
        self.motivo_fin = self.motivo_fin or "fin"
        self._guardar_sesion(fin=True)
        protocolo.borrar_abierta(self.carpeta)
        st = self.stats
        log.info("Grabación terminada (%s): %.2f h, %d canciones medidas, %d cambios, %d sobre %.0f LU.",
                 self.motivo_fin, self.escritor.frame_global / self.sr / 3600, st["canciones"],
                 st["transiciones"], st["sobre_umbral"], st["umbral"])
        self.publicador.enviar({"tipo": "fin", "motivo": self.motivo_fin, "estado": self._estado()})
        time.sleep(0.5)
        self.publicador.cerrar()
        try:
            self.listener.close()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


def _configurar_log(motor=None):
    raiz = logging.getLogger("loudness")
    raiz.setLevel(logging.INFO)
    if not raiz.handlers:
        h = logging.FileHandler(sistema.carpeta_app() / "motor.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        raiz.addHandler(h)
    if motor is not None:
        corto = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
        h = logging.FileHandler(os.path.join(motor.carpeta, "registro.log"), encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        raiz.addHandler(h)
        v = HandlerVentana(motor)
        v.setFormatter(corto)
        raiz.addHandler(v)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="LoudnessLab --motor")
    ap.add_argument("--motor", action="store_true")
    ap.add_argument("--config", help="una sesión nueva, con esta configuración")
    ap.add_argument("--continuar", help="retomar esta sesión abierta, que se cortó")
    args = ap.parse_args(argv)
    _configurar_log()
    if not sistema.candado_motor():
        # Ya hay un motor (quizá lo lanzaron la ventana y el guardián a la vez): no hay nada que hacer.
        log.info("Ya hay un motor corriendo; este no arranca.")
        return 0 if args.continuar else 1
    try:
        protocolo.ruta_error().unlink()
    except OSError:
        pass
    motor = None
    try:
        if args.continuar:
            ab = protocolo.leer_abierta()
            if not ab or not sesiones._misma_carpeta(ab["carpeta"], args.continuar):
                # Solo se retoma la sesión que quedó anotada como abierta: nunca una sesión vieja.
                raise RuntimeError(f"«{args.continuar}» no es la sesión abierta: no se retoma.")
            motor = Motor(dict(ab.get("extra") or {}), continuar=args.continuar, estado=ab.get("estado", "grabando"))
        else:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
            if protocolo.motor_vivo():
                raise RuntimeError("Ya hay una grabación en curso.")
            motor = Motor(cfg)
        _configurar_log(motor)
        motor.ejecutar()
        return 0
    except Exception as e:
        log.exception("El motor se detuvo por un error")
        try:
            protocolo.ruta_error().write_text(f"{e}\n\n{traceback.format_exc()}", encoding="utf-8")
        except OSError:
            pass
        if motor is not None and not args.continuar and not motor.abierta:
            # una sesión nueva que no alcanzó a empezar: la ventana muestra el error. Si la sesión ya
            # estaba abierta, no se avisa «fin»: sigue abierta y se retoma.
            motor.publicador.enviar({"tipo": "fin", "motivo": f"error: {e}"})
            time.sleep(0.5)
        return 1
    finally:
        protocolo.borrar_activo(os.getpid())
