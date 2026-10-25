r"""Pruebas de la sonoridad percibida (plan v0.2, S7): el algoritmo ISO 532-1 copiado de MoSQITo, la
calibración a dB SPL, el LUFS percibido y el cálculo por segundo en vivo == de una vez.
Uso: .venv\Scripts\python.exe tests\test_zwicker.py
Si MoSQITo 1.2.1 está a mano (PYTHONPATH), compara además la copia con el original."""

import os
import sys

import numpy as np

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from loudness_lab import bandas, sonoridad, zwicker  # noqa: E402

fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sr = 48000
    rng = np.random.default_rng(4)

    print("1. ISO 532-1 desde tercios de octava")
    t = np.arange(sr * 5) / sr
    e = bandas.BancoTercios(sr, 1).procesar((np.sqrt(2) * np.sin(2 * np.pi * 1000 * t))[:, None])[10:].mean(axis=0)
    c = 40 - 10 * np.log10(e[bandas.NOMINALES.index(1000)])        # banda de 1 kHz a 40 dB SPL
    n = zwicker.sonoridad(10 * np.log10(e) + c)[0]
    chequeo("tono de 1 kHz a 40 dB SPL ≈ 1 sone (40 phon)", abs(n - 1) < 0.05,
            f"({n:.3f} sone, {float(zwicker.sone_a_phon(n)):.1f} phon)")
    L = 30 + 50 * rng.random((5000, 28)) - np.linspace(0, 25, 28)
    a = zwicker.sonoridad(L)
    b = np.concatenate([zwicker.sonoridad(L[i:i + 700]) for i in range(0, len(L), 700)])
    uno = np.array([zwicker.sonoridad(L[i])[0] for i in range(0, 40)])
    chequeo("por lotes de cualquier tamaño == fila por fila", np.array_equal(a, b) and np.array_equal(a[:40], uno))
    chequeo("más nivel, más sonoridad", np.all(np.diff(zwicker.sonoridad(np.arange(30, 90, 5.0)[:, None]
                                                                        * np.ones(28))) > 0))
    try:
        from mosqito.sq_metrics.loudness.loudness_zwst._calc_slopes import _calc_slopes
        from mosqito.sq_metrics.loudness.loudness_zwst._main_loudness import _main_loudness
        ref = np.concatenate([np.atleast_1d(_calc_slopes(_main_loudness(L[i:i + 500].T.copy(), "free"))[0])
                              for i in range(0, len(L), 500)])
        chequeo("la copia == MoSQITo 1.2.1", np.array_equal(a, ref), f"(dif máx {np.abs(a - ref).max():.1e})")
    except ImportError:
        print("  (sin MoSQITo a mano: no se compara la copia con el original)")

    print("2. Calibración y LUFS percibido")
    medida = sonoridad.calibracion_spl()
    chequeo("C = 79,89 dB (ruido rosa a −14 LUFS = 65 dB SPL)", abs(sonoridad.c_spl() - medida) < 1e-4
            and abs(medida - 79.89) < 0.01, f"({medida:.4f})")
    chequeo("C sigue al nivel de escucha", abs(sonoridad.c_spl(75) - sonoridad.c_spl(65) - 10) < 1e-9)
    for lufs in (-30.0, -14.0, -8.0):
        spl = 65 + lufs + 14
        ln = float(zwicker.sone_a_phon(zwicker.sonoridad(np.full(29, spl - 10 * np.log10(29)))[0]))
        per = sonoridad.lufs_percibido(ln)
        chequeo(f"ruido rosa a {lufs:g} LUFS se percibe a {lufs:g} LUFS", abs(per - lufs) < 0.05, f"({per:.2f})")

    print("3. Por segundo: en vivo == de una vez; una canción")
    x = np.stack([bandas.ruido_rosa(sr * 40, sr, 7), bandas.ruido_rosa(sr * 40, sr, 8)], 1)
    x[sr * 20:] *= 0.3
    filas = bandas.BancoTercios(sr, 2).procesar(x).astype(np.float32)

    def leer(a, b):
        return filas[a:b]

    viva = sonoridad.Sonoridad(leer)
    for k in range(1, 41):
        viva.hasta(k)
    junta = sonoridad.Sonoridad(leer).hasta(40)
    chequeo("segundo a segundo == de una vez", np.array_equal(viva.n, junta.n) and len(junta.n) == 40)
    r = sonoridad.Sonoridad(leer, desde=10)
    chequeo("al retomar no calcula lo de antes", np.isnan(r.segundos(5, 10)).all()
            and np.array_equal(r.segundos(10, 20), junta.n[10:20]))
    st = np.full(len(filas), -20.0)
    st[300:] = -70.0      # los últimos 10 s, silencio: no cuentan
    c1 = junta.cancion(0, len(filas), st)
    chequeo("N5 de la canción sale de los segundos con música", c1 is not None
            and abs(c1["n5"] - np.percentile(junta.n[:30], 95)) < 1e-9, f"({c1 and round(c1['n5'], 2)} sone)")
    chequeo("una canción de menos de 10 s de música no tiene sonoridad", junta.cancion(0, 90, st) is None)
    graves = np.ones(29)
    graves[:8] *= 30
    d40, d60, d80 = (bandas.espectro_cancion(graves[None, :], [0.0], phon=ph)["desajuste_db"] for ph in (40, 60, 80))
    chequeo("índice rápido: con graves de más, cuanto más bajo el volumen, más se nota", d40 < d60 < d80 < 0,
            f"({d40:.1f} / {d60:.1f} / {d80:.1f} dB a 40 / 60 / 80 phon)")
    p = sonoridad.parametros({"sonoridad": {"nivel_escucha_db_spl": 55, "phon_indice": 50}}, {"nivel_escucha_db_spl": 70})
    q = sonoridad.parametros({}, {"nivel_escucha_db_spl": 70}, phon=40)
    chequeo("parámetros: lo pedido, después la sesión, la configuración y los de siempre",
            (p["nivel"], p["phon"]) == (55, 50) and (q["nivel"], q["phon"]) == (70, 40)
            and sonoridad.parametros({}) == {"nivel": 65.0, "campo": "libre", "phon": 60.0})
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
