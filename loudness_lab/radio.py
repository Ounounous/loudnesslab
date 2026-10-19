"""Qué canción suena en una radio web, leído de su propio stream (títulos ICY).

Algunas radios web (Mystify, con AzuraCast + Shoutcast) suenan en el navegador sin publicar el título
en el panel multimedia de Windows. Pero el stream trae el título adentro: cada ~16 KB de audio puede
venir un «StreamTitle='Artista - Título'», y el cambio cae en un punto exacto del stream.

El motor abre su propia conexión al stream solo para eso. Su copia no está sincronizada con la que
suena en el navegador (cada conexión tiene su propio retraso), así que cada minuto se compara un
trozo de la copia, decodificado, con el audio que se está grabando: la correlación da el desfase
exacto entre ambas (medido: 0,99 de correlación, sin deriva). Con ese desfase, cada cambio de título
se ubica en la muestra de la grabación donde ocurre, y se anota en pistas.csv como un aviso más.

radio.csv (en la sesión) registra los títulos tal como llegan, cada alineación y dónde quedó cada
cambio, para poder revisarlo o corregirlo después.
"""

import csv
import io
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import numpy as np

from .metadata import CAMPOS, ahora_iso

log = logging.getLogger("loudness.radio")

ALINEAR_CADA_S = 60.0        # se vuelve a medir el desfase cada minuto (el navegador puede re-cargar su buffer)
VENTANA_S = 25.0             # trozo del stream que se compara
CAPTURA_S = 120.0            # audio grabado que se guarda para comparar
CORRELACION_MINIMA = 0.6     # bajo esto, la comparación no se acepta (silencio, otra cosa sonando)
AGENTE = "LoudnessLab (medicion de loudness)"
CAMPOS_RADIO = ["hora_pc", "evento", "conexion", "stream_s", "frame", "desfase", "correlacion", "texto", "album",
                "frame_titulo"]



def albumes(carpeta):
    """Lo que la API dijo de cada canción en una sesión: «artista - título» → álbum (de radio.csv)."""
    out = {}
    try:
        with open(os.path.join(carpeta, "radio.csv"), encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("evento") == "album" and r.get("album"):
                    out[r["texto"].strip()] = r["album"]
    except OSError:
        pass
    return out


def separar(stream_title):
    """«Artista - Título» → (artista, título). Sin guion, todo es el título."""
    t = (stream_title or "").strip()
    if " - " in t:
        a, b = t.split(" - ", 1)
        return a.strip(), b.strip()
    return "", t


def _stream_title(meta):
    m = re.search(r"StreamTitle='(.*?)';", meta, re.S)
    return m.group(1) if m else None


def probar(url, timeout=10):
    """Para el chequeo previo: se conecta y devuelve el primer título que trae el stream."""
    req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        metaint = int(r.headers.get("icy-metaint") or 0)
        if not metaint:
            raise RuntimeError("el stream no trae títulos (no ofrece metadatos ICY)")
        for _ in range(8):
            r.read(metaint)
            n = r.read(1)[0] * 16
            if n:
                t = _stream_title(r.read(n).rstrip(b"\0").decode("utf-8", "replace"))
                if t is not None:
                    return t
    raise RuntimeError("el stream no mandó ningún título")


def api_nowplaying(url):
    """La dirección de «qué suena» de AzuraCast para este stream, si el servidor la tiene (sirve para
    completar el álbum). None si no."""
    p = urllib.parse.urlsplit(url)
    base = f"https://{p.hostname}/api"
    try:
        estaciones = json.load(urllib.request.urlopen(urllib.request.Request(
            f"{base}/stations", headers={"User-Agent": AGENTE}), timeout=10))
    except Exception:
        return None
    for s in estaciones:
        urls = [s.get("listen_url")] + [m.get("url") for m in s.get("mounts") or []]
        if any(u and u.split("?")[0] == url.split("?")[0] for u in urls):
            return f"{base}/nowplaying/{s['shortcode']}"
    return None


_BITRATES = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],      # MPEG-1 capa III
             2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}         # MPEG-2/2.5 capa III
_FRECUENCIAS = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _largo_frame(h):
    """Largo en bytes del frame MP3 (capa III) que empieza con estos 4 bytes, o None si no es uno."""
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    version, capa = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    bi, si, relleno = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if version == 1 or capa != 1 or bi in (0, 15) or si == 3:
        return None
    kbps = _BITRATES[1 if version == 3 else 2][bi]
    sr = _FRECUENCIAS[version][si]
    return (144 if version == 3 else 72) * kbps * 1000 // sr + relleno


