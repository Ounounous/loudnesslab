"""Capa de frecuencias: energía por tercio de octava cada 100 ms, y cómo se compara LUFS con el oído.

Portado de la referencia del plan v0.2 (docs/plan_v0.2/referencia/bandas.py).

  BancoTercios      energía por tercio de octava (IEC 61260-1, base 10, 25 Hz–16 kHz, 29 bandas) cada
                    100 ms, con estado entre llamadas. Misma rejilla que MedidorBloques (bs1770.py): el
                    bloque n de espectro.f32 es el bloque n de mediciones.f32.
  ganancia_k        cuánto pesa el filtro K de BS.1770 (el de LUFS) cada banda.
  sensibilidad_oido cuánto oye el oído cada banda a un nivel de escucha (phon), según ISO 226:2003.
  desajuste_lufs_oido  índice rápido (dB): + la canción suena más de lo que dice su LUFS, − suena menos.
                    Usa curvas de tonos puros, sin enmascaramiento: sirve para ordenar canciones, no
                    como valor absoluto. Ruido rosa = 0.
  reparto_regiones  % de la energía en graves, medios-bajos, medios y agudos.

A volumen de música de fondo (≈ 60 phon) el filtro K cuenta casi completos los graves y los agudos
extremos, que el oído oye mucho menos: eso es lo que esta capa deja medir.
"""

import numpy as np
from scipy.signal import butter, sosfilt, sosfreqz

K_BANDAS = np.arange(-16, 13)
FC = 1000.0 * 10.0 ** (K_BANDAS / 10.0)          # 25,1 Hz … 15,8 kHz
NOMINALES = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
             1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000]
BORDE = 10.0 ** (1.0 / 20.0)                      # bordes de banda: fc / BORDE … fc * BORDE
N_BANDAS = len(FC)                                # 29
PHON_ESCUCHA = 60.0                               # nivel de música de fondo para el desajuste (por defecto)

REGIONES = {
    "graves": (25, 200),         # LUFS los cuenta casi completos; el oído, a volumen bajo, casi no
    "medios_bajos": (250, 400),
    "medios": (500, 5000),       # donde LUFS y el oído coinciden mejor
    "agudos": (6300, 16000),     # LUFS suma +3,3 dB parejo; el oído pierde sensibilidad
}
NOMBRES_REGIONES = {"graves": "Graves", "medios_bajos": "Medios-bajos", "medios": "Medios", "agudos": "Agudos"}

METADATOS = {"archivo": "espectro.f32", "tipo": "float32", "bloque_ms": 100, "columnas": N_BANDAS,
             "bandas_hz_nominal": NOMINALES, "bandas_hz_exacta": [round(float(f), 3) for f in FC],
             "metodo": "tercio de octava IEC 61260-1 base 10, Butterworth orden 3 (6 polos), sosfilt con estado",
             "unidad": "media cuadrática por bloque, sumada sobre canales (escala completa al cuadrado)"}


def disenar_banco(sr, orden=3):
    """Un pasabanda Butterworth por banda (orden 3: 6 polos, 3 secciones sos), diseño clásico para
    clase 1 de IEC 61260-1."""
    return [butter(orden, [fc / BORDE, min(fc * BORDE, 0.49 * sr)], btype="bandpass", fs=sr, output="sos")
            for fc in FC]


