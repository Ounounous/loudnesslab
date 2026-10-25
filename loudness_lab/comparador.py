"""Comparador de sesiones y servicios (plan v0.2, S8).

Rehace cada sesión desde sus archivos (sin audio) con las definiciones del estudio (estudio.py), agrega
por servicio con intervalos de confianza, empareja las mismas canciones entre servicios y escribe un
Excel con las hojas Sesiones, Servicios, Canciones, Cambios, Pares y Espectro (más Notas). Es el
`comparar_servicios.py` con que se hizo el informe del 20-oct, llevado a la app: con el mismo catálogo
reproduce sus números (tests/test_comparador.py).

El catálogo (servicio, lista, si vale, tramos excluidos) es el de cada sesion.json (`estudio`). Una
sesión sin esos datos no entra y se informa, para completarla en «Datos del estudio…».

  LoudnessLab.exe --comparar [carpeta de grabaciones] [--salida archivo.xlsx] [--json valores.json]
"""

import collections
import datetime as dt
import itertools
import json
import math
import os

import numpy as np

from . import VERSION, estudio, sesiones

CAMPOS_CANCION = ("inicio_s", "duracion_s", "artista", "titulo", "album", "lufs", "lra", "tp", "cuerpo", "st_max",
                  "primeros_10s", "ultimos_10s", "cola_s", "cabeza_s", "corta", "parcial", "excluida", "valida",
                  "delta_i", "salto_cuerpo", "subida_arranque", "silencio_previo_s", "lufs_percibido", "ln", "n5",
                  "desajuste_db")
NOTAS = [
    ("Canción válida", "No es corta, no es parcial (la primera, la última y las pegadas a una pausa o corte) y no cae "
                       "en un tramo excluido. Solo las válidas cuentan."),
    ("Servicios", "Cada canción cuenta una vez: el promedio de sus reproducciones válidas, relativas a la mediana de "
                  "su sesión. Entre corchetes (columnas _ic): intervalo de confianza al 95 % (bootstrap, 4000)."),
    ("ΔI", "LUFS integrado de la canción nueva − el de la anterior."),
    ("Salto de cuerpo", "Mediana del short-term de la nueva (de los 10 a los 70 s) − la del final de la anterior "
                        "(sin sus últimos 10 s)."),
    ("Subida de arranque", "Máximo short-term de los 3 a los 15 s de la nueva − esa misma referencia."),
    ("Salto en el borde", "LUFS de los primeros 10 s de la nueva − los últimos 10 s de la anterior."),
    ("Silencio entre canciones", "Cola de la anterior + cabeza de la nueva: segundos con momentary más de 20 LU bajo el "
                                 "cuerpo de cada una, pegados al corte. Sin los cortes inciertos de radios web."),
    ("LUFS percibido", "Sonoridad de Zwicker (ISO 532-1) en escala LUFS: el LUFS al que un ruido rosa sonaría igual de "
                       "fuerte. desv_percibido: su dispersión entre canciones, junto a la de sus LUFS "
                       "(desv_lufs_con_sonoridad), en las canciones con capa de frecuencias."),
    ("Pares", "Las mismas canciones en dos servicios (artista principal y título normalizados; título parecido si no "
              "calza exacto): promedio de sus reproducciones válidas en cada uno."),
]


def _nombre(carpeta):
    return os.path.basename(os.path.normpath(carpeta))


def candidatas(base):
    """Las sesiones terminadas de `base` con lo que dicen sus datos del estudio: [{carpeta, nombre, horas,
    servicio, estado, capa, falta}]. `falta`: por qué no entraría al comparar ("" si entra)."""
    from . import capa
    out = []
    for s in sesiones.listar(base):
        if s["estado"] != "terminada":
            continue
        j = sesiones._json(os.path.join(s["carpeta"], "sesion.json")) or {}
        e = sesiones.estudio_de(j)
        falta = ("no válida para el estudio" if e.get("estado") == "no_valida"
                 else "" if e.get("servicio") and e.get("estado") in ("valida", "parcial")
                 else "faltan los datos del estudio (servicio y si vale)")
        out.append({"carpeta": s["carpeta"], "nombre": _nombre(s["carpeta"]), "horas": s["horas"],
                    "servicio": e.get("servicio") or "", "estado": e.get("estado") or "", "falta": falta,
                    "capa": bool(capa.filas_espectro(s["carpeta"]))})
    return out


def _fila_cancion(nombre, servicio, q):
    d = {"sesion": nombre, "servicio": servicio, **{c: q.get(c) for c in CAMPOS_CANCION}}
    d["graves_pct"] = (q.get("reparto") or {}).get("graves")
    return d