def _decodificar(mp3):
    """MP3 (desde cualquier punto del stream) → (audio float32, sr, bytes saltados). Se empieza en un
    inicio de frame: uno cuyo largo lleva a otros dos frames válidos seguidos."""
    import soundfile as sf
    i = 0
    while True:
        i = mp3.find(bytes([0xFF]), i)
        if i < 0 or i + 4 > len(mp3):
            raise ValueError("no encuentro un frame MP3")
        n1 = _largo_frame(mp3[i:i + 4])
        n2 = n1 and _largo_frame(mp3[i + n1:i + n1 + 4])
        if n1 and n2 and _largo_frame(mp3[i + n1 + n2:i + n1 + n2 + 4]):
            break
        i += 1
    x, sr = sf.read(io.BytesIO(mp3[i:]), dtype="float32", always_2d=True)
    return x, sr, i


class Captura:
    """Los últimos minutos de lo que se grabó (mono), por frame de la grabación."""

    def __init__(self, sr, segundos=CAPTURA_S):
        self.sr = sr
        self.cap = int(sr * segundos)
        self.buf = np.zeros(self.cap, dtype=np.float32)   # circular: el frame f va en f % cap
        self.ini = 0           # primer frame guardado
        self.fin = 0           # frame siguiente al último guardado
        self.lock = threading.Lock()

    def alimentar(self, inicio, bloque):
        x = bloque.mean(axis=1) if bloque.ndim > 1 else bloque
        x = np.asarray(x, dtype=np.float32)
        with self.lock:
            if inicio != self.fin:    # (no debería pasar: los frames son correlativos)
                self.ini = inicio
            fin = inicio + len(x)
            x, inicio = x[-self.cap:], max(inicio, fin - self.cap)
            i, k = inicio % self.cap, len(x)
            a = min(k, self.cap - i)
            self.buf[i:i + a] = x[:a]
            self.buf[:k - a] = x[a:]
            self.fin = fin
            self.ini = max(self.ini, fin - self.cap)

    def tramo(self, desde):
        """(audio desde el frame `desde` hasta el final, frame de inicio)."""
        with self.lock:
            ini = max(desde, self.ini)
            n = max(0, self.fin - ini)
            i = ini % self.cap
            if i + n <= self.cap:
                return self.buf[i:i + n].copy(), ini
            return np.concatenate([self.buf[i:], self.buf[:n - (self.cap - i)]]), ini


def _energia_db(x, hop):
    n = len(x) // hop
    e = (x[:n * hop].reshape(n, hop) ** 2).mean(axis=1)
    return 10 * np.log10(e + 1e-10)


def alinear(stream_pcm, captura, captura_ini, sr):
    """Dónde aparece `stream_pcm` dentro de `captura`: (frame global de su inicio, correlación) o
    (None, correlación). Primero en grueso (energía cada 100 ms), después muestra a muestra."""
    from scipy.signal import fftconvolve
    hop = sr // 10
    es, ec = _energia_db(stream_pcm, hop), _energia_db(captura, hop)
    if len(ec) < len(es) or np.std(es) < 1.0:
        return None, 0.0        # muy poco grabado, o el trozo es casi silencio
    # correlación normalizada en todas las posiciones posibles (es entero dentro de ec)
    n = len(es)
    a = (es - es.mean()) / (es.std() + 1e-9)
    c = np.convolve(ec, np.ones(n), "valid") / n
    c2 = np.convolve(ec ** 2, np.ones(n), "valid") / n
    sd = np.sqrt(np.maximum(c2 - c ** 2, 1e-12))
    r = np.correlate(ec, a, "valid") / n / sd
    k = int(np.argmax(r))
    if r[k] < CORRELACION_MINIMA:
        return None, float(r[k])
    # fino: ±0,3 s alrededor, con la señal
    margen = int(0.3 * sr)
    j0 = max(0, k * hop - margen)
    seg = captura[j0:k * hop + len(stream_pcm) + margen]
    if len(seg) < len(stream_pcm):
        return None, float(r[k])
    cc = fftconvolve(seg, stream_pcm[::-1], mode="valid")
    j = j0 + int(np.argmax(cc))
    tramo = captura[j:j + len(stream_pcm)]
    rr = float(np.corrcoef(tramo, stream_pcm)[0, 1]) if len(tramo) == len(stream_pcm) else 0.0
    if rr < CORRELACION_MINIMA:
        return None, rr
    return captura_ini + j, rr