class BancoTercios:
    """Audio continuo → energía por banda y por bloque de 100 ms, con estado entre llamadas. Por bloque,
    la media cuadrática de cada banda SUMADA sobre los canales (igual que la energía z de LUFS)."""

    def __init__(self, sr, canales, orden=3):
        self.sr = sr
        self.canales = canales
        self.blk = sr // 10
        self.sos = disenar_banco(sr, orden)
        self.zi = [np.zeros((s.shape[0], 2, canales)) for s in self.sos]
        self.resto = np.zeros((0, canales))

    def procesar(self, x):
        from .bs1770 import sin_desnormales
        x = np.asarray(x, dtype=np.float64)
        buf = np.vstack([self.resto, x]) if len(self.resto) else x
        usable = (len(buf) // self.blk) * self.blk
        self.resto = buf[usable:]
        nb = usable // self.blk
        if not nb:
            return np.zeros((0, N_BANDAS))
        xb = buf[:usable]
        out = np.empty((nb, N_BANDAS))
        for j, s in enumerate(self.sos):
            y, self.zi[j] = sosfilt(s, xb, axis=0, zi=self.zi[j])
            sin_desnormales(self.zi[j])      # (con silencio digital, si no, se pone muy lento)
            out[:, j] = (y.reshape(nb, self.blk, -1) ** 2).mean(axis=1).sum(axis=1)
        return out


# ------------------------------------------------------------------------------ pesos por banda
_F226 = np.array([20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
                  1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500])
_AF = np.array([0.532, 0.506, 0.480, 0.455, 0.432, 0.409, 0.387, 0.367, 0.349, 0.330, 0.315, 0.301,
                0.288, 0.276, 0.267, 0.259, 0.253, 0.250, 0.246, 0.244, 0.243, 0.243, 0.243, 0.242,
                0.242, 0.245, 0.254, 0.271, 0.301])
_LU = np.array([-31.6, -27.2, -23.0, -19.1, -15.9, -13.0, -10.3, -8.1, -6.2, -4.5, -3.1, -2.0, -1.1,
                -0.4, 0.0, 0.3, 0.5, 0.0, -2.7, -4.1, -1.0, 1.7, 2.5, 1.2, -2.1, -7.1, -11.2, -10.7,
                -3.1])
_TF = np.array([78.5, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4, 8.6, 6.2,
                4.4, 3.0, 2.2, 2.4, 3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5, 6.0, 12.6, 13.9, 12.3])
_cache = {}


def iso226_spl(phon):
    """Nivel (dB SPL) que suena como `phon` en cada frecuencia de la tabla ISO 226:2003 (20 Hz–12,5 kHz)."""
    af = 4.47e-3 * (10 ** (0.025 * phon) - 1.15) + (0.4 * 10 ** (((_TF + _LU) / 10) - 9)) ** _AF
    return (10 / _AF) * np.log10(af) - _LU + 94


def sensibilidad_oido(phon=PHON_ESCUCHA):
    """dB relativos a 1 kHz por banda (FC). NaN sobre 12,5 kHz: ISO 226 no lo define."""
    clave = ("oido", float(phon))
    if clave not in _cache:
        s = phon - iso226_spl(phon)
        out = np.interp(np.log10(FC), np.log10(_F226), s)
        out[FC > 13000] = np.nan
        _cache[clave] = out
    return _cache[clave]


def ganancia_k(sr=48000):
    """Ganancia del filtro K de BS.1770 (dB) en el centro de cada banda."""
    clave = ("k", int(sr))
    if clave not in _cache:
        from .bs1770 import filtro_k
        _, h = sosfreqz(filtro_k(sr), worN=FC, fs=sr)
        _cache[clave] = 20 * np.log10(np.abs(h))
    return _cache[clave]


def desajuste_lufs_oido(energia_bandas, phon=PHON_ESCUCHA, sr=48000):
    """Índice en dB: + = suena más de lo que dice su LUFS; − = suena menos. energia_bandas: (29,) o
    (n, 29), energía lineal por banda (promedio). Referencia 0 dB = ruido rosa. Solo bandas ≤ 12,5 kHz."""
    e = np.atleast_2d(np.asarray(energia_bandas, dtype=float))
    sel = FC < 13000
    wk = 10 ** (ganancia_k(sr)[sel] / 10)
    wo = 10 ** (sensibilidad_oido(phon)[sel] / 10)

    def ind(m):
        return 10 * np.log10((m * wo).sum(axis=1)) - 10 * np.log10((m * wk).sum(axis=1))

    d = ind(e[:, sel]) - ind(np.ones((1, sel.sum())))
    return d if d.size > 1 else float(d[0])


def reparto_regiones(energia_bandas):
    """% de la energía (sin ponderar) en cada región de REGIONES."""
    e = np.asarray(energia_bandas, dtype=float)
    tot = e.sum()
    nom = np.array(NOMINALES)
    if not tot > 0:
        return {k: float("nan") for k in REGIONES}
    return {k: float(100 * e[(nom >= a) & (nom <= b)].sum() / tot) for k, (a, b) in REGIONES.items()}


def espectro_cancion(filas, st, silencio=-60.0, sr=48000, phon=PHON_ESCUCHA):
    """El espectro de una canción: promedio de sus filas de bandas en los bloques con música (short-term
    sobre −60 LUFS), su reparto y su desajuste (al nivel de escucha `phon`). None si no hay bloques con
    música."""
    filas = np.asarray(filas, dtype=np.float64)
    st = np.asarray(st, dtype=float)
    if not len(filas) or len(filas) != len(st):
        return None
    m = st > silencio
    if not m.any():
        return None
    e = filas[m].mean(axis=0)
    return {"espectro": [float(v) for v in e], "reparto": reparto_regiones(e),
            "desajuste_db": float(desajuste_lufs_oido(e, phon=phon, sr=sr))}


def ruido_rosa(n, sr, semilla=1):
    """Ruido rosa (potencia ∝ 1/f) por síntesis espectral, normalizado a RMS 0,1 (para las pruebas)."""
    rng = np.random.default_rng(semilla)
    espectro = rng.normal(size=n // 2 + 1) + 1j * rng.normal(size=n // 2 + 1)
    f = np.fft.rfftfreq(n, 1 / sr)
    f[0] = f[1]
    x = np.fft.irfft(espectro / np.sqrt(f), n)
    return 0.1 * x / np.sqrt(np.mean(x ** 2))
