"""Prueba de regresión del estudio (plan v0.2, S3, S5 y S9).

Rehace las sesiones del catálogo con el Seguidor de la app (sin audio: mediciones.f32 + pistas.csv) y
compara con los valores del informe (docs/plan_v0.2/valores_referencia.json, solo en el equipo del estudio): por sesión, por servicio
(con intervalos) y canciones pareadas. Después comprueba la alerta de cambio de estado: dos alertas en
Dinner and drinks (12:50 nivel, 12:53 transiciones) y ninguna en las otras sesiones válidas.

Solo lee las sesiones. Uso, desde la carpeta del repositorio:
    .venv\\Scripts\\python.exe tests\\test_estudio.py [--grabaciones Q:\\LoudnessLab\\grabaciones]
"""

import argparse
import collections
import datetime as dt
import json
import math
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from loudness_lab import estudio  # noqa: E402
from loudness_lab.sesiones import pausas_de, repasar  # noqa: E402

PLAN = os.path.join(RAIZ, "docs", "plan_v0.2")
TOLERANCIA = 0.01
fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def comparar(ref, nuevo, ruta, difs):
    if isinstance(ref, dict):
        for k, v in ref.items():
            comparar(v, (nuevo or {}).get(k), f"{ruta}.{k}", difs)
    elif isinstance(ref, (list, tuple)) and ref and not isinstance(ref[0], dict):
        for i, (a, b) in enumerate(zip(ref, nuevo or [])):
            comparar(a, b, f"{ruta}[{i}]", difs)
    elif isinstance(ref, (int, float)) and not isinstance(ref, bool):
        if isinstance(ref, float) and math.isnan(ref):
            if nuevo is not None and not (isinstance(nuevo, float) and math.isnan(nuevo)):
                difs.append((ruta, ref, nuevo))
        elif nuevo is None or abs(ref - nuevo) > TOLERANCIA:
            difs.append((ruta, ref, nuevo))