class LectorStream(threading.Thread):
    """Lee el stream sin parar: guarda los últimos minutos de MP3 (por byte) y avisa cada título."""

    def __init__(self, url, titulos, detener):
        super().__init__(daemon=True, name="radio-stream")
        self.url = url
        self.titulos = titulos          # cola: (conexión, segundo del stream, título)
        self.detener = detener
        self.conexion = 0
        self.bps = 24000.0              # bytes por segundo (se toma de icy-br)
        self.mp3 = bytearray()
        self.mp3_ini = 0                # byte (de esta conexión) donde empieza self.mp3
        self.total = 0
        self.lock = threading.Lock()
        self.error = ""

    def tramo(self, t0, t1):
        """MP3 entre los segundos [t0, t1) del stream de la conexión actual: (bytes, segundo de inicio)."""
        with self.lock:
            b0 = max(self.mp3_ini, int(t0 * self.bps))
            b1 = min(self.total, int(t1 * self.bps))
            if b1 <= b0:
                return b"", 0.0, self.conexion
            return bytes(self.mp3[b0 - self.mp3_ini:b1 - self.mp3_ini]), b0 / self.bps, self.conexion

    def segundos(self):
        with self.lock:
            return self.total / self.bps

    def run(self):
        espera = 2
        while not self.detener.is_set():
            try:
                req = urllib.request.Request(self.url, headers={"Icy-MetaData": "1", "User-Agent": AGENTE})
                r = urllib.request.urlopen(req, timeout=20)
                metaint = int(r.headers.get("icy-metaint") or 0)
                if not metaint:
                    raise RuntimeError("el stream no trae títulos (sin metadatos ICY)")
                br = r.headers.get("icy-br")
                with self.lock:
                    self.conexion += 1
                    self.bps = float(br.split(",")[0]) * 1000 / 8 if br else 24000.0
                    self.mp3, self.mp3_ini, self.total = bytearray(), 0, 0
                conexion = self.conexion
                if self.error:
                    log.info("Stream de la radio reconectado.")
                self.error = ""
                espera = 2
                tope = int(self.bps * CAPTURA_S * 1.5)
                while not self.detener.is_set():
                    audio = r.read(metaint)
                    if len(audio) < metaint:
                        raise ConnectionError("el stream se cortó")
                    with self.lock:
                        self.mp3 += audio
                        self.total += len(audio)
                        if len(self.mp3) > tope:
                            corte = len(self.mp3) - tope
                            del self.mp3[:corte]
                            self.mp3_ini += corte
                    n = r.read(1)[0] * 16
                    if n:
                        t = _stream_title(r.read(n).rstrip(b"\0").decode("utf-8", "replace"))
                        if t is not None:
                            self.titulos.put((conexion, self.total / self.bps, t))
                r.close()
            except Exception as e:
                if not self.error:
                    log.warning("No pude leer el stream de la radio (%s); reintento.", e)
                self.error = str(e)
                self.detener.wait(espera)
                espera = min(30, espera * 2)


