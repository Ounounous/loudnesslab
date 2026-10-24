"""Deja los archivos de una sesión cortada listos para continuarla.

Cuando el motor muere de golpe (un reinicio, un corte de luz, una caída) los archivos quedan como
estaban en ese instante:

- el WAV que se estaba escribiendo tiene el audio completo, pero su cabecera dice «0 s» (se
  completa al cerrarlo). soundfile y ffmpeg lo leen igual; otros programas no.
- mediciones.f32 puede ir algo atrasado respecto del audio (se vacía a disco cada segundo) y su
  última fila puede estar a medias.
- un CSV o el registro pueden terminar en una línea a medias.

Aquí se repara todo eso sin perder audio: la cabecera se corrige, las mediciones que falten se
vuelven a calcular desde el audio (con el mismo medidor, partiendo un poco antes para que el filtro
llegue con el mismo estado) y las líneas a medias se descartan. Lo único que se descarta del audio
es el resto de menos de 100 ms que no completa un bloque, para que audio y mediciones sigan
alineados en la misma rejilla de 100 ms que usa analizar.py.
"""

import datetime as dt
import glob
import os
import re
import struct

import numpy as np

from .bandas import N_BANDAS
from .bs1770 import MedidorBloques

CALENTAR_BLOQUES = 20   # 2 s de audio previo para que el filtro K y el true peak lleguen con su estado


# ----------------------------------------------------------------------------
# WAV
# ----------------------------------------------------------------------------
def _chunks(f):
    """(id, posición del contenido, tamaño declarado) de cada chunk de un RIFF/WAVE."""
    f.seek(0, os.SEEK_END)
    largo = f.tell()
    f.seek(0)
    cab = f.read(12)
    if len(cab) < 12 or cab[:4] != b"RIFF" or cab[8:12] != b"WAVE":
        raise ValueError("no es un WAV")
    out, pos = [], 12
    while pos + 8 <= largo:
        f.seek(pos)
        cid, tam = struct.unpack("<4sI", f.read(8))
        out.append((cid, pos + 8, tam))
        if cid == b"data":
            break      # lo que sigue es audio: con la cabecera sin cerrar, su tamaño no sirve
        pos += 8 + tam + (tam & 1)
    return out, largo


def reparar_wav(ruta, quitar=0):
    """Corrige la cabecera de un WAV que quedó abierto y le quita `quitar` frames del final.
    Devuelve los frames que quedan. No toca un WAV que ya está bien."""
    with open(ruta, "r+b") as f:
        chunks, largo = _chunks(f)
        fmt = next((c for c in chunks if c[0] == b"fmt "), None)
        data = next((c for c in chunks if c[0] == b"data"), None)
        if fmt is None or data is None:
            raise ValueError(f"{os.path.basename(ruta)}: WAV sin formato o sin audio")
        f.seek(fmt[1] + 12)
        alineado, = struct.unpack("<H", f.read(2))   # bytes por frame
        declarado = data[2]
        if 0 < declarado <= largo - data[1]:
            disponibles = declarado // alineado        # se cerró bien: vale lo que dice
        else:
            disponibles = (largo - data[1]) // alineado   # quedó abierto: vale lo que hay
        frames = max(0, disponibles - quitar)
        bytes_audio = frames * alineado
        if bytes_audio == declarado:
            return frames      # ya estaba bien
        f.truncate(data[1] + bytes_audio)
        f.seek(4)
        f.write(struct.pack("<I", data[1] + bytes_audio - 8))
        f.seek(data[1] - 4)
        f.write(struct.pack("<I", bytes_audio))
        fact = next((c for c in chunks if c[0] == b"fact"), None)
        if fact is not None and fact[2] >= 4:
            f.seek(fact[1])
            f.write(struct.pack("<I", frames))
        return frames


def _hora_de_nombre(nombre):
    m = re.match(r"audio_\d+_(\d{8}_\d{6})\.wav$", nombre)
    if not m:
        return ""
    return dt.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").astimezone().isoformat(timespec="milliseconds")


def inventario(carpeta, archivos_previos=()):
    """Los WAV de la sesión en orden, con sus frames y dónde empieza cada uno. Se toma lo que hay en
    disco (sesion.json se guarda cada minuto y puede no conocer el último archivo)."""
    previos = {a.get("archivo"): a for a in archivos_previos or []}
    import soundfile as sf
    nombres = sorted(os.path.basename(p) for p in glob.glob(os.path.join(carpeta, "audio_*.wav")))
    out, total = [], 0
    for n in nombres:
        frames = sf.info(os.path.join(carpeta, n)).frames
        out.append({"archivo": n, "frame_global_inicio": total, "frames": frames,
                    "hora_pc_inicio": (previos.get(n) or {}).get("hora_pc_inicio") or _hora_de_nombre(n)})
        total += frames
    return out, total


