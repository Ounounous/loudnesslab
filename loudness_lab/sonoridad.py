"""Sonoridad percibida (Zwicker, ISO 532-1 estacionario) por segundo y por canción (plan v0.2, S7).

Se calcula desde la capa de frecuencias (espectro.f32), así que funciona en vivo, en una sesión
reabierta y en una sesión archivada, y da lo mismo en los tres casos:

  - Por segundo de la sesión (segundo k = bloques 10k … 10k+9): la energía media de cada banda en
    ese segundo, llevada a dB SPL con la calibración, y su sonoridad N (sone) con zwicker.sonoridad.
  - Por canción, en los segundos con música (short-term sobre −60 LUFS en al menos la mitad de sus
    bloques): N5 (la sonoridad superada el 5 % del tiempo, la que ISO 532-1 sugiere para sonidos que
    cambian), la mediana de N, LN (el nivel de sonoridad del N5, en phon) y el **LUFS percibido**:
    el LUFS al que un ruido rosa sonaría igual de fuerte. Con eso la sonoridad queda en la escala de
    LUFS: una canción con LUFS percibido mayor que su LUFS suena más de lo que dice su LUFS.

Calibración: una señal de LUFS_REF LUFS suena a `nivel` dB SPL en la mesa (por defecto, −14 LUFS =
65 dB SPL, a ajustar con la medición en sala). C = nivel − LUFS_REF + DESFASE_ROSA, con el desfase
medido con ruido rosa estéreo (calibracion_spl; 79,89 dB a 65 dB SPL).
"""

import numpy as np

from . import zwicker
from .bandas import N_BANDAS, BancoTercios, ruido_rosa

LUFS_REF = -14.0
NIVEL_ESCUCHA = 65.0        # dB SPL de una señal a LUFS_REF en la mesa
DESFASE_ROSA = 0.888406     # dB (ver calibracion_spl): C = nivel − LUFS_REF + DESFASE_ROSA
CAMPO = "libre"
SILENCIO = -60.0
MIN_SEGUNDOS = 10           # una canción con menos segundos de música no tiene sonoridad
LEER_DE_A = 3000            # segundos por lectura de espectro.f32
_tablas = {}


def c_spl(nivel=NIVEL_ESCUCHA, lufs_ref=LUFS_REF):
    """Constante (dB) tal que nivel SPL de una banda = 10·log10(energía de la banda) + C."""
    return float(nivel) - float(lufs_ref) + DESFASE_ROSA


def calibracion_spl(lufs_ref=LUFS_REF, nivel=NIVEL_ESCUCHA, sr=48000, segundos=20, semilla=1):
    """C medida: ruido rosa estéreo llevado a `lufs_ref` LUFS suena a `nivel` dB SPL (port de la
    referencia del plan). Las pruebas comprueban que c_spl la reproduce."""
    from .bs1770 import MedidorBloques, integrado
    x = ruido_rosa(sr * segundos, sr, semilla)
    x = np.stack([x, x], axis=1)
    m = MedidorBloques(sr, 2, true_peak=False)
    z, _, _ = m.procesar(x)
    zt, _, _ = m.terminar()
    z = np.vstack([z, zt]).sum(axis=1)
    x = x * 10 ** ((lufs_ref - integrado(z, 0, len(z))) / 20)
    e = BancoTercios(sr, 2).procesar(x)[20:].mean(axis=0)      # sin el arranque de los filtros
    return float(nivel - 10 * np.log10(e.sum()))


def por_segundo(filas, c, campo=CAMPO):
    """Filas de espectro.f32 (10 por segundo, segundos completos) → N (sone) de cada segundo."""
    f = np.asarray(filas, dtype=np.float32)
    k = len(f) // 10
    if not k:
        return np.zeros(0)
    e = f[:k * 10].reshape(k, 10, N_BANDAS).astype(np.float64).mean(axis=1)
    return zwicker.sonoridad(10 * np.log10(np.maximum(e, 1e-30)) + c, campo)


def _tabla_rosa(nivel, campo):
    """LN del ruido rosa (misma energía en cada tercio de octava) según su LUFS, para traducir LN a
    LUFS percibido. Ruido rosa a LUFS_REF = `nivel` dB SPL (la calibración)."""
    clave = (round(float(nivel), 3), campo)
    if clave not in _tablas:
        L = np.arange(-80.0, 10.05, 0.1)
        spl = nivel + (L - LUFS_REF)
        niveles = (spl - 10 * np.log10(N_BANDAS))[:, None] * np.ones(N_BANDAS)
        _tablas[clave] = (L, zwicker.sone_a_phon(zwicker.sonoridad(niveles, campo)))
    return _tablas[clave]