class HiloRadio(threading.Thread):
    """Hace lo mismo que HiloMetadata (escribe pistas.csv y avisa al motor), pero con los títulos del
    stream de la radio, ubicados en la grabación por alineación."""

    def __init__(self, ruta_csv, obtener_posicion, url, al_evento, sr, ajuste_s=0.0, intervalo=0.1):
        """`ajuste_s`: cuánto después del título suele estar el borde real entre canciones en esta radio
        (config.STREAM_POR_FUENTE). El cambio se anota ahí y el ajuste fino del seguimiento busca el hueco
        en ±3 s, igual que con el aviso de Windows de las otras fuentes."""
        super().__init__(daemon=True, name="radio")
        self.ruta = ruta_csv
        self.ruta_radio = os.path.join(os.path.dirname(ruta_csv), "radio.csv")
        self.obtener_posicion = obtener_posicion
        self.url = url
        self.al_evento = al_evento
        self.sr = sr
        self.ajuste_s = ajuste_s
        self.intervalo = intervalo
        self.detener = threading.Event()
        self.marcas = queue.Queue()
        self.titulos = queue.Queue()
        self.captura = Captura(sr)
        self.lector = LectorStream(url, self.titulos, self.detener)
        self.desfase = None        # frame de la grabación = segundo del stream × sr + desfase
        self.desfase_conexion = 0
        self.ult_alineacion = 0.0
        self.desde_frame = 0       # no se compara audio de antes de una pausa (cambia el desfase)
        self.pendientes = deque()  # (frame, info) esperando que la grabación llegue ahí
        self.por_ubicar = deque()  # (conexión, segundo del stream, título) esperando un desfase
        self.titulo = None         # el último título visto
        self.en_pausa = False
        self.album = {}            # «artista - título» → (artista, título, álbum), de la API si hay
        self.api = None
        self.ult_api = 0.0

    # lo que llama el motor
    def marcar(self, evento, posicion):
        self.marcas.put((evento, ahora_iso(), posicion))

    def alimentar(self, inicio, bloque):
        self.captura.alimentar(inicio, bloque)

    @property
    def aviso(self):
        e = self.lector.error
        return f"No puedo leer el stream de la radio para los títulos ({e})." if e else ""

    # registro
    def _radio(self, evento, **kw):
        nuevo = not os.path.exists(self.ruta_radio)
        try:
            with open(self.ruta_radio, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CAMPOS_RADIO)
                if nuevo:
                    w.writeheader()
                w.writerow({"hora_pc": ahora_iso(), "evento": evento, **kw})
        except OSError:
            pass

    def _escribir(self, w, f, evento, info, frame=None, hora=None):
        fg, arch, fa = self.obtener_posicion()
        if frame is not None:       # un frame que ya pasó: mismo archivo, si alcanza
            fa = max(0, fa - (fg - frame))
            fg = frame
        fila = {"hora_pc": hora or ahora_iso(), "frame_global": fg, "archivo": arch, "frame_en_archivo": fa,
                "evento": evento, **info}
        w.writerow(fila)
        f.flush()
        if self.al_evento:
            self.al_evento(fila)

    def _info(self, titulo):
        artista, nombre = separar(titulo)
        album = ""
        api = self.album.get(titulo.strip())
        if api:                      # la API los tiene por separado: más confiable que partir en el guion
            artista, nombre, album = api
        return {"app": f"radio:{urllib.parse.urlsplit(self.url).hostname}", "artista": artista, "titulo": nombre,
                "album": album, "estado": "reproduciendo", "posicion_s": "", "duracion_s": ""}

    # alineación
    def _alinear(self):
        fin_s = self.lector.segundos()
        conexion = self.lector.conexion
        if fin_s < VENTANA_S + 5:
            return
        capt, capt_ini = self.captura.tramo(self.desde_frame)
        if len(capt) < self.sr * (VENTANA_S + 10):
            return
        # el stream propio suele ir adelantado: se prueba con un trozo de hace 15 s, y si no aparece,
        # con otros más antiguos
        for atras in (15, 35, 60, 5):
            mp3, t0, con = self.lector.tramo(fin_s - atras - VENTANA_S, fin_s - atras)
            if con != conexion or len(mp3) < self.lector.bps * VENTANA_S * 0.8:
                continue
            try:
                x, sr_s, salto = _decodificar(mp3)
            except Exception:
                continue
            t0 += salto / self.lector.bps
            pcm = x.mean(axis=1)
            if sr_s != self.sr:
                from scipy.signal import resample_poly
                g = np.gcd(self.sr, sr_s)
                pcm = resample_poly(pcm, self.sr // g, sr_s // g).astype(np.float32)
            frame, corr = alinear(pcm, capt, capt_ini, self.sr)
            if frame is None:
                continue
            nuevo = frame - int(round(t0 * self.sr))
            if self.desfase is not None and self.desfase_conexion == conexion and abs(nuevo - self.desfase) > self.sr * 0.05:
                log.info("Radio: el navegador cambió su retraso (%+.2f s); realineado.", (nuevo - self.desfase) / self.sr)
            elif self.desfase is None:
                log.info("Radio: títulos sincronizados con la grabación (correlación %.3f).", corr)
            self.desfase, self.desfase_conexion = nuevo, conexion
            self.ult_alineacion = time.time()
            self._radio("alineacion", conexion=conexion, stream_s=f"{t0:.3f}", frame=frame, desfase=nuevo,
                        correlacion=f"{corr:.4f}")
            return
        self.ult_alineacion = time.time() - ALINEAR_CADA_S + 15    # reintento en 15 s
        self._radio("alineacion_fallida", conexion=conexion)

    def _ubicar(self):
        """Pasa a frames los títulos que ya tienen desfase."""
        while self.por_ubicar:
            conexion, t, titulo = self.por_ubicar[0]
            if conexion != self.desfase_conexion or self.desfase is None:
                if conexion < self.lector.conexion:     # de una conexión anterior que nunca se alineó
                    self.por_ubicar.popleft()
                    self._radio("titulo_perdido", conexion=conexion, stream_s=f"{t:.3f}", texto=titulo)
                    continue
                return
            self.por_ubicar.popleft()
            frame = int(round((t + self.ajuste_s) * self.sr)) + self.desfase
            self.pendientes.append((frame, titulo))

    def _api(self, forzar=False):
        """Completa artista/título/álbum desde la API (cada 30 s, o a los 5 s si hay un título que no tiene)."""
        if time.time() - self.ult_api < (5 if forzar else 30):
            return
        self.ult_api = time.time()
        try:
            if self.api is None:
                self.api = api_nowplaying(self.url) or ""
            if self.api:
                d = json.load(urllib.request.urlopen(urllib.request.Request(self.api, headers={"User-Agent": AGENTE}),
                                                     timeout=10))
                for x in [d.get("now_playing") or {}, d.get("playing_next") or {}] + list(d.get("song_history") or []):
                    s = x.get("song") or {}
                    if s.get("title"):
                        clave = f"{s.get('artist', '')} - {s.get('title', '')}".strip()
                        if clave not in self.album:   # queda anotado: si llegó tarde, el Excel lo completa de aquí
                            self._radio("album", texto=clave, album=s.get("album") or "")
                        self.album[clave] = (s.get("artist") or "", s.get("title") or "", s.get("album") or "")
        except Exception:
            pass

    def run(self):
        self.lector.start()
        nuevo = not os.path.exists(self.ruta)
        with open(self.ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            primero = True
            while not self.detener.is_set():
                while True:   # las marcas van primero, como en HiloMetadata
                    try:
                        evento, hora, pos = self.marcas.get_nowait()
                    except queue.Empty:
                        break
                    fg, arch, fa = pos
                    fila = {"hora_pc": hora, "frame_global": fg, "archivo": arch, "frame_en_archivo": fa,
                            "evento": evento}
                    w.writerow(fila)
                    f.flush()
                    if self.al_evento:
                        self.al_evento(fila)
                    self.en_pausa = evento == "pausa"
                    self.pendientes.clear()
                    self.por_ubicar.clear()
                    if not self.en_pausa:
                        # al reanudar: la canción que suena empieza aquí (llega empezada), y el desfase
                        # hay que volver a medirlo con audio de después de la pausa
                        self.desde_frame = fg
                        self.desfase = None
                        if self.titulo:
                            self._escribir(w, f, "cambio_pista", self._info(self.titulo))
                while True:
                    try:
                        conexion, t, titulo = self.titulos.get_nowait()
                    except queue.Empty:
                        break
                    if titulo == self.titulo:
                        continue
                    self._radio("titulo", conexion=conexion, stream_s=f"{t:.3f}", texto=titulo)
                    self.titulo = titulo
                    self.ult_api = 0.0      # la API puede tardar en enterarse: se consulta ya y se reintenta
                    if self.en_pausa:
                        continue
                    if primero:
                        # el primer título: la canción ya venía sonando, empieza aquí para la grabación
                        primero = False
                        self._escribir(w, f, "cambio_pista", self._info(titulo))
                        self._radio("colocado", conexion=conexion, stream_s=f"{t:.3f}",
                                    frame=self.obtener_posicion()[0], texto="(inicio de la grabación)")
                    else:
                        self.por_ubicar.append((conexion, t, titulo))
                if not self.en_pausa and time.time() - self.ult_alineacion >= ALINEAR_CADA_S:
                    self._alinear()
                self._ubicar()
                pos = self.obtener_posicion()[0]
                while self.pendientes and self.pendientes[0][0] <= pos:
                    frame, titulo = self.pendientes.popleft()
                    self._escribir(w, f, "cambio_pista", self._info(titulo), frame=frame)
                    self._radio("colocado", frame=frame, frame_titulo=frame - int(round(self.ajuste_s * self.sr)),
                                desfase=self.desfase, texto=titulo)
                faltan = any(t.strip() not in self.album for _, t in self.pendientes) or                     any(t.strip() not in self.album for _, _, t in self.por_ubicar)
                self._api(forzar=faltan)
                self.detener.wait(self.intervalo)
