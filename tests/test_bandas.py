r"""Pruebas de la capa de frecuencias (plan v0.2, S6): ruido rosa plano, tono de 1 kHz en su banda, trozos
iguales a una pasada, el medidor con bandas igual al banco solo y a la referencia del plan, y el filtro K
contra el oído. Uso: .venv\Scripts\python.exe tests\test_bandas.py"""

import os
import sys

import numpy as np

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from loudness_lab import bandas  # noqa: E402
from loudness_lab.bs1770 import MedidorBloques  # noqa: E402

fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sr = 48000
    x = bandas.ruido_rosa(sr * 30, sr)
    e = bandas.BancoTercios(sr, 1).procesar(x[:, None])[20:].mean(axis=0)
    nivel = 10 * np.log10(e)
    nom = np.array(bandas.NOMINALES)
    sel = (nom >= 31.5) & (nom <= 12500)
    desv = np.abs(nivel[sel] - nivel[sel].mean()).max()
    chequeo("ruido rosa: ±0,5 dB por banda entre 31,5 Hz y 12,5 kHz", desv <= 0.5, f"(desvío máx {desv:.2f} dB)")

    t = np.arange(sr * 5) / sr
    e = bandas.BancoTercios(sr, 1).procesar(np.sin(2 * np.pi * 1000 * t)[:, None])[10:].mean(axis=0)
    i = bandas.NOMINALES.index(1000)
    chequeo("tono de 1 kHz: ≥ 95 % de la energía en su banda", e[i] / e.sum() >= 0.95, f"({100 * e[i] / e.sum():.1f} %)")

    xs = np.stack([x, x[::-1]], 1)[:sr * 6]
    a = bandas.BancoTercios(sr, 2).procesar(xs)
    b = bandas.BancoTercios(sr, 2)
    rng = np.random.default_rng(3)
    partes, pos = [], 0
    while pos < len(xs):
        k = int(rng.integers(1, 20000))
        partes.append(b.procesar(xs[pos:pos + k]))
        pos += k
    c = np.vstack(partes)
    chequeo("en trozos de cualquier tamaño == una pasada", np.allclose(a, c, rtol=1e-12, atol=1e-18),
            f"(dif máx {np.abs(a - c).max():.1e})")

    m = MedidorBloques(sr, 2, bandas=True)
    filas = [m.procesar(xs[i:i + 7777]) for i in range(0, len(xs), 7777)] + [m.terminar()]
    zc = np.vstack([f[0] for f in filas])
    bb = np.vstack([f[3] for f in filas])
    m0 = MedidorBloques(sr, 2)
    filas0 = [m0.procesar(xs[i:i + 7777]) for i in range(0, len(xs), 7777)] + [m0.terminar()]
    z0 = np.vstack([f[0] for f in filas0])
    chequeo("el medidor con bandas entrega lo mismo que sin bandas, más las bandas", np.array_equal(zc, z0)
            and len(bb) == len(zc) == len(xs) // (sr // 10), f"({len(bb)} bloques)")
    chequeo("las bandas del medidor == el banco solo", np.allclose(bb, a, rtol=1e-12, atol=1e-18))
    ref = os.path.join(RAIZ, "docs", "plan_v0.2", "referencia")
    if os.path.exists(os.path.join(ref, "bandas.py")):
        sys.path.insert(0, ref)
        import importlib
        referencia = importlib.import_module("bandas")
        r = referencia.BancoTercios(sr, 2).procesar(xs)
        chequeo("== el banco de la referencia del plan", np.allclose(bb, r, rtol=1e-12, atol=1e-18))
        sys.path.remove(ref)

    k = bandas.ganancia_k(sr)
    k = k - k[i]
    o = bandas.sensibilidad_oido(60)
    j50, j8k = bandas.NOMINALES.index(50), bandas.NOMINALES.index(8000)
    chequeo("a 60 phon LUFS cuenta de más los graves (50 Hz: ~+25 dB) y los agudos (8 kHz: ~+15 dB)",
            abs((k[j50] - o[j50]) - 25.3) < 0.2 and abs((k[j8k] - o[j8k]) - 14.8) < 0.2,
            f"({k[j50] - o[j50]:+.1f} y {k[j8k] - o[j8k]:+.1f} dB)")
    chequeo("desajuste del ruido rosa = 0", abs(bandas.desajuste_lufs_oido(np.ones(bandas.N_BANDAS))) < 1e-9)
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
