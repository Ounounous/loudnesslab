r"""Validación de la sonoridad percibida (plan v0.2, S7) contra ISO 532-1 variable en el tiempo.

Lo que usa la app: Zwicker estacionario, un valor por segundo, desde la energía media de cada tercio
de octava en ese segundo, y N5 de la canción sobre esos segundos. La referencia: loudness_zwtv de
MoSQITo 1.2.1 (ISO 532-1 método variable en el tiempo, con su ponderación temporal, un valor cada 2 ms)
sobre el mismo audio. Se compara N5 (y su LN) en 30 s del medio de cada canción.

Necesita el audio (WAV) de las sesiones y MoSQITo a mano (no es dependencia de la app):
  set PYTHONPATH=<carpeta con mosqito y pyuff>
  .venv\Scripts\python.exe tests\validar_zwicker.py "Q:\LoudnessLab\grabaciones\<sesión>" … [--por-sesion 7]
Solo lee los WAV. Escribe la tabla en docs\plan_v0.2\validacion_zwicker.csv.

Las dos rutas reciben la misma señal: la suma de los canales en una sola, (L + R)/√2 (en música, casi
siempre muy correlacionada, conserva la energía de L y R), en Pa según la calibración de la app
(−14 LUFS = 65 dB SPL).
"""

import argparse
import csv
import os
import sys
import time

import numpy as np

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import soundfile as sf  # noqa: E402
from loudness_lab import capa, sesiones, sonoridad, zwicker  # noqa: E402
from loudness_lab.bandas import BancoTercios  # noqa: E402

SEGUNDOS = 30
PREVIO = 3          # segundos antes para que los filtros (y la ponderación temporal) arranquen
P0 = 2e-5


def leer_tramo(carpeta, s, f0, n):
    """n frames desde el frame global f0, cruzando de un WAV al siguiente si hace falta."""
    partes, pos = [], 0
    for ruta in capa.wavs(carpeta, s):
        fr = sf.info(ruta).frames
        a, b = max(f0, pos), min(f0 + n, pos + fr)
        if a < b:
            x, _ = sf.read(ruta, start=a - pos, stop=b - pos, dtype="float64", always_2d=True)
            partes.append(x)
        pos += fr
    return np.concatenate(partes) if partes else np.zeros((0, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sesiones", nargs="+")
    ap.add_argument("--por-sesion", type=int, default=7)
    ap.add_argument("--nivel", type=float, default=sonoridad.NIVEL_ESCUCHA)
    ap.add_argument("--salida", default=os.path.join(RAIZ, "docs", "plan_v0.2", "validacion_zwicker.csv"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    capa._prioridad_baja()      # (puede haber una grabación en curso)
    from mosqito.sq_metrics.loudness.loudness_zwtv.loudness_zwtv import loudness_zwtv

    c = sonoridad.c_spl(args.nivel)
    k = P0 * 10 ** (c / 20)          # escala completa → Pa: nivel de banda = 10·log10(e) + C
    filas = []
    for carpeta in args.sesiones:
        s = sesiones._json(os.path.join(carpeta, "sesion.json")) or {}
        sr = int(s.get("sample_rate") or 48000)
        snap = sesiones.reconstruir(carpeta)
        validas = [p for p in snap["pistas"] if p.get("valida") and p["duracion_s"] >= SEGUNDOS + 2 * PREVIO + 20]
        elegidas = [validas[i] for i in np.linspace(0, len(validas) - 1, min(args.por_sesion, len(validas))).astype(int)]
        nombre = os.path.basename(os.path.normpath(carpeta))
        for p in elegidas:
            medio = p["inicio_s"] + p["duracion_s"] / 2
            f0 = int((medio - SEGUNDOS / 2 - PREVIO) * sr)
            x = leer_tramo(carpeta, s, f0, (SEGUNDOS + PREVIO) * sr)
            m = (x[:, 0] + x[:, 1]) / np.sqrt(2) if x.shape[1] > 1 else x[:, 0]
            # la app: energía por tercio cada 100 ms → promedio por segundo → Zwicker estacionario
            filas_b = BancoTercios(sr, 1).procesar(m[:, None])[PREVIO * 10:]
            n_seg = sonoridad.por_segundo(filas_b, c)
            n5_app = float(np.percentile(n_seg, 95))
            # la referencia: ISO 532-1 variable en el tiempo
            t0 = time.time()
            N, _, _, t = loudness_zwtv(m * k, sr, field_type="free")
            N = np.asarray(N)[np.asarray(t) >= PREVIO]
            n5_ref = float(np.percentile(N, 95))
            fila = {"sesion": nombre, "cancion": f"{p['artista']} – {p['titulo']}", "lufs": round(p["lufs"], 2),
                    "graves_pct": round((p.get("reparto") or {}).get("graves", float("nan")), 1),
                    "n5_app": round(n5_app, 3), "n5_zwtv": round(n5_ref, 3),
                    "ln_app": round(float(zwicker.sone_a_phon(n5_app)), 2),
                    "ln_zwtv": round(float(zwicker.sone_a_phon(n5_ref)), 2),
                    "n_mediana_app": round(float(np.median(n_seg)), 3), "n_mediana_zwtv": round(float(np.median(N)), 3)}
            fila["dif_ln"] = round(fila["ln_app"] - fila["ln_zwtv"], 2)
            fila["dif_n5_pct"] = round(100 * (n5_app / n5_ref - 1), 1)
            filas.append(fila)
            print(f"  {nombre[:28]:28} {fila['cancion'][:44]:44} N5 {n5_app:6.2f} / {n5_ref:6.2f} sone · "
                  f"LN {fila['ln_app']:5.1f} / {fila['ln_zwtv']:5.1f} ({fila['dif_ln']:+.2f}) · {time.time() - t0:.0f} s",
                  flush=True)
    if not filas:
        print("Sin canciones.")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.salida)), exist_ok=True)
    with open(args.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    d = np.array([f["dif_ln"] for f in filas])
    dn = np.array([f["dif_n5_pct"] for f in filas])
    ln_app, ln_ref = np.array([f["ln_app"] for f in filas]), np.array([f["ln_zwtv"] for f in filas])
    r = float(np.corrcoef(ln_app, ln_ref)[0, 1])
    # lo que importa para el estudio: ¿ordena y dispersa las canciones igual?
    rango = lambda v: np.argsort(np.argsort(v))  # noqa: E731
    rho = float(np.corrcoef(rango(ln_app), rango(ln_ref))[0, 1])
    print(f"\n{len(filas)} canciones · LN app − zwtv: media {d.mean():+.2f} phon, desv. {d.std(ddof=1):.2f}, "
          f"de {d.min():+.2f} a {d.max():+.2f} · N5: media {dn.mean():+.1f} %")
    print(f"correlación de LN {r:.3f} · de rangos (Spearman) {rho:.3f} · desv. de LN entre canciones: "
          f"app {ln_app.std(ddof=1):.2f} / zwtv {ln_ref.std(ddof=1):.2f} phon")
    print(f"Tabla en {args.salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
