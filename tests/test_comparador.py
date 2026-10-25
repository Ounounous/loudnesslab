r"""Prueba de regresión del comparador integrado (plan v0.2, S8 y S9): con el catálogo del informe, el
comparador de la app reproduce los números del informe del 20-oct (por sesión, por servicio con
intervalos y canciones pareadas: conteos exactos, el resto con tolerancia 0,01) y escribe su Excel.
Necesita las grabaciones y los datos del estudio (docs/plan_v0.2, fuera del repositorio).
Uso: .venv\Scripts\python.exe tests\test_comparador.py [--grabaciones Q:\LoudnessLab\grabaciones]"""

import argparse
import json
import os
import sys
import tempfile
import time
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "tests"))
from loudness_lab import comparador  # noqa: E402
from test_estudio import PLAN, comparar  # noqa: E402

fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grabaciones", default=r"Q:\LoudnessLab\grabaciones")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    ruta_cat = os.path.join(PLAN, "referencia", "catalogo_sesiones.json")
    ruta_ref = os.path.join(PLAN, "valores_referencia.json")
    if not (os.path.exists(ruta_cat) and os.path.exists(ruta_ref)):
        print("Faltan los datos del estudio (docs/plan_v0.2): esta prueba corre en el equipo del estudio.")
        return 0
    catalogo = json.load(open(ruta_cat, encoding="utf-8"))["sesiones"]
    ref = json.load(open(ruta_ref, encoding="utf-8"))
    del_informe = {r["sesion"] for r in ref["sesiones"]}
    catalogo = {k: v for k, v in catalogo.items() if k in del_informe or v.get("estado") == "no_valida"}
    carpetas = [os.path.join(args.grabaciones, n) for n in catalogo]

    print("1. El comparador integrado, con el catálogo del informe (sin la marca de corte incierto, como el informe)")
    t0 = time.time()
    r = comparador.comparar(carpetas, catalogo=catalogo, marcar_inciertos=False)
    print(f"     {len(r['sesiones'])} sesiones en {time.time() - t0:.0f} s; no entraron: {r['omitidas']}")
    difs = []
    por_nombre = {f["sesion"]: f for f in r["sesiones"]}
    for x in ref["sesiones"]:
        comparar(x, por_nombre.get(x["sesion"]), f"sesiones[{x['sesion']}]", difs)
    chequeo("por sesión == informe", not difs, f"({len(difs)} diferencias)" if difs else "")
    difs_s = []
    por_servicio = {a["servicio"]: a for a in r["servicios"]}
    for x in ref["servicios"]:
        comparar(x, por_servicio.get(x["servicio"]), f"servicios[{x['servicio']}]", difs_s)
    chequeo("por servicio, con intervalos == informe", not difs_s, f"({len(difs_s)} diferencias)" if difs_s else "")
    difs_p = []
    comparar(ref["pares"], (r["pares"].get(("Soundtrack", "Spotify")) or (None, {}))[1], "pares", difs_p)
    chequeo("canciones pareadas Soundtrack–Spotify == informe", not difs_p,
            f"({len(difs_p)} diferencias)" if difs_p else "")
    for d in (difs + difs_s + difs_p)[:10]:
        print("     ", d)
    chequeo("la sesión no válida del catálogo no entra, y dice por qué",
            any(m == "no válida para el estudio" for _, m in r["omitidas"]))

    print("2. El Excel del comparador")
    ruta = os.path.join(tempfile.mkdtemp(prefix="comparador_"), "comparacion.xlsx")
    comparador.escribir_excel(ruta, r)
    with zipfile.ZipFile(ruta) as z:
        libro = z.read("xl/workbook.xml").decode("utf-8")
    hojas = ["Sesiones", "Servicios", "Canciones", "Cambios", "Pares Soundtrack-Spotify", "Espectro", "Notas"]
    chequeo("con sus hojas", all(f'name="{h}"' in libro for h in hojas), str([h for h in hojas if f'name="{h}"' not in libro]))
    chequeo("una fila por canción y por cambio", len(r["canciones"]) > 1000 and len(r["cambios"]) > 900,
            f"({len(r['canciones'])} canciones, {len(r['cambios'])} cambios)")
    os.remove(ruta)

    print("3. Las sesiones que se pueden comparar (con sus datos del estudio)")
    cand = comparador.candidatas(args.grabaciones)
    sin = [c["nombre"] for c in cand if c["falta"].startswith("faltan")]
    chequeo("las que no tienen datos del estudio se señalan", all(c["falta"] == "" or c["falta"] for c in cand),
            f"({len(cand)} terminadas; sin datos: {sin})")
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
