r"""Pruebas de la capa de frecuencias con una sesión sintética (plan v0.2, S6 y S12): verificar y generar
desde el audio, reparar tras un corte (mediciones y espectro), seguir midiendo con el medidor
calentado igual que de corrido, y archivar. Uso: .venv\Scripts\python.exe tests\test_capa.py"""

import json
import os
import shutil
import sys
import tempfile

import numpy as np

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import soundfile as sf  # noqa: E402
from loudness_lab import bandas, capa, recuperar  # noqa: E402
from loudness_lab.bs1770 import MedidorBloques  # noqa: E402

fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def medir(x, sr, trozo=4096):
    m = MedidorBloques(sr, x.shape[1], bandas=True)
    partes = [m.procesar(x[i:i + trozo]) for i in range(0, len(x), trozo)] + [m.terminar()]
    return [np.concatenate([p[k] for p in partes]) for k in range(4)]


def db(x):
    return 10 * np.log10(np.maximum(x, 1e-30))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sr, blk = 48000, 4800
    rng = np.random.default_rng(5)
    # 60 s: ruido rosa con niveles que cambian (como canciones) y un silencio
    x = np.stack([bandas.ruido_rosa(sr * 60, sr, 1), bandas.ruido_rosa(sr * 60, sr, 2)], 1).astype(np.float32)
    x *= np.repeat(rng.uniform(0.3, 2.0, 6), sr * 10)[:, None].astype(np.float32)
    x[sr * 31:sr * 33] = 0
    d = tempfile.mkdtemp(prefix="capa_")
    try:
        c = os.path.join(d, "sesion_prueba")
        os.makedirs(c)
        sf.write(os.path.join(c, "audio_0001_20261024_120000.wav"), x, sr, subtype="FLOAT")
        zc, sp, tp, bb = medir(x, sr)        # lo que habría medido el motor en vivo
        np.column_stack([zc, sp, tp]).astype(np.float32).tofile(os.path.join(c, "mediciones.f32"))
        bb.astype(np.float32).tofile(os.path.join(c, "espectro.f32"))
        s = {"etiqueta": "sesion_prueba", "sample_rate": sr, "canales": 2, "fin_pc": "2026-10-24T12:01:00-03:00",
             "archivos": [{"archivo": "audio_0001_20261024_120000.wav", "frame_global_inicio": 0}],
             "frames_totales": len(x), "estudio": {"servicio": "Otro", "estado": "valida"},
             "espectro": {"origen": "en vivo"}}
        json.dump(s, open(os.path.join(c, "sesion.json"), "w", encoding="utf-8"))
        open(os.path.join(c, "pistas.csv"), "w", encoding="utf-8").write(
            "hora_pc,frame_global,archivo,frame_en_archivo,evento,app,artista,titulo,album,estado,posicion_s,duracion_s\n"
            "x,0,a,0,cambio_pista,app,A,T,,reproduciendo,,\n")

        print("1. Verificar contra el audio (con el espectro medido en vivo)")
        inf = capa.procesar(c)
        chequeo("mediciones y espectro calzan con el audio", inf["ok"] and inf["mediciones_dif_max_db"] <= 1e-4
                and inf["espectro_dif_max_db"] <= 1e-4, str({k: inf[k] for k in ("mediciones_dif_max_db", "espectro_dif_max_db")}))
        chequeo("y la sesión queda lista para archivar", capa.archivable(c))

        print("2. Generar el espectro cuando no existe")
        os.remove(os.path.join(c, "espectro.f32"))
        inf = capa.procesar(c)
        e2 = np.fromfile(os.path.join(c, "espectro.f32"), dtype=np.float32).reshape(-1, bandas.N_BANDAS)
        chequeo("generado igual al medido en vivo", inf["espectro"] == "generado desde el audio" and len(e2) == len(bb)
                and np.allclose(e2, bb.astype(np.float32), rtol=1e-6, atol=1e-20))

        print("3. Reparar tras un corte: mediciones y espectro atrasados, con una fila a medias")
        with open(os.path.join(c, "mediciones.f32"), "r+b") as f:
            f.truncate(400 * 16 + 7)
        with open(os.path.join(c, "espectro.f32"), "r+b") as f:
            f.truncate(350 * 116 + 50)
        _, total, bloques, informe = recuperar.preparar(c, sr, 2, s["archivos"])
        m2 = np.fromfile(os.path.join(c, "mediciones.f32"), dtype=np.float32).reshape(-1, 4)
        e3 = np.fromfile(os.path.join(c, "espectro.f32"), dtype=np.float32).reshape(-1, bandas.N_BANDAS)
        ok_m = len(m2) == bloques and np.abs(db(m2[:, :2].sum(1)) - db(zc.sum(1)[:bloques])).max() < 1e-3
        ok_e = len(e3) == bloques and np.nanmax(np.abs(db(e3[350:]) - db(bb[350:bloques]))[bb[350:bloques] > 1e-12]) < 0.01
        chequeo("mediciones completas y como de corrido", ok_m, f"({len(m2)} filas)")
        chequeo("espectro completo y como de corrido", ok_e, f"({len(e3)} filas) · {informe}")

        print("4. Seguir midiendo con el medidor calentado == de corrido")
        corte = 300
        m, descartar = recuperar.medidor_calentado(c, recuperar.inventario(c)[0], sr, 2, corte, bandas=True)
        resto = x[corte * blk:]
        partes = [m.procesar(resto[i:i + 3000]) for i in range(0, len(resto), 3000)] + [m.terminar()]
        b4 = np.concatenate([p[3] for p in partes])[descartar:]
        z4 = np.concatenate([p[0] for p in partes])[descartar:].sum(1)
        n = min(len(b4), len(bb) - corte)
        sel = bb[corte:corte + n] > 1e-12
        chequeo("bandas después del corte iguales a las de corrido", np.abs(db(b4[:n])[sel] - db(bb[corte:corte + n])[sel]).max() < 0.01)
        chequeo("energía (LUFS) igual a la de corrido", np.abs(db(z4[:n]) - db(zc.sum(1)[corte:corte + n])).max() < 0.01)

        print("5. Archivar")
        capa.procesar(c)
        libres = capa.archivar(c)
        s2 = json.load(open(os.path.join(c, "sesion.json"), encoding="utf-8"))
        chequeo("borra el audio y deja constancia", libres > 0 and not capa.wavs(c) and s2["audio_archivado"]["bytes"] == libres)
        chequeo("mediciones y espectro siguen ahí", os.path.exists(os.path.join(c, "mediciones.f32"))
                and os.path.exists(os.path.join(c, "espectro.f32")))
        try:
            capa.archivar(c)
            chequeo("no se puede archivar dos veces", False)
        except RuntimeError:
            chequeo("no se puede archivar dos veces", True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