def comparar(carpetas, catalogo=None, al_avanzar=None, marcar_inciertos=True):
    """Compara las sesiones de `carpetas`. `catalogo`: {nombre de carpeta: datos del estudio} para usar en vez
    de los de cada sesion.json (la prueba de regresión usa el del informe). Devuelve {sesiones, servicios,
    canciones, cambios, pares: {(a, b): (filas, resumen)}, espectro, omitidas: [(nombre, motivo)]}."""
    filas, por_servicio, Qs, Ts, Es, omitidas = [], collections.defaultdict(list), [], [], [], []
    for k, carpeta in enumerate(carpetas):
        nombre = _nombre(carpeta)
        if al_avanzar:
            al_avanzar(k / max(1, len(carpetas)), nombre)
        s = sesiones._json(os.path.join(carpeta, "sesion.json"))
        if s is None:
            omitidas.append((nombre, "no es una sesión (falta sesion.json)"))
            continue
        info = dict(catalogo[nombre]) if catalogo and nombre in catalogo else sesiones.estudio_de(s)
        if info.get("estado") == "no_valida":
            omitidas.append((nombre, "no válida para el estudio"))
            continue
        if not (info.get("servicio") and info.get("estado") in ("valida", "parcial")):
            omitidas.append((nombre, "faltan los datos del estudio (servicio y si vale)"))
            continue
        if not os.path.exists(os.path.join(carpeta, "mediciones.f32")):
            omitidas.append((nombre, "no tiene mediciones"))
            continue
        tramos = info.get("tramos_excluidos") or []
        seg, _, _ = sesiones.repasar(carpeta, s, sesiones._config_de(s, carpeta), tramos=tramos,
                                     marcar_inciertos=marcar_inciertos)
        seg.terminar()
        e = seg.estadisticas(intervalos=False)    # (además marca canciones y cambios válidos)
        Q, T = seg.cerradas, seg.transiciones
        V = [q for q in Q if q["valida"]]
        _, med = estudio.niveles(Q)
        fila = estudio.resumen_sesion(nombre, info, Q, T, seg.z.v, seg.st.v, tramos)
        fila.update(estudio.metricas_cambios(V, [t for t in T if t["valida"]], med))
        fila.update(estudio.metricas_sonoridad(V))
        fila["capa_frecuencias"] = seg.sonoridad is not None
        fila["techo_cadena_dbfs"] = (e.get("techo_cadena") or {}).get("dbfs")
        filas.append(fila)
        if info["estado"] == "valida":
            por_servicio[info["servicio"]].append((nombre, Q, T))
        Qs += [_fila_cancion(nombre, info["servicio"], q) for q in Q]
        Ts += [dict(sesion=nombre, servicio=info["servicio"], **t) for t in T]
        Es += [{"sesion": nombre, "servicio": info["servicio"], "artista": q.get("artista"), "titulo": q.get("titulo"),
                "valida": q["valida"], **{f"{r}_pct": (q.get("reparto") or {}).get(r) for r in ("graves", "medios_bajos",
                                                                                           "medios", "agudos")},
                "desajuste_db": q.get("desajuste_db"), "lufs": q.get("lufs"), "lufs_percibido": q.get("lufs_percibido"),
                "ln": q.get("ln"), "espectro": q["espectro"]} for q in Q if q.get("espectro")]
    if al_avanzar:
        al_avanzar(1.0, "agregando por servicio")
    servicios = []
    for sv, datos in por_servicio.items():
        a = estudio.agregado_servicio(sv, datos)
        a.update(estudio.sonoridad_servicio(datos))
        servicios.append(a)
    pares = {}
    for a, b in itertools.combinations(sorted(por_servicio), 2):
        f, r = estudio.emparejar(por_servicio, a, b)
        if f:
            pares[(a, b)] = (f, r)
    return {"sesiones": filas, "servicios": servicios, "canciones": Qs, "cambios": Ts, "pares": pares,
            "espectro": Es, "omitidas": omitidas}


# ------------------------------------------------------------------------------ Excel
def _vacio(v):
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _plano(fila):
    """Una fila para el Excel: los intervalos (tuplas) en dos columnas, _inf y _sup."""
    out = {}
    for k, v in fila.items():
        if isinstance(v, (tuple, list)) and len(v) == 2 and k.endswith("_ic"):
            out[f"{k}_inf"], out[f"{k}_sup"] = v
        else:
            out[k] = v
    return out