def leer_audio(carpeta, archivos, f0, f1):
    """El audio de los frames globales [f0, f1), aunque cruce de un WAV a otro."""
    import soundfile as sf
    partes = []
    for a in archivos:
        ini = a["frame_global_inicio"]
        fin = ini + a["frames"]
        lo, hi = max(f0, ini), min(f1, fin)
        if lo < hi:
            with sf.SoundFile(os.path.join(carpeta, a["archivo"])) as s:
                s.seek(lo - ini)
                partes.append(s.read(hi - lo, dtype="float32", always_2d=True))
    if not partes:
        return np.zeros((0, 1), dtype=np.float32)
    return np.concatenate(partes)


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------
def cerrar_linea(ruta, descartar_media=True):
    """Si el archivo termina en una línea a medias, la descarta (CSV) o la cierra (registro): lo que
    se agregue después debe empezar en una línea nueva."""
    try:
        with open(ruta, "r+b") as f:
            f.seek(0, os.SEEK_END)
            largo = f.tell()
            if not largo:
                return
            f.seek(largo - 1)
            if f.read(1) == b"\n":
                return
            if not descartar_media:
                f.write(b"\r\n" if os.name == "nt" else b"\n")
                return
            pos = largo
            paso = 4096
            while pos > 0:
                ini = max(0, pos - paso)
                f.seek(ini)
                trozo = f.read(pos - ini)
                k = trozo.rfind(b"\n")
                if k >= 0:
                    f.truncate(ini + k + 1)
                    return
                pos = ini
            f.truncate(0)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Todo junto
# ----------------------------------------------------------------------------
def preparar(carpeta, sr, canales, archivos_previos=()):
    """Repara los archivos de la sesión y deja audio y mediciones del mismo largo: un número entero
    de bloques de 100 ms. Devuelve (archivos, frames, bloques, informe)."""
    blk = sr // 10
    informe = []
    # 1. cabeceras de los WAV
    for p in sorted(glob.glob(os.path.join(carpeta, "audio_*.wav"))):
        reparar_wav(p)
    archivos, total = inventario(carpeta, archivos_previos)
    # 2. el resto que no completa un bloque se quita del final (puede cruzar a un archivo anterior
    #    solo si el último tiene menos de 100 ms: entonces se borra)
    resto = total % blk
    while resto and archivos:
        ult = archivos[-1]
        ruta = os.path.join(carpeta, ult["archivo"])
        if ult["frames"] <= resto:
            os.remove(ruta)
            resto -= ult["frames"]
            archivos.pop()
        else:
            reparar_wav(ruta, quitar=resto)
            ult["frames"] -= resto
            resto = 0
    total = sum(a["frames"] for a in archivos)
    bloques = total // blk
    if total % blk:
        informe.append("quedó un resto de audio sin completar un bloque")
    # 3. mediciones (y espectro, si la sesión lo tiene): filas completas, ni más ni menos que los
    #    bloques de audio; las que falten se vuelven a medir desde el audio
    ruta_m = os.path.join(carpeta, "mediciones.f32")
    fila = (canales + 2) * 4
    largo = os.path.getsize(ruta_m) if os.path.exists(ruta_m) else 0
    filas = min(largo // fila, bloques)
    with open(ruta_m, "ab") as f:
        f.truncate(filas * fila)
    ruta_e = os.path.join(carpeta, "espectro.f32")
    fila_e = N_BANDAS * 4
    filas_e = None
    if os.path.exists(ruta_e):
        filas_e = min(os.path.getsize(ruta_e) // fila_e, bloques)
        with open(ruta_e, "ab") as f:
            f.truncate(filas_e * fila_e)
    desde = min(filas, filas_e if filas_e is not None else filas)
    if desde < bloques:
        z, sp, tp, bb = remedir(carpeta, archivos, sr, canales, desde, bloques, bandas=True)
        if filas < bloques:
            with open(ruta_m, "ab") as f:
                f.write(np.column_stack([z, sp, tp])[filas - desde:].astype(np.float32).tobytes())
            informe.append(f"se volvieron a medir {bloques - filas} bloques ({(bloques - filas) / 10:.1f} s) desde el audio")
        if filas_e is not None and filas_e < bloques:
            with open(ruta_e, "ab") as f:
                f.write(bb[filas_e - desde:].astype(np.float32).tobytes())
            informe.append(f"se volvieron a medir las bandas de {bloques - filas_e} bloques")
    # 4. CSV y registro sin líneas a medias
    for nombre in ("pistas.csv", "reloj.csv", "picos.csv"):
        cerrar_linea(os.path.join(carpeta, nombre))
    cerrar_linea(os.path.join(carpeta, "registro.log"), descartar_media=False)
    return archivos, total, bloques, informe


def remedir(carpeta, archivos, sr, canales, desde, hasta, bandas=False):
    """Las mediciones de los bloques [desde, hasta), calculadas desde el audio en disco: (z, sp, tp) y,
    con bandas, también las bandas. El medidor parte CALENTAR_BLOQUES antes para llegar con el estado
    que tenía en vivo."""
    blk = sr // 10
    ini = max(0, desde - CALENTAR_BLOQUES)
    audio = leer_audio(carpeta, archivos, ini * blk, hasta * blk)
    m = MedidorBloques(sr, canales, bandas=bandas)
    partes = [m.procesar(audio), m.terminar()]
    return tuple(np.concatenate([p[k] for p in partes])[desde - ini:hasta - ini] for k in range(len(partes[0])))


def medidor_calentado(carpeta, archivos, sr, canales, bloques, bandas=False):
    """Un medidor para seguir midiendo desde el bloque `bloques`, con el estado que habría tenido si
    el audio hubiera seguido de corrido (como lo ve analizar.py, que mide todos los WAV seguidos).
    Devuelve (medidor, filas que va a entregar y hay que descartar porque ya están medidas)."""
    blk = sr // 10
    ini = max(0, bloques - CALENTAR_BLOQUES)
    m = MedidorBloques(sr, canales, bandas=bandas)
    if bloques > ini:
        listos = len(m.procesar(leer_audio(carpeta, archivos, ini * blk, bloques * blk))[1])
        return m, (bloques - ini) - listos
    return m, 0
