"""Recortes de los cambios de canción con un salto notorio.

Cuando el salto de volumen entre dos canciones pasa el umbral, se guardan aparte, en <sesión>/picos/,
los últimos 10 s de la canción que terminó y los primeros 10 s de la que empezó (los mismos tramos con
que se mide el salto), para oír el cambio sin buscarlo en una grabación de horas. picos.csv los indexa.
"""

import csv
import datetime as dt
import os
from collections import deque

import numpy as np

from .sistema import limpiar_nombre

SEG_ANTES = 10     # de la canción que terminó
SEG_DESPUES = 10   # de la que empezó
CAMPOS = ["n", "archivo", "hora_pc", "corte_s", "salto_lu", "lufs_ultimos_10s_anterior",
          "lufs_primeros_10s_siguiente", "de", "a", "corte_en_recorte_s", "corte_ajustado",
          "delta_i_lu", "salto_cuerpo_lu"]     # (estas dos, cuando termina la canción nueva: ver completar)


class Retrovisor:
    """Los últimos segundos de audio en memoria. El WAV en disco no sirve para esto: mientras se graba
    no tiene al día su final, y el recorte se decide recién cuando la canción nueva lleva su duración
    mínima."""

    def __init__(self, sr, segundos):
        self.capacidad = int(segundos * sr)
        self.trozos = deque()   # (frame global del primer frame, audio)
        self.fin = 0

    def agregar(self, inicio, x):
        self.trozos.append((inicio, x))
        self.fin = inicio + len(x)
        # se suelta el trozo más antiguo mientras lo que queda siga cubriendo la capacidad
        while len(self.trozos) > 1 and self.fin - (self.trozos[0][0] + len(self.trozos[0][1])) >= self.capacidad:
            self.trozos.popleft()

    def extraer(self, f0, f1):
        """El audio entre los frames globales [f0, f1). Si el comienzo ya no está en memoria (o es
        anterior al inicio de la grabación), entrega desde donde haya. (audio, frame de inicio) o
        (None, f0) si no hay nada."""
        partes, inicio = [], None
        for ini, x in self.trozos:
            a, b = max(f0, ini), min(f1, ini + len(x))
            if a < b:
                partes.append(x[a - ini:b - ini])
                if inicio is None:
                    inicio = a
        if not partes:
            return None, f0
        return np.concatenate(partes), inicio


class GuardaPicos:
    def __init__(self, carpeta, sr, min_dur):
        self.sr = sr
        self.blk = sr // 10
        self.carpeta = os.path.join(carpeta, "picos")
        self.indice = os.path.join(carpeta, "picos.csv")
        self.n = len(self.guardados())   # al retomar una sesión cortada, la numeración sigue
        # la canción nueva lleva min_dur s cuando se decide: desde su corte hay que poder volver 10 s
        self.retrovisor = Retrovisor(sr, max(min_dur, SEG_DESPUES) + SEG_ANTES + 30)

    def guardados(self):
        """Los recortes que ya están en picos.csv, por su posición (corte_s)."""
        try:
            with open(self.indice, encoding="utf-8", newline="") as f:
                return {round(float(r["corte_s"]), 3) for r in csv.DictReader(f) if r.get("corte_s")}
        except (OSError, ValueError, KeyError):
            return set()

    def alimentar(self, inicio, x):
        self.retrovisor.agregar(inicio, x)

    def _leer(self):
        try:
            with open(self.indice, encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                return list(r), list(r.fieldnames or [])
        except OSError:
            return [], []

    def _reescribir(self, filas):
        tmp = self.indice + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
            w.writeheader()
            w.writerows(filas)
        os.replace(tmp, self.indice)

    def completar(self, cambios):
        """Agrega ΔI y salto de cuerpo a los recortes de estos cambios (Seguidor.cambios_nuevos), si los
        hay: se conocen recién cuando la canción nueva termina, bastante después de guardar el recorte."""
        filas, _ = self._leer()
        if not filas:
            return 0
        por_corte = {round(float(r["corte_s"]), 1): r for r in filas if r.get("corte_s")}
        hechos = 0
        for t in cambios:
            r = por_corte.get(round(float(t["t_s"]), 1))
            if r is not None:
                r["delta_i_lu"] = "" if t["delta_i"] != t["delta_i"] else round(float(t["delta_i"]), 2)
                r["salto_cuerpo_lu"] = "" if t["salto_cuerpo"] != t["salto_cuerpo"] else round(float(t["salto_cuerpo"]), 2)
                hechos += 1
        if hechos:
            self._reescribir(filas)
        return hechos

    def guardar(self, e, reloj):
        """Guarda el recorte de un salto notorio (ver Seguidor._revisar_salto). `reloj(frame)` da la
        hora (epoch) de un frame del audio. Devuelve lo que escribió en picos.csv, o None si el audio
        ya no estaba en memoria."""
        import soundfile as sf
        corte = e["corte"] * self.blk
        audio, ini = self.retrovisor.extraer(corte - SEG_ANTES * self.sr, corte + SEG_DESPUES * self.sr)
        if audio is None:
            return None
        self.n += 1
        hora = dt.datetime.fromtimestamp(reloj(corte)).astimezone()
        corto = f"{self.n:04d}_{hora:%H%M%S}_{e['salto']:+.1f}LU.wav"
        largo = f"{corto[:-4]}_{limpiar_nombre(e['de'])[:30]}_a_{limpiar_nombre(e['a'])[:30]}.wav"
        os.makedirs(self.carpeta, exist_ok=True)
        for archivo in (largo, corto):
            try:
                sf.write(os.path.join(self.carpeta, archivo), audio, self.sr, subtype="FLOAT", format="WAV")
                break
            except Exception:
                # con un nombre de sesión largo, el nombre con los títulos puede pasar el límite de
                # ruta de Windows (260): se reintenta sin ellos, que quedan igual en picos.csv
                if archivo == corto:
                    raise
        fila = {"n": self.n, "archivo": archivo, "hora_pc": hora.isoformat(timespec="milliseconds"),
                "corte_s": round(corte / self.sr, 3), "salto_lu": round(e["salto"], 2),
                "lufs_ultimos_10s_anterior": round(e["ultimos_10s"], 2),
                "lufs_primeros_10s_siguiente": round(e["primeros_10s"], 2), "de": e["de"], "a": e["a"],
                "corte_en_recorte_s": round((corte - ini) / self.sr, 3), "corte_ajustado": e["corte_ajustado"]}
        nuevo = not os.path.exists(self.indice)
        if not nuevo:
            filas, campos = self._leer()
            if campos != CAMPOS:      # un picos.csv de antes de v0.2: se le agregan las columnas nuevas
                self._reescribir(filas)
        with open(self.indice, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            w.writerow(fila)
        return fila