def cargar(carpeta, tramos, **opciones):
    with open(os.path.join(carpeta, "sesion.json"), encoding="utf-8") as f:
        s = json.load(f)
    cfg = s.get("config") if isinstance(s.get("config"), dict) else {}
    seg, eventos, n = repasar(carpeta, s, cfg, tramos=tramos, **opciones)
    seg.terminar()
    return s, seg, eventos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grabaciones", default=r"Q:\LoudnessLab\grabaciones")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if not all(os.path.exists(os.path.join(PLAN, r)) for r in ("valores_referencia.json",
                                                                 os.path.join("referencia", "catalogo_sesiones.json"))):
        print("Faltan los datos del estudio (docs/plan_v0.2/valores_referencia.json y referencia/catalogo_sesiones.json):\n"
              "no están en el repositorio. Esta prueba corre en el equipo del estudio, con sus grabaciones.")
        return 0
    catalogo = json.load(open(os.path.join(PLAN, "referencia", "catalogo_sesiones.json"), encoding="utf-8"))["sesiones"]
    ref = json.load(open(os.path.join(PLAN, "valores_referencia.json"), encoding="utf-8"))
    # el catálogo crece con las sesiones nuevas; el informe se hizo con estas
    del_informe = {r["sesion"] for r in ref["sesiones"]}
    catalogo = {k: v for k, v in catalogo.items() if k in del_informe or v.get("estado") == "no_valida"}

    print("1. Por sesión, por servicio y pares, contra el informe (sin la marca de corte incierto, como el informe)")
    filas, por_servicio, vivo = [], collections.defaultdict(list), {}
    for nombre, info in catalogo.items():
        carpeta = os.path.join(args.grabaciones, nombre)
        if info.get("estado") not in ("valida", "parcial") or not os.path.exists(os.path.join(carpeta, "mediciones.f32")):
            continue
        tramos = info.get("tramos_excluidos", [])
        s, seg, _ = cargar(carpeta, tramos, marcar_inciertos=False)
        e = seg.estadisticas()
        P, T = seg.cerradas, seg.transiciones
        filas.append(estudio.resumen_sesion(nombre, info, P, T, seg.z.v, seg.st.v, tramos))
        vivo[nombre] = e
        if info.get("estado") == "valida":
            por_servicio[info["servicio"]].append((nombre, P, T))
    difs = []
    por_nombre = {f["sesion"]: f for f in filas}
    for r in ref["sesiones"]:
        comparar(r, por_nombre.get(r["sesion"]), f"sesiones[{r['sesion']}]", difs)
    chequeo("las 9 sesiones reproducen el informe", not difs, f"({len(difs)} diferencias)" if difs else "")
    servicios = {sv: estudio.agregado_servicio(sv, d) for sv, d in por_servicio.items()}
    difs_s = []
    for r in ref["servicios"]:
        comparar(r, servicios.get(r["servicio"]), f"servicios[{r['servicio']}]", difs_s)
    chequeo("los 3 servicios (con intervalos) reproducen el informe", not difs_s,
            f"({len(difs_s)} diferencias)" if difs_s else "")
    _, pares = estudio.emparejar(por_servicio, "Soundtrack", "Spotify")
    difs_p = []
    comparar(ref["pares"], pares, "pares", difs_p)
    chequeo("las canciones pareadas Soundtrack–Spotify reproducen el informe", not difs_p,
            f"({pares.get('pares')} pares)")
    # las métricas de S4 por sesión (Seguidor.estadisticas) usan las mismas definiciones que las del servicio
    difs_c = []
    for r in ref["servicios"]:
        TV = [t for _, _, T in por_servicio.get(r["servicio"], []) for t in T if t["valida"]]
        m = estudio.metricas_cambios([], TV, float("nan"))
        comparar({k: r[k] for k in ("pct_dI_3", "pct_cuerpo_3", "pct_arranque_5", "p95_arranque")}, m,
                 f"metricas_cambios[{r['servicio']}]", difs_c)
    chequeo("las métricas de S4 por sesión, juntas por servicio, reproducen el informe", not difs_c,
            f"({len(difs_c)} diferencias)" if difs_c else "")
    for d in (difs + difs_s + difs_p + difs_c)[:12]:
        print("     ", d)

    print("\n2. Los promedios en vivo (Seguidor.estadisticas) dicen lo mismo que el informe")
    equiv = {"canciones": "canciones", "unicas": "unicas", "mediana": "lufs_mediana", "desv": "lufs_desv",
             "pct_05": "pct_05", "pct_1": "pct_1", "cambios": "cambios", "pct_dI_1": "pct_dI_1",
             "silencio_mediana_s": "silencio_mediana_s", "pct_tp_0": "pct_tp_0"}
    difs_v = []
    for nombre, e in vivo.items():
        comparar({k: por_nombre[nombre][v] for k, v in equiv.items()}, e, f"vivo[{nombre}]", difs_v)
    chequeo("vivo == informe en las 9 sesiones", not difs_v, f"({len(difs_v)} diferencias)" if difs_v else "")
    for d in difs_v[:8]:
        print("     ", d)
    e = vivo["Dinner and drinks soundtrack"]
    print(f"     ej. Dinner and drinks: {e['canciones']} canciones, mediana {e['mediana']:.2f}, ±0,5 LU {e['pct_05']:.1f} % "
          f"(IC {e['ic']['pct_05'][0]:.1f}–{e['ic']['pct_05'][1]:.1f}), horas válidas {e['horas_validas']:.2f}")

    print("\n3. Corte incierto (S15) en Mystify: qué cambia")
    carpeta = os.path.join(args.grabaciones, "Mystify radio 19-10-2026")
    if os.path.exists(carpeta):
        _, seg, _ = cargar(carpeta, [])
        e2 = seg.estadisticas()
        e1 = vivo["Mystify radio 19-10-2026"]
        print(f"     cambios válidos {e2['cambios']}, con corte incierto {e2['inciertos']}; "
              f"saltos en el borde contados {e1['transiciones']} → {e2['transiciones']}; "
              f"silencio mediano {e1['silencio_mediana_s']:.2f} → {e2['silencio_mediana_s']:.2f} s")
        chequeo("con S15, ΔI y nivel no cambian (no dependen del punto del corte)",
                e2["cambios"] == e1["cambios"] and abs(e2["pct_dI_1"] - e1["pct_dI_1"]) < 1e-9
                and e2["canciones"] == e1["canciones"])

    print("\n4. Alerta de cambio de estado: repaso de las sesiones válidas de Soundtrack y Spotify, sin tramos excluidos")
    alertas = {}
    for nombre, info in catalogo.items():
        if info.get("estado") != "valida" or info.get("servicio") not in ("Soundtrack", "Spotify"):
            continue
        carpeta = os.path.join(args.grabaciones, nombre)
        s, seg, eventos = cargar(carpeta, [])
        sr = int(s.get("sample_rate") or 48000)
        archivos = s.get("archivos") or []
        t0 = dt.datetime.fromisoformat(archivos[0]["hora_pc_inicio"]).timestamp()
        pausas = pausas_de(eventos, sr)
        horas = []
        for a in seg.alertas_nuevas:
            if a["activa"]:
                hora = t0 + a["t_s"] + sum(d for ts, d in pausas if ts < a["t_s"])
                horas.append((a["codigo"], dt.datetime.fromtimestamp(hora).strftime("%H:%M"), round(a["valor"], 2)))
        alertas[nombre] = horas
        print(f"     {nombre[:40]:40s} {horas or 'sin alertas'}")
    dinner = alertas.get("Dinner and drinks soundtrack", [])
    chequeo("Dinner and drinks: alerta de nivel a las 12:50", any(c == "nivel" and h == "12:50" for c, h, _ in dinner))
    chequeo("Dinner and drinks: alerta de transiciones a las 12:53",
            any(c == "transiciones" and h == "12:53" for c, h, _ in dinner))
    otras = {k: v for k, v in alertas.items() if k != "Dinner and drinks soundtrack" and v}
    chequeo("ninguna alerta en las otras 6 sesiones", not otras, str(otras) if otras else "")

    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 0 if not fallas else 1


if __name__ == "__main__":
    sys.exit(main())