def lufs_percibido(ln, nivel=NIVEL_ESCUCHA, campo=CAMPO):
    """El LUFS al que un ruido rosa tendría el mismo nivel de sonoridad `ln` (phon)."""
    L, ln_rosa = _tabla_rosa(nivel, campo)
    return float(np.interp(ln, ln_rosa, L, left=np.nan, right=np.nan))


class Sonoridad:
    """N por segundo de la sesión, calculado una vez y guardado. `leer(a, b)` da las filas [a, b) de
    espectro.f32. `desde`: el primer segundo que se calcula (al retomar una sesión en vivo, los de
    antes no se calculan: la canción que cruza el corte no cuenta)."""

    def __init__(self, leer, nivel=NIVEL_ESCUCHA, campo=CAMPO, desde=0):
        self.leer = leer
        self.nivel = float(nivel)
        self.campo = campo
        self.c = c_spl(self.nivel)
        self.desde = int(desde)
        self.n = np.zeros(0)        # segundos [desde, desde + len)

    def hasta(self, k1):
        """Calcula hasta el segundo k1 (sin incluirlo), si falta."""
        while self.desde + len(self.n) < k1:
            k0 = self.desde + len(self.n)
            k = min(k1, k0 + LEER_DE_A)
            nuevos = por_segundo(self.leer(k0 * 10, k * 10), self.c, self.campo)
            if not len(nuevos):
                break
            self.n = np.concatenate([self.n, nuevos])
        return self

    def segundos(self, k0, k1):
        """N de los segundos [k0, k1); NaN los que no hay (antes de `desde` o sin espectro)."""
        self.hasta(k1)
        out = np.full(max(0, k1 - k0), np.nan)
        a, b = max(k0, self.desde), min(k1, self.desde + len(self.n))
        if b > a:
            out[a - k0:b - k0] = self.n[a - self.desde:b - self.desde]
        return out

    def ahora(self, n_bloques):
        """N del último segundo completo."""
        k = n_bloques // 10
        v = self.segundos(k - 1, k) if k >= 1 else np.zeros(0)
        return float(v[0]) if len(v) else np.nan

    def cancion(self, a, b, st):
        """Sonoridad de la canción de los bloques [a, b): {n5, n_mediana, ln, lufs_percibido}, o None si
        tiene menos de MIN_SEGUNDOS segundos con música."""
        k0, k1 = -(-int(a) // 10), int(b) // 10
        if k1 - k0 < MIN_SEGUNDOS:
            return None
        n = self.segundos(k0, k1)
        s = np.asarray(st[k0 * 10:k1 * 10], dtype=float)
        if len(s) < (k1 - k0) * 10:
            return None
        musica = (s.reshape(-1, 10) > SILENCIO).mean(axis=1) >= 0.5
        x = n[musica & ~np.isnan(n)]
        if len(x) < MIN_SEGUNDOS:
            return None
        n5 = float(np.percentile(x, 95))
        ln = float(zwicker.sone_a_phon(n5))
        return {"n5": n5, "n_mediana": float(np.median(x)), "ln": ln,
                "lufs_percibido": lufs_percibido(ln, self.nivel, self.campo)}

    def metadatos(self):
        return {"metodo": "ISO 532-1:2017 estacionario (Zwicker), un valor por segundo desde espectro.f32; "
                          "algoritmo de MoSQITo 1.2.1",
                "campo": self.campo, "lufs_ref": LUFS_REF, "nivel_escucha_db_spl": self.nivel,
                "c_spl_db": round(self.c, 3),
                "por_cancion": "N5 (percentil 95 de N en los segundos con música), mediana de N, LN = phon del N5, "
                               "LUFS percibido = LUFS de un ruido rosa con el mismo LN"}


def de_sesion(s, cfg=None):
    """Nivel de escucha y campo con que se calcula la sonoridad de una sesión: lo que dice su sesion.json
    (`sonoridad`), o la configuración, o los valores por defecto."""
    meta = (s or {}).get("sonoridad") or {}
    nivel = meta.get("nivel_escucha_db_spl", (cfg or {}).get("nivel_escucha_db_spl", NIVEL_ESCUCHA))
    return float(nivel or NIVEL_ESCUCHA), meta.get("campo", CAMPO)
