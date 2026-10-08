"""Medición de loudness ITU-R BS.1770-4 / EBU R128, compartida por el medidor en vivo y el análisis.

Hay una sola implementación a propósito: si el medidor en vivo y el análisis calcularan distinto,
los números que se ven durante la grabación no coincidirían con los del informe final.

Todo se apoya en bloques de 100 ms. Por cada bloque se mide la energía K-ponderada de cada canal
y los picos; con eso se reconstruye cualquier métrica (momentary = 4 bloques, short-term = 30,
integrado y LRA con su gating). Por eso es lo único que hace falta medir y guardar.
"""

import numpy as np
from scipy.signal import firwin, lfilter, sosfilt

ABS_GATE = -70.0
SILENCIO = -60.0  # bajo esto, una ventana short-term se considera silencio/pausa


# ----------------------------------------------------------------------------
# Filtro K (coeficientes exactos para cualquier sample rate)
# ----------------------------------------------------------------------------
def filtro_k(sr):
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / sr)
    Vh = 10 ** (G / 20)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    s1 = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0,
          1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / sr)
    a0 = 1 + K / Q + K * K
    s2 = [1.0, -2.0, 1.0, 1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    return np.array([s1, s2])


def lufs(z):
    z = np.asarray(z, dtype=float)
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(np.maximum(z, 1e-20))


def db(x):
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(x, 1e-12))