def escribir_excel(ruta, r):
    """El libro del comparador. Las columnas llevan los nombres de los campos (los mismos del informe)."""
    import xlsxwriter
    from .bandas import NOMINALES
    libro = xlsxwriter.Workbook(str(ruta), {"nan_inf_to_errors": False})
    try:
        base = {"font_name": "Calibri", "font_size": 11}
        enc = libro.add_format({**base, "bold": True, "bg_color": "#2f6db5", "font_color": "#ffffff", "text_wrap": True,
                                "valign": "vcenter"})
        f_num = libro.add_format({**base, "num_format": "0.00"})
        f_txt = libro.add_format(base)
        f_gris = libro.add_format({**base, "font_color": "#6b7280", "text_wrap": True, "valign": "top"})

        def hoja(nombre, filas):
            h = libro.add_worksheet(nombre[:31])
            filas = [_plano(f) for f in filas]
            if not filas:
                h.write_string(0, 0, "Sin datos.", f_gris)
                return
            cols = list(dict.fromkeys(c for f in filas for c in f))
            h.set_row(0, 30)
            for j, c in enumerate(cols):
                h.write_string(0, j, c, enc)
                h.set_column(j, j, 30 if c in ("sesion", "de", "a", "titulo", "artista") else 13)
            for i, f in enumerate(filas, 1):
                for j, c in enumerate(cols):
                    v = f.get(c)
                    if isinstance(v, (bool, np.bool_)):
                        h.write_string(i, j, "sí" if v else "no", f_txt)
                    elif isinstance(v, (int, float, np.integer, np.floating)) and not _vacio(float(v)):
                        h.write_number(i, j, float(v), f_num)
                    elif not _vacio(v) and v != "":
                        h.write_string(i, j, str(v), f_txt)
            h.freeze_panes(1, 1)
            h.autofilter(0, 0, len(filas), len(cols) - 1)

        hoja("Sesiones", r["sesiones"])
        hoja("Servicios", r["servicios"])
        hoja("Canciones", r["canciones"])
        hoja("Cambios", r["cambios"])
        if not r["pares"]:
            hoja("Pares", [])
        for (a, b), (filas, _) in r["pares"].items():
            hoja(f"Pares {a}-{b}", filas)
        etiquetas = [f"{f / 1000:g} kHz" if f >= 1000 else f"{f:g} Hz" for f in NOMINALES]
        hoja("Espectro", [{**{k: v for k, v in e.items() if k != "espectro"},
                           **dict(zip(etiquetas, (10 * np.log10(np.maximum(np.asarray(e["espectro"]), 1e-30))).tolist()))}
                          for e in r["espectro"]])
        h = libro.add_worksheet("Notas")
        h.set_column(0, 0, 26)
        h.set_column(1, 1, 110)
        h.write_string(0, 0, "Comparador de Loudness Lab", enc)
        h.write_string(0, 1, f"{VERSION} · {dt.datetime.now():%d-%m-%Y %H:%M}", enc)
        fila = 2
        for t, d in NOTAS:
            h.write_string(fila, 0, t, f_txt)
            h.write_string(fila, 1, d, f_gris)
            fila += 1
        for (a, b), (_, res) in r["pares"].items():
            fila += 1
            h.write_string(fila, 0, f"Pares {a}–{b}", f_txt)
            h.write_string(fila, 1, ", ".join(f"{k}: {round(v, 2) if isinstance(v, float) else v}" for k, v in res.items()),
                           f_gris)
        if r["omitidas"]:
            fila += 2
            h.write_string(fila, 0, "Sesiones que no entraron", enc)
            for nombre, motivo in r["omitidas"]:
                fila += 1
                h.write_string(fila, 0, nombre, f_txt)
                h.write_string(fila, 1, motivo, f_gris)
    finally:
        libro.close()


def comparar_y_escribir(carpetas, ruta, al_avanzar=None):
    """Para la ventana: compara y escribe el Excel. Devuelve lo comparado."""
    r = comparar(carpetas, al_avanzar=al_avanzar)
    escribir_excel(ruta, r)
    return r


# ------------------------------------------------------------------------------ comando
def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="LoudnessLab --comparar",
                                 description="Compara las sesiones terminadas (por sesión y por servicio).")
    ap.add_argument("--comparar", action="store_true")
    ap.add_argument("base", nargs="?", default=r"Q:\LoudnessLab\grabaciones", help="carpeta de grabaciones")
    ap.add_argument("--salida", default=None, help="Excel (por defecto, comparacion_servicios.xlsx en la carpeta)")
    ap.add_argument("--json", default=None, help="guarda los valores agregados (sesiones, servicios y pares)")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cand = candidatas(args.base)
    for c in cand:
        if c["falta"]:
            print(f"  (no entra) {c['nombre']}: {c['falta']}")
    r = comparar([c["carpeta"] for c in cand if not c["falta"]],
                 al_avanzar=lambda f, t: print(f"  [{100 * f:3.0f} %] {t}", flush=True))
    for a in r["servicios"]:
        if a.get("canciones"):
            print(f"{a['servicio']:11s} canciones {a['canciones']:4d}  desv {a['desv']:.2f}  ±0,5 LU {a['pct_05']:.0f} %  "
                  f"|ΔI|>1 {a['pct_dI_1']:.0f} %  cuerpo>3 {a['pct_cuerpo_3']:.0f} %")
    salida = args.salida or os.path.join(args.base, "comparacion_servicios.xlsx")
    escribir_excel(salida, r)
    print(f"Excel: {salida}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"sesiones": r["sesiones"], "servicios": r["servicios"],
                       "pares": {f"{a}-{b}": res for (a, b), (_, res) in r["pares"].items()}}, f, indent=1,
                      ensure_ascii=False, default=float)
    return 0
