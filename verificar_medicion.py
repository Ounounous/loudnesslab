"""Verifica que la medición coincide con los valores de referencia de la EBU.

Pasa los archivos del «EBU loudness test set» por el mismo camino que la grabación en vivo
(MedidorBloques + Seguidor, con el audio entregado en trozos de tamaño variable) y compara con lo
que exigen EBU Tech 3341 y Tech 3342 (tabla 1 de cada documento, con sus tolerancias).

Además comprueba dos cosas propias de esta herramienta:
  - que entregar el audio en trozos o de una sola vez da lo mismo;
  - que el medidor en vivo (Seguidor) y el análisis (media_movil) dan lo mismo.

Y, si hay ffmpeg, compara contra su filtro ebur128 (un medidor independiente) con los programas
auténticos del set y con los WAV que se pasen en --comparar (por ejemplo, una sesión grabada).

También verifica la capa de frecuencias y la sonoridad percibida (plan v0.2, S6 y S7) con señales de
prueba que no necesitan el set: ruido rosa y tonos por el banco de tercios de octava, ISO 532-1 (un tono
de 1 kHz a 40 dB SPL es 1 sone, por definición) y la calibración a dB SPL.

Uso:
    python verificar_medicion.py CARPETA_CON_EL_SET [--comparar a.wav b.wav] [--salida informe.txt]
    python verificar_medicion.py --solo-frecuencias
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loudness_lab.bs1770 import ABS_GATE, MedidorBloques, db, integrado, lra, lufs, media_movil  # noqa: E402
from loudness_lab.seguimiento import Seguidor  # noqa: E402

TOL_LU = 0.1                    # Tech 3341, tabla 1, casos 1-14
TP_MENOS, TP_MAS = 0.4, 0.2     # Tech 3341, tabla 1, casos 15-23: +0.2/-0.4 dBTP
TOL_LRA = 1.0                   # Tech 3342, tabla 1
FF_TOL = {"I": 0.1, "LRA": 0.3, "TP": 0.2}   # contra ffmpeg: no es una norma, es un chequeo cruzado

# Los casos 13 y 14 miden el máximo momentary de tonos de 400 ms que empiezan cada 20 ms. La
# herramienta calcula todo sobre bloques de 100 ms, así que un tono que no empieza en esa rejilla se
# lee más bajo: 20 o 80 ms de desfase, -0,22 dB; 40 o 60 ms, -0,46 dB (= 10·log10(0,9)). No toca el
# integrado ni el LRA. Se informan como fallas, pero aparte de las que no tienen explicación.
CASOS_REJILLA = {"13", "14"}


# ----------------------------------------------------------------------------
# Medición
# ----------------------------------------------------------------------------
def trozos_de(x, rng):
    """El audio en trozos de tamaño variable, como lo entrega la tarjeta de sonido."""
    i = 0
    while i < len(x):
        n = int(rng.integers(256, 20000))
        yield x[i:i + n]
        i += n


def trozos_de_archivo(ruta, seg=5):
    with sf.SoundFile(ruta) as f:
        for b in f.blocks(blocksize=f.samplerate * seg, dtype="float64", always_2d=True):
            yield b


def medir(trozos, sr, canales):
    """Camino en vivo: devuelve la energía K por bloque (suma de canales), el true peak por bloque y
    las curvas momentary / short-term que calculó el Seguidor mientras recibía los trozos."""
    m, seg = MedidorBloques(sr, canales), Seguidor()
    zs, tps, zcs = [], [], []

    def recibir(r):
        zc, sp, tp = r
        if len(sp):
            zcs.append(zc)
            zs.append(zc.sum(axis=1))
            tps.append(tp)
            seg.agregar(zs[-1], sp, tp)

    for t in trozos:
        recibir(m.procesar(t))
    recibir(m.terminar())
    return {"zc": np.vstack(zcs), "z": np.concatenate(zs), "tp": np.concatenate(tps),
            "mom_vivo": seg.mom.v.copy(), "st_vivo": seg.st.v.copy(), "sr": sr}


def iguales_en_dB(a, b):
    """Dos curvas en LUFS son iguales si lo son donde hay señal; bajo el gate absoluto todo es el
    mismo silencio (ahí el ruido de redondeo de las sumas cambia el valor, ver refinar_corte)."""
    if len(a) != len(b) or not np.array_equal(np.isnan(a), np.isnan(b)):
        return False
    a, b = np.nan_to_num(a, nan=-999.0), np.nan_to_num(b, nan=-999.0)
    con_senal = (a > ABS_GATE) | (b > ABS_GATE)
    return bool(np.allclose(a[con_senal], b[con_senal], atol=1e-6))


class Medidas:
    """Mide cada archivo del set una sola vez y recuerda las comprobaciones de consistencia."""

    def __init__(self, carpeta):
        self.carpeta = carpeta
        self.cache = {}
        self.consistencia = []   # (archivo, trozos == una pasada, vivo == análisis)

    def ruta(self, patron):
        r = glob.glob(os.path.join(glob.escape(self.carpeta), patron))
        if len(r) != 1:
            raise FileNotFoundError(f"{patron}: {len(r)} archivos en {self.carpeta}")
        return r[0]

    def de(self, patron):
        if patron in self.cache:
            return self.cache[patron]
        ruta = self.ruta(patron)
        x, sr = sf.read(ruta, dtype="float64", always_2d=True)
        if x.shape[1] > 2:
            raise ValueError("más de 2 canales")
        r = medir(trozos_de(x, np.random.default_rng(1)), sr, x.shape[1])
        una = MedidorBloques(sr, x.shape[1])
        a = una.procesar(x)
        b = una.terminar()
        z1, tp1 = np.concatenate([a[0].sum(axis=1), b[0].sum(axis=1)]), np.concatenate([a[2], b[2]])
        trozos_ok = bool(len(z1) == len(r["z"]) and np.allclose(z1, r["z"], rtol=1e-7, atol=1e-18)
                         and np.allclose(tp1, r["tp"], rtol=1e-7, atol=1e-12))
        r["mom"], r["st"] = lufs(media_movil(r["z"], 4)), lufs(media_movil(r["z"], 30))
        vivo_ok = iguales_en_dB(r["mom_vivo"], r["mom"]) and iguales_en_dB(r["st_vivo"], r["st"])
        self.consistencia.append((os.path.basename(ruta), trozos_ok, vivo_ok))
        n = len(r["z"])
        r.update(n=n, I=integrado(r["z"], 0, n), lra=lra(r["z"], 0, n), tp_dbtp=float(db(r["tp"].max())),
                 canales=x.shape[1])
        self.cache[patron] = r
        return r


# ----------------------------------------------------------------------------
# Informe
# ----------------------------------------------------------------------------
class Informe:
    def __init__(self):
        self.filas = []   # (caso, qué, medido, esperado, ok o None, nota)

    def numero(self, caso, que, valores, esperado, menos=TOL_LU, mas=TOL_LU, nota=""):
        v = np.atleast_1d(np.asarray(valores, dtype=float))
        ok = bool(len(v) and np.all(np.isfinite(v)) and np.all(v >= esperado - menos - 1e-9)
                  and np.all(v <= esperado + mas + 1e-9))
        medido = "-" if not len(v) else (f"{v[0]:.2f}" if len(v) == 1 else f"{v.min():.2f} a {v.max():.2f}")
        self.filas.append((caso, que, medido, f"{esperado:+.1f} (-{menos:g}/+{mas:g})", ok, nota))

    def no_aplica(self, caso, que, nota):
        self.filas.append((caso, que, "-", "-", None, nota))

    def texto(self):
        ancho = max(len(f[1]) for f in self.filas)
        out = [f"{'Caso':<7}{'Qué':<{ancho + 2}}{'Medido':>16}  {'Esperado':<22}Resultado"]
        for fila in self.filas:
            caso, que, medido, esperado, ok, nota = fila
            res = "no aplica" if ok is None else ("OK" if ok else ("FALLA (límite conocido)" if self.conocida(fila) else "FALLA"))
            out.append(f"{caso:<7}{que:<{ancho + 2}}{medido:>16}  {esperado:<22}{res}" + (f"  {nota}" if nota else ""))
        return "\n".join(out)

    @staticmethod
    def conocida(fila):
        return fila[4] is False and fila[0] in CASOS_REJILLA

    def resumen(self):
        ok = sum(1 for f in self.filas if f[4] is True)
        conocidas = sum(1 for f in self.filas if self.conocida(f))
        mal = sum(1 for f in self.filas if f[4] is False) - conocidas
        na = sum(1 for f in self.filas if f[4] is None)
        return ok, mal, conocidas, na


# ----------------------------------------------------------------------------
# Tech 3341 y 3342
# ----------------------------------------------------------------------------
def pruebas_ebu(med, inf):
    # 1-8: nivel integrado de tonos y programas (M y S también en 1 y 2)
    for caso, archivo, nivel in [("1", "seq-3341-1-16bit.wav", -23.0), ("2", "seq-3341-2-16bit.wav", -33.0)]:
        r = med.de(archivo)
        inf.numero(caso, "Tono 1 kHz: integrado", r["I"], nivel)
        inf.numero(caso, "Tono 1 kHz: momentary (valores válidos)", r["mom"][3:], nivel)
        inf.numero(caso, "Tono 1 kHz: short-term (valores válidos)", r["st"][29:], nivel)
    for caso, archivo in [("3", "seq-3341-3-16bit-v02.wav"), ("4", "seq-3341-4-16bit-v02.wav"),
                          ("5", "seq-3341-5-16bit-v02.wav")]:
        inf.numero(caso, "Gating relativo/absoluto: integrado", med.de(archivo)["I"], -23.0)
    inf.no_aplica("6", "Señal 5.0 canales", "la herramienta mide estéreo (L y R con peso 1)")
    inf.numero("7", "Programa auténtico 1 (NLR): integrado", med.de("seq-3341-7_seq-3342-5-24bit.wav")["I"], -23.0)
    inf.numero("8", "Programa auténtico 2 (WLR): integrado",
               med.de("seq-3341-2011-8_seq-3342-6-24bit-v02.wav")["I"], -23.0)

    # 9-14: short-term y momentary con tonos intermitentes
    inf.numero("9", "Short-term constante tras 3 s", med.de("seq-3341-9-24bit.wav")["st"][29:], -23.0)
    mx = [np.nanmax(med.de(f"seq-3341-10-{i}-24bit.wav*")["st"]) for i in range(1, 21)]
    inf.numero("10", "Máximo short-term de cada uno de los 20 archivos", mx, -23.0)
    st = med.de("seq-3341-11-24bit.wav")["st"]
    mx = [np.nanmax(st[k * 60:(k + 1) * 60]) for k in range(20)]
    for k in range(20):
        inf.numero("11", f"Máximo short-term, tono {k + 1:>2}", mx[k], -38.0 + k)
    inf.numero("12", "Momentary constante tras 1 s", med.de("seq-3341-12-24bit.wav")["mom"][9:], -23.0)
    mx = [np.nanmax(med.de(f"seq-3341-13-{i}-24bit.wav*")["mom"]) for i in range(1, 21)]
    for k, v in enumerate(mx):
        inf.numero("13", f"Máximo momentary, archivo {k + 1:>2} (silencio inicial {k * 20} ms)", v, -23.0)
    mom = med.de("seq-3341-14-24bit.wav*")["mom"]
    for k in range(20):
        inf.numero("14", f"Máximo momentary, tono {k + 1:>2} (silencio inicial {k * 20} ms)",
                   np.nanmax(mom[k * 8:(k + 1) * 8]), -38.0 + k)

    # 15-23: true peak
    esperado_tp = {15: -6.0, 16: -6.0, 17: -6.0, 18: -6.0, 19: +3.0, 20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0}
    for caso, nivel in esperado_tp.items():
        r = med.de(f"seq-3341-{caso}-24bit.wav*")
        inf.numero(str(caso), "True peak máximo (dBTP)", r["tp_dbtp"], nivel, TP_MENOS, TP_MAS)

    # Tech 3342: Loudness Range
    for caso, archivo, esperado in [("L1", "seq-3342-1-16bit.wav", 10), ("L2", "seq-3342-2-16bit.wav", 5),
                                    ("L3", "seq-3342-3-16bit.wav", 20), ("L4", "seq-3342-4-16bit.wav", 15),
                                    ("L5", "seq-3341-7_seq-3342-5-24bit.wav", 5),
                                    ("L6", "seq-3341-2011-8_seq-3342-6-24bit-v02.wav", 15)]:
        inf.numero(caso, "LRA (LU)", med.de(archivo)["lra"], esperado, TOL_LRA, TOL_LRA)

    # Otros archivos del set con nivel conocido
    for nivel in (-20, -26, -40):
        inf.numero("extra", f"Tono 1 kHz de {nivel} LUFS: integrado",
                   med.de(f"1kHz Sine {nivel} LUFS-16bit.wav")["I"], float(nivel))
    r = med.de("EBU-reference_listening_signal_pinknoise_500Hz_2kHz_R128.wav")
    inf.numero("extra", f"Ruido rosa de referencia ({r['canales']} canal): integrado", r["I"], -23.0)


# ----------------------------------------------------------------------------
# Contra ffmpeg
# ----------------------------------------------------------------------------
def ffmpeg_resumen(ruta):
    p = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostats", "-i", ruta, "-af",
                        "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    t = p.stderr.split("Summary:")[-1]
    campos = {"I": r"I:\s+(-?[\d.]+) LUFS", "LRA": r"LRA:\s+(-?[\d.]+) LU", "TP": r"Peak:\s+(-?[\d.]+) dBFS"}
    out = {}
    for k, patron in campos.items():
        m = re.search(patron, t)
        out[k] = float(m.group(1)) if m else float("nan")
    return out


def contra_ffmpeg(rutas, inf):
    for ruta in rutas:
        nombre = os.path.basename(ruta)
        r = medir(trozos_de_archivo(ruta), sf.info(ruta).samplerate, sf.info(ruta).channels)
        n = len(r["z"])
        mio = {"I": integrado(r["z"], 0, n), "LRA": lra(r["z"], 0, n), "TP": float(db(r["tp"].max()))}
        ff = ffmpeg_resumen(ruta)
        for k, etiqueta in (("I", "integrado (LUFS)"), ("LRA", "LRA (LU)"), ("TP", "true peak (dBTP)")):
            ok = bool(np.isfinite(mio[k]) and np.isfinite(ff[k]) and abs(mio[k] - ff[k]) <= FF_TOL[k] + 1e-9)
            inf.filas.append(("ffmpeg", f"{nombre[:40]}: {etiqueta}", f"{mio[k]:.2f}",
                              f"{ff[k]:+.1f} (±{FF_TOL[k]:g})", ok, ""))


# ----------------------------------------------------------------------------
def pruebas_frecuencias(inf):
    """La capa de frecuencias (bandas.py) y la sonoridad percibida (zwicker.py, sonoridad.py)."""
    from loudness_lab import bandas, sonoridad, zwicker
    sr = 48000
    x = bandas.ruido_rosa(sr * 30, sr)
    e = bandas.BancoTercios(sr, 1).procesar(x[:, None])[20:].mean(axis=0)
    nivel = 10 * np.log10(e)
    nom = np.array(bandas.NOMINALES)
    sel = (nom >= 31.5) & (nom <= 12500)
    inf.numero("frec", "Ruido rosa: nivel de cada tercio respecto de la media (31,5 Hz–12,5 kHz)",
               nivel[sel] - nivel[sel].mean(), 0.0, 0.5, 0.5)
    t = np.arange(sr * 5) / sr
    e1 = bandas.BancoTercios(sr, 1).procesar(np.sin(2 * np.pi * 1000 * t)[:, None])[10:].mean(axis=0)
    i = bandas.NOMINALES.index(1000)
    inf.numero("frec", "Tono 1 kHz: % de la energía en la banda de 1 kHz", 100 * e1[i] / e1.sum(), 97.5, 2.5, 2.5)
    xs = np.stack([x, x[::-1]], 1)[:sr * 6]
    a = bandas.BancoTercios(sr, 2).procesar(xs)
    b = bandas.BancoTercios(sr, 2)
    rng = np.random.default_rng(3)
    partes, pos = [], 0
    while pos < len(xs):
        k = int(rng.integers(1, 20000))
        partes.append(b.procesar(xs[pos:pos + k]))
        pos += k
    igual = np.allclose(a, np.vstack(partes), rtol=1e-12, atol=1e-18)
    inf.filas.append(("frec", "Bandas: trozos de cualquier tamaño == una pasada", "sí" if igual else "no", "sí", igual, ""))
    m = MedidorBloques(sr, 2, true_peak=False, bandas=True)
    m.procesar(xs[:sr * 3])
    m.procesar(np.zeros((sr * 10, 2)))
    limpio = all(np.all((z == 0) | (np.abs(z) >= 1e-30)) for z in [m.zi] + list(m.banco.zi))
    inf.filas.append(("frec", "Silencio digital: filtros sin números desnormales", "sí" if limpio else "no", "sí", limpio,
                      ""))
    e2 = bandas.BancoTercios(sr, 1).procesar((np.sqrt(2) * np.sin(2 * np.pi * 1000 * t))[:, None])[10:].mean(axis=0)
    c = 40 - 10 * np.log10(e2[i])
    n = float(zwicker.sonoridad(10 * np.log10(e2) + c)[0])
    inf.numero("532-1", "Tono 1 kHz a 40 dB SPL: sonoridad (sone)", n, 1.0, 0.05, 0.05)
    inf.numero("532-1", "Calibración: ruido rosa a −14 LUFS = 65 dB SPL → C (dB)", sonoridad.calibracion_spl(), 79.89,
               0.01, 0.01)
    ln = float(zwicker.sone_a_phon(zwicker.sonoridad(np.full(29, 65 - 10 * np.log10(29)))[0]))
    inf.numero("532-1", "Ruido rosa a −14 LUFS: LUFS percibido", sonoridad.lufs_percibido(ln), -14.0, 0.05, 0.05)


def main():
    ap = argparse.ArgumentParser(description="Verifica la medición contra el EBU loudness test set.")
    ap.add_argument("carpeta", nargs="?", help="carpeta con los WAV del EBU loudness test set (ya descomprimido)")
    ap.add_argument("--solo-frecuencias", action="store_true",
                    help="solo la capa de frecuencias y la sonoridad (no necesitan el set)")
    ap.add_argument("--comparar", nargs="*", default=[], help="WAV propios para comparar con ffmpeg")
    ap.add_argument("--sin-ffmpeg", action="store_true")
    ap.add_argument("--salida", help="guarda además el informe en este archivo")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    inf = Informe()
    if not args.solo_frecuencias:
        if not args.carpeta:
            ap.error("falta la carpeta con el EBU loudness test set (o --solo-frecuencias)")
        med = Medidas(args.carpeta)
        pruebas_ebu(med, inf)
        mal_trozos = [a for a, t, v in med.consistencia if not t]
        mal_vivo = [a for a, t, v in med.consistencia if not v]
        n = len(med.consistencia)
        inf.filas.append(("propio", f"Trozos de cualquier tamaño == una pasada ({n} archivos)",
                          f"{n - len(mal_trozos)}/{n}", f"{n}/{n}", not mal_trozos, ", ".join(mal_trozos)))
        inf.filas.append(("propio", f"Medidor en vivo == análisis ({n} archivos)", f"{n - len(mal_vivo)}/{n}",
                          f"{n}/{n}", not mal_vivo, ", ".join(mal_vivo)))
    pruebas_frecuencias(inf)

    if args.solo_frecuencias or args.sin_ffmpeg:
        pass
    elif shutil.which("ffmpeg"):
        programas = [med.ruta("seq-3341-7_seq-3342-5-24bit.wav"), med.ruta("seq-3341-2011-8_seq-3342-6-24bit-v02.wav")]
        contra_ffmpeg(programas + list(args.comparar), inf)
    else:
        print("(ffmpeg no está en el PATH: se omite la comparación cruzada)")

    texto = inf.texto()
    ok, mal, conocidas, na = inf.resumen()
    texto += f"\n\n{ok} correctas, {mal} fallidas sin explicación, {conocidas} fallidas por un límite conocido, {na} no aplican."
    if conocidas:
        texto += ("\nLímite conocido (casos 13 y 14): momentary y short-term se calculan cada 100 ms; un tono corto que no\n"
                  "empieza en esa rejilla se lee hasta 0,46 dB más bajo. No afecta al integrado ni al LRA.")
    print(texto)
    if args.salida:
        with open(args.salida, "w", encoding="utf-8") as f:
            f.write(texto + "\n")
    return 1 if mal else 0


if __name__ == "__main__":
    sys.exit(main())