def media_movil(x, n):
    """Promedio de las últimas n posiciones (incluida la actual); NaN donde no hay n valores."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


# ----------------------------------------------------------------------------
# Métricas BS.1770 / EBU 3342 sobre un rango de bloques de 100 ms [a, b)
# ----------------------------------------------------------------------------
def integrado(z, a, b):
    """LUFS integrado con gating absoluto (-70) y relativo (-10 LU), bloques de 400 ms con 75% solape."""
    if b - a < 4:
        return np.nan
    m = media_movil(z[a:b], 4)[3:]
    lm = lufs(m)
    m = m[lm > ABS_GATE]
    if not len(m):
        return np.nan
    rel = lufs(m.mean()) - 10
    m = m[lufs(m) > rel]
    return float(lufs(m.mean())) if len(m) else np.nan


def lra(z, a, b):
    """Loudness Range (EBU Tech 3342): percentiles 10-95 de short-term, gating -70 abs y -20 LU rel."""
    if b - a < 30:
        return np.nan
    s = media_movil(z[a:b], 30)[29:]
    ls = lufs(s)
    sel = ls > ABS_GATE
    if not sel.any():
        return np.nan
    rel = lufs(s[sel].mean()) - 20
    v = ls[sel & (ls > rel)]
    if len(v) < 2:
        return np.nan
    return float(np.percentile(v, 95) - np.percentile(v, 10))


def refinar_corte(mom, b, ventana):
    """Busca el punto más silencioso cerca del cambio reportado (el aviso de Windows llega con retraso).
    Solo mueve el corte si hay una caída clara (>= 6 LU bajo la mediana de la zona)."""
    a0, b0 = max(0, b - ventana), min(len(mom), b + ventana)
    zona = mom[a0:b0]
    if not len(zona) or np.all(np.isnan(zona)):
        return b, False
    i = int(np.nanargmin(zona))
    if np.nanmedian(zona) - zona[i] >= 6:
        return a0 + i, True
    return b, False


# ----------------------------------------------------------------------------
# Medición en streaming
# ----------------------------------------------------------------------------
class TruePeak:
    """True peak por sobremuestreo 4x, procesando por partes sin cortes entre ellas.

    Usa el mismo filtro que scipy.signal.resample_poly(x, 4, 1) por defecto (FIR de 81
    coeficientes, ventana Kaiser beta=5), separado en sus 4 fases polifásicas con estado: el
    máximo de las 4 fases es el máximo de la señal sobremuestreada.

    Al ser causal, la salida llega `retardo` muestras tarde respecto de la de resample_poly (que
    es de fase cero): el valor que entrega en la muestra n corresponde a la muestra n - retardo.
    MedidorBloques compensa ese retardo; sin compensarlo, un pico al final de un bloque se
    contaba en el bloque siguiente.
    """

    def __init__(self, canales, factor=4):
        h = firwin(2 * 10 * factor + 1, 1.0 / factor, window=("kaiser", 5.0)) * factor
        self.retardo = ((len(h) - 1) // 2) // factor
        self.fases = [h[p::factor] for p in range(factor)]
        self.estado = [np.zeros((len(f) - 1, canales)) for f in self.fases]

    def picos(self, x):
        """Valor absoluto máximo sobremuestreado en cada muestra de entrada (vector de len(x))."""
        m = np.zeros(len(x))
        for i, f in enumerate(self.fases):
            y, self.estado[i] = lfilter(f, [1.0], x, axis=0, zi=self.estado[i])
            np.maximum(m, np.abs(y).max(axis=1), out=m)
        return m


class MedidorBloques:
    """Convierte audio continuo en mediciones por bloque de 100 ms, con estado entre llamadas.

    Por bloque entrega la energía K-ponderada de cada canal (media cuadrática), el pico de muestra
    y el true peak, ambos lineales. El audio puede llegar en trozos de cualquier tamaño: lo que no
    completa un bloque queda guardado para la llamada siguiente.

    Un bloque se entrega recién cuando su true peak está completo, lo que exige unas pocas
    muestras posteriores (el retardo del filtro); el último bloque sale al llamar a terminar().
    """

    def __init__(self, sr, canales, true_peak=True):
        self.sr = sr
        self.canales = canales
        self.blk = sr // 10
        self.sos = filtro_k(sr)
        self.zi = np.zeros((self.sos.shape[0], 2, canales))
        self.tp = TruePeak(canales) if true_peak else None
        self.resto = np.zeros((0, canales))          # muestras que aún no completan un bloque
        self.pend_z = np.zeros((0, canales))         # bloques medidos que esperan su true peak
        self.pend_sp = np.zeros(0)
        self.bloque_pend = 0                         # índice global del primer bloque pendiente
        self.tp_buf = np.zeros(0)                    # true peak por muestra, ya alineado
        self.tp_ini = 0                              # muestra global a la que corresponde tp_buf[0]
        self._saltar = self.tp.retardo if self.tp is not None else 0

    def procesar(self, x):
        """x: (n, canales). Devuelve (z_por_canal (nb, canales), sample_peak (nb,), true_peak (nb,))."""
        x = np.asarray(x)
        buf = np.vstack([self.resto, x]) if len(self.resto) else x
        usable = (len(buf) // self.blk) * self.blk
        self.resto = buf[usable:]
        nb = usable // self.blk
        if nb:
            xb = np.asarray(buf[:usable], dtype=np.float64)
            y, self.zi = sosfilt(self.sos, xb, axis=0, zi=self.zi)
            self.pend_z = np.vstack([self.pend_z, (y.reshape(nb, self.blk, -1) ** 2).mean(axis=1)])
            self.pend_sp = np.concatenate([self.pend_sp, np.abs(xb).reshape(nb, self.blk, -1).max(axis=(1, 2))])
        if self.tp is not None and len(x):
            self._alimentar_tp(np.asarray(x, dtype=np.float64))
        return self._entregar()

    def terminar(self):
        """Entrega los bloques que esperaban su true peak. Después del final se asume silencio,
        igual que resample_poly."""
        if self.tp is not None:
            self._alimentar_tp(np.zeros((self.tp.retardo, self.canales)))
        return self._entregar()

    def _alimentar_tp(self, x):
        m = self.tp.picos(x)
        if self._saltar:  # las primeras salidas corresponden a tiempos antes del inicio
            k = min(self._saltar, len(m))
            m = m[k:]
            self._saltar -= k
        self.tp_buf = np.concatenate([self.tp_buf, m])

    def _entregar(self):
        n = len(self.pend_sp)
        if self.tp is None:
            listos, tp = n, self.pend_sp.copy()
        else:
            cubiertos = (self.tp_ini + len(self.tp_buf)) // self.blk - self.bloque_pend
            listos = max(0, min(n, cubiertos))
            if listos:
                a = self.bloque_pend * self.blk - self.tp_ini
                fin = a + listos * self.blk
                tp = self.tp_buf[a:fin].reshape(listos, self.blk).max(axis=1)
                self.tp_buf = self.tp_buf[fin:]
                self.tp_ini += fin
        if not listos:
            return np.zeros((0, self.canales)), np.zeros(0), np.zeros(0)
        zc, sp = self.pend_z[:listos], self.pend_sp[:listos]
        self.pend_z, self.pend_sp = self.pend_z[listos:], self.pend_sp[listos:]
        self.bloque_pend += listos
        return zc, sp, tp
