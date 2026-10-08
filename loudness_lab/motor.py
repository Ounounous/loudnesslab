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

from . import VERSION, dispositivos, protocolo, sistema
from .bs1770 import MedidorBloques
from .metadata import HiloMetadata, HiloSimulado
from .seguimiento import Seguidor, Serie

SEG_POR_ARCHIVO = 3600
DISCO_MINIMO_GB = 2.0   # bajo esto se detiene, para no dejar archivos a medias con el disco lleno
DISCO_AVISO_GB = 20.0
SIN_DATOS_S = 5.0       # el silencio de fondo garantiza un flujo continuo: si no llega nada, se cayó

log = logging.getLogger("loudness")


def ahora_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


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
    def __init__(self, cfg):
        self.cfg = cfg
        self.etiqueta = sistema.limpiar_nombre(cfg.get("etiqueta") or "sesion")
        base = Path(cfg.get("carpeta_salida") or sistema.carpeta_salida_por_defecto())
        self.carpeta = str(base / f"{self.etiqueta}_{dt.datetime.now():%Y%m%d_%H%M%S}")
        os.makedirs(self.carpeta, exist_ok=False)
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

        self.escritor = Escritor(self.carpeta, self.sr, self.ch)
        self.medidor = MedidorBloques(self.sr, self.ch)
        self.seguidor = Seguidor(min_dur=float(cfg.get("min_dur", 30)), refinar_s=3.0,
                                 umbral_salto=float(cfg.get("umbral_salto", 3.0)))
        self.f_bloques = open(os.path.join(self.carpeta, "mediciones.f32"), "ab")
        self.f_reloj = open(os.path.join(self.carpeta, "reloj.csv"), "a", newline="", encoding="utf-8")
        self.w_reloj = csv.writer(self.f_reloj)
        self.w_reloj.writerow(["hora_pc", "epoch_s", "frame_global", "segundos_audio", "desfase_s"])

        self.sesion = {
            "app": f"Loudness Lab {VERSION}",
            "etiqueta": self.etiqueta, "notas": cfg.get("notas", ""),
            "condiciones": {k: cfg.get(k, "") for k in ("fuente", "playlist", "normalizacion", "crossfade", "calidad")},
            "inicio_pc": ahora_iso(), "dispositivo": nombre, "sample_rate": self.sr, "canales": self.ch,
            "formato": "WAV float32", "segundos_por_archivo": SEG_POR_ARCHIVO, "filtro_app": cfg.get("app", ""),
            "umbrales": {k: cfg.get(k) for k in ("umbral_salto", "alerta_silencio_min", "min_dur")},
            "checklist": cfg.get("checklist", {}), "prueba_cadena": cfg.get("prueba_cadena"),
            "equipo": {"windows": platform.platform(), "nombre": platform.node()},
            "mediciones": {"archivo": "mediciones.f32", "tipo": "float32", "bloque_ms": 100,
                           "columnas": [f"z_canal{i}" for i in range(self.ch)] + ["sample_peak", "true_peak"],
                           "nota": "z = energía K-ponderada (media cuadrática) del canal; picos lineales"},
        }

        clave = secrets.token_hex(16)
        self.listener = Listener(("127.0.0.1", 0), authkey=bytes.fromhex(clave))
        self.publicador.escuchar(self.listener)
        protocolo.escribir_activo({"pid": os.getpid(), "puerto": self.listener.address[1], "clave": clave,
                                   "carpeta": self.carpeta, "etiqueta": self.etiqueta, "inicio": ahora_iso()})

        self._abrir_audio()
        self.monitor = Monitor(self)
        if cfg.get("monitor"):
            self.monitor.abrir(cfg.get("monitor_dispositivo", ""), cfg.get("monitor_vol", 0.5))

        pistas = os.path.join(self.carpeta, "pistas.csv")
        if cfg.get("_simular_avisos"):
            self.meta = HiloSimulado(pistas, self.escritor.posicion, cfg["_simular_avisos"], self.cola_eventos.put)
        else:
            self.meta = HiloMetadata(pistas, self.escritor.posicion, cfg.get("app", ""), self.cola_eventos.put)
        self.meta.start()

        if not sistema.impedir_suspension(True):
            log.warning("No pude impedir que el PC se suspenda: revisa la configuración de energía.")
        self.libre_gb = sistema.espacio_libre_gb(self.carpeta)
        log.info("Grabando %s (%d Hz, %d canales) en %s", nombre, self.sr, self.ch, self.carpeta)
        log.info("Espacio libre: %.0f GB (≈ %.0f h de audio). App a seguir: %s",
                 self.libre_gb, self.libre_gb / self.gb_hora, cfg.get("app") or "cualquiera")
        try:
            self._bucle()
        finally:
            self._cerrar()

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
        self.t0 = time.time()
        horas = float(cfg.get("horas") or 0)
        fin = self.t0 + horas * 3600 if horas > 0 else None
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
            if ahora - ult_seg >= 1:
                self._cada_segundo()
                ult_seg = ahora
            if ahora - ult_estado >= 0.25:
                self.publicador.enviar(self._estado())
                ult_estado = ahora
            if ahora - ult_min >= 60:
                self._cada_minuto()
                ult_min = ahora
            if fin and ahora >= fin:
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
        inicio = self.escritor.frame_global
        if inicio < self.silenciar_hasta:
            bloque = bloque.copy()
            bloque[:self.silenciar_hasta - inicio] = 0.0
        self.escritor.escribir(bloque)
        self.monitor.alimentar(bloque)
        self._medir(*self.medidor.procesar(bloque))

    def _medir(self, zc, sp, tp):
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
            elif cmd == "monitor":
                if c.get("on"):
                    if self.monitor.stream is not None and c.get("dispositivo", "") == self.cfg.get("monitor_dispositivo", ""):
                        self.monitor.vol = float(c.get("vol", self.monitor.vol))
                    else:
                        self.cfg["monitor_dispositivo"] = c.get("dispositivo", "")
                        self.monitor.abrir(self.cfg["monitor_dispositivo"], float(c.get("vol", 0.5)))
                else:
                    self.monitor.cerrar()

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
        if self.curva_pend:
            self.publicador.enviar({"tipo": "curva", "puntos": self.curva_pend})
            self.curva_pend = []
        self.stats = self.seguidor.estadisticas()
        s = self.escritor.frame_global / self.sr
        self.desfase = (time.time() - self.t0) - s
        sil = self.seguidor.silencio_bloques / 10
        lim = float(self.cfg.get("alerta_silencio_min") or 2) * 60
        self._alerta("silencio", s > 5 and sil >= lim, f"Sin música hace {sil / 60:.0f} min.")
        self._alerta("ruta", self.estado_app == "reproduciendo" and sil >= 10,
                     f"La app dice que está reproduciendo, pero no llega audio a {self.nombre_dispositivo}. "
                     "Revisa que su salida sea ese dispositivo (Sistema > Sonido > Mezclador de volumen).")
        app = self.cfg.get("app")
        self._alerta("metadata", bool(app) and self.sin_meta_desde is not None
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
            log.info("%.2f h grabadas · desfase %+.2f s · pérdidas %d", s / 3600, self.desfase, self.overflows)

    # ------------------------------------------------------------------ estado
    def _estado(self):
        seg = self.seguidor
        return {
            "tipo": "estado", "grabando": not self.terminado,
            "segundos": self.escritor.frame_global / self.sr, "inicio_epoch": self.t0,
            "etiqueta": self.etiqueta, "carpeta": self.carpeta,
            "momentary": seg.momentary(), "shortterm": seg.shortterm(),
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
                "pistas": list(self.seguidor.cerradas[-500:]),
                "curva_t": self.curva_t.v.copy(), "curva_st": self.curva_st.v.copy(),
                "estado": self._estado()}

    def _guardar_sesion(self, fin=False):
        s = dict(self.sesion, archivos=self.escritor.archivos, frames_totales=self.escritor.frame_global,
                 segundos_totales=self.escritor.frame_global / self.sr, overflows=self.overflows,
                 reaperturas=self.reaperturas, resumen_en_vivo=self.stats)
        if fin:
            s["fin_pc"] = ahora_iso()
            s["motivo_fin"] = self.motivo_fin
        ruta = os.path.join(self.carpeta, "sesion.json")
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False, default=float)
        os.replace(tmp, ruta)

    # ------------------------------------------------------------------ cierre
    def _cerrar(self):
        self._cerrar_audio()
        while True:  # lo que quedó en cola también se guarda
            datos = self._sacar_audio(0)
            if not datos:
                break
            self._procesar_audio(datos)
        self._medir(*self.medidor.terminar())
        self._procesar_eventos()
        self.seguidor.terminar()
        self._publicar_pistas()
        self.meta.detener.set()
        self.meta.join(timeout=3)
        self.monitor.cerrar()
        self.escritor.cerrar()
        self.f_reloj.close()
        self.f_bloques.close()
        self.stats = self.seguidor.estadisticas()
        self.motivo_fin = self.motivo_fin or "fin"
        self._guardar_sesion(fin=True)
        sistema.impedir_suspension(False)
        self.terminado = True
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
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    _configurar_log()
    try:
        protocolo.ruta_error().unlink()
    except OSError:
        pass
    motor = None
    try:
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
        if motor is not None:
            motor.publicador.enviar({"tipo": "fin", "motivo": f"error: {e}"})
            time.sleep(0.5)
        return 1
    finally:
        protocolo.borrar_activo(os.getpid())
