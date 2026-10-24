"""Las definiciones del estudio, en un solo lugar.

Lo usan el seguimiento en vivo (Seguidor), las sesiones reabiertas, el Excel y las pruebas, para que
todos cuenten igual. Es el código de referencia del informe del 20-oct-2026
(docs/plan_v0.2/referencia/comparar_servicios.py) llevado a la app; los resultados se comparan contra
docs/plan_v0.2/valores_referencia.json.

    canción válida       no es corta, no es parcial, no cae en un tramo excluido y tiene LUFS
    parcial              la primera y la última de la sesión, la que empieza tras una pausa o un corte
                         y la que se cierra por una pausa o un corte (llegan o se van empezadas)
    excluida             se cruza con un tramo excluido (error de operación, en segundos de audio)
    repetidas            misma clave (artista principal + título normalizados): en los agregados por
                         servicio cuenta una vez (promedio de sus reproducciones)
    cuerpo               mediana del short-term (> −60 LUFS) sin los primeros ni los últimos 10 s
    ΔI                   LUFS integrado de la canción nueva − el de la anterior
    salto de cuerpo      mediana ST de [b+10 s, b+70 s] de la nueva − mediana ST de los 60 s antes de
                         los últimos 10 s de la anterior
    subida de arranque   máximo ST en [b+3 s, b+15 s] de la nueva − esa misma referencia anterior
    salto en el borde    LUFS de los primeros 10 s de la nueva − últimos 10 s de la anterior
    silencio             cola de la anterior + cabeza de la nueva: segundos con momentary más de 20 LU
                         bajo el cuerpo de cada una, pegados al corte
    corte incierto       con una radio web, el ajuste fino no encontró un hueco: el corte quedó donde
                         llegó el título (no se calculan borde, arranque ni silencio)
"""

import collections
import difflib
import math
import re
import unicodedata

import numpy as np

from .bs1770 import integrado, lra

SILENCIO = -60.0
SEMILLA = 7
REPETICIONES = 4000

# Alerta de cambio de estado (calibrada con las 7 sesiones válidas de Soundtrack y Spotify)
NIVEL_ULTIMAS, NIVEL_ANTERIORES = 3, 10
NIVEL_ALERTA_LU, NIVEL_REARME_LU = 2.0, 1.5
TRANS_VENTANA = 10
TRANS_ALERTA, TRANS_REARME = 0.5, 0.3
ENCIMADO_LU = 10.0         # un cambio es «encimado» si el momentary no baja ni 10 LU bajo los cuerpos


# ------------------------------------------------------------------------------ por canción
def med_st(st, a, b):
    v = st[max(a, 0):max(b, 0)]
    v = v[~np.isnan(v)]
    v = v[v > SILENCIO]
    return float(np.median(v)) if len(v) else math.nan


def excluida(ini_s, fin_s, tramos):
    return any(ini_s < t["hasta_s"] and fin_s > t["desde_s"] for t in tramos or [])


def medir_cancion(p, st, mom):
    """Lo que se mide de una canción al cerrarla (no cambia después): bloques, cuerpo, cola, cabeza
    y máximo short-term."""
    a = int(round(p["inicio_s"] * 10))
    b = a + int(round(p["duracion_s"] * 10))
    cuerpo = med_st(st, a + 100, b - 100) if b - a > 300 else math.nan
    cola = cabeza = math.nan
    if not math.isnan(cuerpo):
        alto = np.nonzero(mom[a:b] > cuerpo - 20)[0]
        if len(alto):
            cola, cabeza = (b - a - 1 - alto[-1]) / 10, alto[0] / 10
    st_max = float(np.nanmax(st[a + 30:b])) if b - a > 40 else math.nan
    return {"a": a, "b": b, "cuerpo": cuerpo, "cola_s": cola, "cabeza_s": cabeza, "st_max": st_max}


def marcar(canciones, tramos=None, terminada=False):
    """Pone `parcial`, `excluida` y `valida` a cada canción (las marcas pueden cambiar mientras la
    sesión sigue: la última solo es parcial cuando la sesión termina)."""
    n = len(canciones)
    for i, p in enumerate(canciones):
        p["parcial"] = bool(i == 0 or (terminada and i == n - 1) or p.get("tras_pausa") or p.get("seguida_de_pausa"))
        p["excluida"] = excluida(p["inicio_s"], p["inicio_s"] + p["duracion_s"], tramos)
        p["valida"] = not (p["corta"] or p["parcial"] or p["excluida"] or _nan(p.get("lufs")))
    return canciones


def _nan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


# ------------------------------------------------------------------------------ por cambio
def medir_cambio(p, q, st):
    """El cambio de la canción p a la q (ya medidas con medir_cancion), o None si no cuentan como
    un cambio: alguna es corta, la nueva empieza tras una pausa o hay más de 1 s entre ellas."""
    if p["corta"] or q["corta"] or q.get("tras_pausa"):
        return None
    if q["inicio_s"] - (p["inicio_s"] + p["duracion_s"]) > 1:
        return None
    b = q["a"]
    ref_prev = med_st(st, max(p["a"], p["b"] - 700), p["b"] - 100)
    ref_next = med_st(st, q["a"] + 100, min(q["b"], q["a"] + 700))
    arranque = float(np.nanmax(st[b + 30:min(q["b"], b + 150)])) if q["b"] - b > 160 else math.nan
    silencio = (p["cola_s"] + q["cabeza_s"]) if not (_nan(p["cola_s"]) or _nan(q["cabeza_s"])) else math.nan
    return {"t_s": q["inicio_s"], "de": f"{p['artista']} - {p['titulo']}", "a": f"{q['artista']} - {q['titulo']}",
            "salto_borde": q["primeros_10s"] - p["ultimos_10s"], "delta_i": q["lufs"] - p["lufs"],
            "salto_cuerpo": ref_next - ref_prev, "subida_arranque": arranque - ref_prev, "silencio_s": silencio,
            "incierto": bool(q.get("corte_incierto"))}


def marcar_cambios(cambios, canciones):
    """`valida` de cada cambio: ninguna de las dos canciones es parcial ni excluida. Los cambios guardan
    los índices de sus canciones (i_de, i_a)."""
    for t in cambios:
        p, q = canciones[t["i_de"]], canciones[t["i_a"]]
        t["valida"] = not (p["parcial"] or q["parcial"] or p["excluida"] or q["excluida"])
    return cambios


# ------------------------------------------------------------------------------ estadística
def pct(c):
    c = np.asarray(c)
    return float(np.mean(c) * 100) if c.size else math.nan


def limpio(v):
    v = np.asarray(v, dtype=float)
    return v[~np.isnan(v)]


def ic(x, f):
    """Intervalo de confianza al 95 % por bootstrap (4000 remuestreos, semilla fija): (inferior, superior).
    `f` recibe la matriz de remuestreos (uno por fila) y devuelve un valor por fila. Sacar todos los
    remuestreos de una vez da la misma secuencia que sacarlos de a uno (comprobado), y es ~20 veces más
    rápido."""
    x = np.asarray(x, dtype=float)
    if len(x) < 3:
        return (math.nan, math.nan)
    rng = np.random.default_rng(SEMILLA)
    v = f(x[rng.integers(0, len(x), (REPETICIONES, len(x)))])
    return tuple(float(t) for t in np.percentile(v, [2.5, 97.5]))


def _desv(m):
    return m.std(axis=1, ddof=1)


def _dentro(lim):
    return lambda m: np.mean(np.abs(m) <= lim, axis=1) * 100


def _fuera(lim):
    return lambda m: np.mean(np.abs(m) > lim, axis=1) * 100


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)
    s = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo|single|radio|album).*$", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clave(p):
    """La misma canción aunque cambie el formato del título: (artista principal, título) normalizados."""
    return (norm(re.split(r",|&| feat| x |;", p.get("artista") or "")[0]), norm(p.get("titulo")))


def niveles(canciones):
    """De las canciones válidas: (LUFS por reproducción, mediana)."""
    L = np.array([q["lufs"] for q in canciones if q.get("valida")], dtype=float)
    return L, (float(np.median(L)) if len(L) else math.nan)


def intervalos(canciones, cambios):
    """Los intervalos de confianza de la sesión (se calculan aparte: tardan ~0,5 s)."""
    L, med = niveles(canciones)
    d = L - med
    TV = [t for t in cambios if t.get("valida")]
    dI = limpio([t["delta_i"] for t in TV])
    sc = limpio([t["salto_cuerpo"] for t in TV])
    per = limpio([q.get("lufs_percibido", math.nan) for q in canciones if q.get("valida")])
    return {"desv": ic(d, _desv), "pct_05": ic(d, _dentro(0.5)), "pct_1": ic(d, _dentro(1)),
            "pct_dI_1": ic(dI, _fuera(1)), "pct_dI_3": ic(dI, _fuera(3)), "pct_cuerpo_3": ic(sc, _fuera(3)),
            "percibido_desv": ic(per, _desv)}


def metricas_cambios(V, TV, med):
    """Las métricas de S4 en una sesión, con las definiciones de agregado_servicio: ΔI y salto de cuerpo
    en todos los cambios válidos; subida de arranque y silencio sin los cortes inciertos (dependen de
    dónde cae el corte); short-term máximo de cada canción válida sobre la mediana de la sesión."""
    seguros = [t for t in TV if not t.get("incierto")]
    dI, sc = limpio([t["delta_i"] for t in TV]), limpio([t["salto_cuerpo"] for t in TV])
    sa, gap = limpio([t["subida_arranque"] for t in seguros]), limpio([t["silencio_s"] for t in seguros])
    stmax = limpio([q["st_max"] - med for q in V]) if V and not math.isnan(med) else np.zeros(0)
    return {
        "pct_dI_3": pct(np.abs(dI) > 3) if len(dI) else math.nan,
        "cambios_cuerpo": int(len(sc)),
        "pct_cuerpo_3": pct(np.abs(sc) > 3) if len(sc) else math.nan,
        "cuerpo_p95": float(np.percentile(np.abs(sc), 95)) if len(sc) else math.nan,
        "cambios_arranque": int(len(sa)),
        "pct_arranque_5": pct(sa > 5) if len(sa) else math.nan,
        "p95_arranque": float(np.percentile(sa, 95)) if len(sa) else math.nan,
        "pct_stmax_5": pct(stmax > 5) if len(stmax) else math.nan,
        "pct_silencio_2": pct(gap > 2) if len(gap) else math.nan,
    }


def metricas_sonoridad(V):
    """Sonoridad percibida de las canciones válidas (S7): LN y LUFS percibido, y la dispersión de LUFS en
    esas mismas canciones, para comparar (la hipótesis del restorán: a igual LUFS, ¿suenan distinto?)."""
    con = [q for q in V if not _nan(q.get("ln")) and not _nan(q.get("lufs_percibido"))]
    ln = np.array([q["ln"] for q in con], dtype=float)
    per = np.array([q["lufs_percibido"] for q in con], dtype=float)
    L = np.array([q["lufs"] for q in con], dtype=float)
    return {
        "con_sonoridad": len(con),
        "ln_mediana": float(np.median(ln)) if len(ln) else math.nan,
        "ln_desv": float(ln.std(ddof=1)) if len(ln) > 1 else math.nan,
        "percibido_desv": float(per.std(ddof=1)) if len(per) > 1 else math.nan,
        "lufs_desv_con_sonoridad": float(L.std(ddof=1)) if len(L) > 1 else math.nan,
        "dif_percibido_mediana": float(np.median(per - L)) if len(L) else math.nan,
    }


def resumen_sesion(nombre, info, canciones, cambios, z, st, tramos):
    """Una fila por sesión, como el informe (ver valores_referencia.json, «sesiones»)."""
    n = len(z)
    mask = np.ones(n, bool)
    for t in tramos or []:
        mask[int(t["desde_s"] * 10):int(t["hasta_s"] * 10)] = False
    st_m = st[mask]
    musica = st_m > SILENCIO
    V = [q for q in canciones if q["valida"]]
    L, med = niveles(canciones)
    TV = [t for t in cambios if t["valida"]]
    dI = limpio([t["delta_i"] for t in TV])
    gap = limpio([t["silencio_s"] for t in TV if not t.get("incierto")])
    zm = z[mask]
    return {
        "sesion": nombre, "servicio": info.get("servicio"), "playlist": info.get("playlist"),
        "estado": info.get("estado"), "horas": round(int(mask.sum()) / 36000, 2),
        "canciones": len(V), "unicas": len({clave(q) for q in V}),
        "lufs_mediana": med, "lufs_desv": float(L.std(ddof=1)) if len(L) > 1 else math.nan,
        "pct_05": pct(np.abs(L - med) <= 0.5) if len(L) else math.nan,
        "pct_1": pct(np.abs(L - med) <= 1) if len(L) else math.nan,
        "cambios": len(dI), "pct_dI_1": pct(np.abs(dI) > 1) if len(dI) else math.nan,
        "silencio_mediana_s": float(np.median(gap)) if len(gap) else math.nan,
        "pct_tp_0": pct(np.array([q["tp"] for q in V]) > 0) if V else math.nan,
        "lufs_sesion": integrado(zm, 0, len(zm)), "lra_sesion": lra(zm, 0, len(zm)),
        "pct_tiempo_st_fuera3": pct(np.abs(st_m[musica] - np.median(st_m[musica])) > 3) if musica.any() else math.nan,
        "nota": info.get("nota", ""),
    }


def agregado_servicio(servicio, datos):
    """datos: lista de (nombre, canciones, cambios) de las sesiones del servicio. Cada canción cuenta una
    vez (promedio de sus reproducciones válidas, relativas a la mediana de su sesión)."""
    devs = collections.defaultdict(list)
    tps, lras, stmax = collections.defaultdict(list), collections.defaultdict(list), []
    for _, Q, _ in datos:
        V = [q for q in Q if q["valida"]]
        if not V:
            continue
        med = np.median([q["lufs"] for q in V])
        for q in V:
            k = clave(q)
            devs[k].append(q["lufs"] - med)
            tps[k].append(q["tp"])
            lras[k].append(q["lra"])
            if not math.isnan(q["st_max"]):
                stmax.append(q["st_max"] - med)
    dev = np.array([np.mean(v) for v in devs.values()])
    tp = np.array([np.max(v) for v in tps.values()])
    lr = limpio([np.mean(v) for v in lras.values()])
    TV = [t for _, _, T in datos for t in T if t["valida"]]
    seguros = [t for t in TV if not t.get("incierto")]
    dI, sc = limpio([t["delta_i"] for t in TV]), limpio([t["salto_cuerpo"] for t in TV])
    sa, sb = limpio([t["subida_arranque"] for t in seguros]), limpio([t["salto_borde"] for t in seguros])
    if not len(dev):
        return {"servicio": servicio, "canciones": 0}
    return {
        "servicio": servicio, "sesiones": len(datos), "canciones": int(len(dev)), "cambios": int(len(dI)),
        "desv": float(dev.std(ddof=1)), "desv_ic": ic(dev, _desv),
        "pct_05": pct(np.abs(dev) <= 0.5), "pct_05_ic": ic(dev, _dentro(0.5)),
        "pct_1": pct(np.abs(dev) <= 1), "pct_1_ic": ic(dev, _dentro(1)),
        "pct_dI_1": pct(np.abs(dI) > 1), "pct_dI_1_ic": ic(dI, _fuera(1)),
        "pct_dI_3": pct(np.abs(dI) > 3), "pct_cuerpo_3": pct(np.abs(sc) > 3),
        "pct_arranque_5": pct(sa > 5), "p95_arranque": float(np.percentile(sa, 95)) if len(sa) else math.nan,
        "pct_stmax_5": pct(np.array(stmax) > 5), "pct_borde_3": pct(np.abs(sb) > 3),
        "pct_tp_0": pct(tp > 0), "pct_tp_m1": pct(tp > -1), "lra_mediana": float(np.median(lr)) if len(lr) else math.nan,
    }


def emparejar(por_servicio, a, b):
    """Mismas canciones en dos servicios: promedio de sus reproducciones válidas en cada uno."""
    def indice(datos):
        idx = collections.defaultdict(list)
        for _, Q, _ in datos:
            for q in Q:
                if q["valida"]:
                    idx[clave(q)].append(q)
        return idx

    A, B = indice(por_servicio.get(a, [])), indice(por_servicio.get(b, []))
    filas = []
    for k, qa in A.items():
        qb = B.get(k)
        if not qb:   # difuso: mismo artista, título parecido
            cand = [(difflib.SequenceMatcher(None, k[1], kb[1]).ratio(), kb) for kb in B if kb[0] == k[0]]
            cand = [c for c in cand if c[0] >= 0.85]
            qb = B[max(cand)[1]] if cand else None
        if not qb:
            continue

        def m(qs, c):
            return float(np.mean([q[c] for q in qs]))
        filas.append({"artista": qa[0]["artista"], "titulo": qa[0]["titulo"],
                      f"lufs_{a}": m(qa, "lufs"), f"lufs_{b}": m(qb, "lufs"),
                      f"tp_{a}": max(q["tp"] for q in qa), f"tp_{b}": max(q["tp"] for q in qb),
                      f"lra_{a}": m(qa, "lra"), f"lra_{b}": m(qb, "lra"),
                      f"cola_{a}": m(qa, "cola_s"), f"cola_{b}": m(qb, "cola_s")})
    if not filas:
        return filas, {}
    la, lb = np.array([f[f"lufs_{a}"] for f in filas]), np.array([f[f"lufs_{b}"] for f in filas])
    res = {"pares": len(filas),
           f"pct_05_{a}": pct(np.abs(la - np.median(la)) <= 0.5), f"pct_05_{b}": pct(np.abs(lb - np.median(lb)) <= 0.5),
           f"mediana_{a}": float(np.median(la)), f"mediana_{b}": float(np.median(lb)),
           "dif_mediana": float(np.median(la - lb))}
    return filas, res


# ------------------------------------------------------------------------------ alerta en vivo
class AlertaEstado:
    """Avisa si, a mitad de una sesión, el nivel de las canciones o la forma de los cambios cambia de
    golpe (por ejemplo, alguien tocó el volumen o la configuración de la zona). Se alimenta al cerrar
    cada canción y cada cambio; devuelve eventos {codigo, activa, texto} cuando una alerta se enciende
    o se apaga.

    nivel          mediana de LUFS de las últimas 3 canciones válidas contra la de las 10 anteriores:
                   alerta sobre 2,0 LU, se rearma bajo 1,5 LU. No aplica a radios (sin normalización).
    transiciones   fracción de cambios «encimados» (el momentary no baja 10 LU bajo los cuerpos en ±3 s
                   del corte: hay crossfade) en los últimos 10 cambios contra los 10 anteriores: alerta
                   si difiere 0,5 o más, se rearma bajo 0,3.

    Una alerta no se rearma hasta que sus dos ventanas se renovaron enteras (13 canciones o 20 cambios
    después de encenderse): antes, las ventanas todavía mezclan lo de antes y lo de después del mismo
    cambio, y la alerta se apagaba y volvía a encender por lo mismo.
    """

    def __init__(self, nivel_aplica=True):
        self.nivel_aplica = nivel_aplica
        self.lufs = []
        self.encimados = []
        self.activas = {"nivel": False, "transiciones": False}
        self.desde = {"nivel": 0, "transiciones": 0}    # cuántos datos había al encenderse

    def _cambiar(self, codigo, activa, texto, valor):
        if activa == self.activas[codigo]:
            return []
        if not activa:
            datos, ancho = ((self.lufs, NIVEL_ULTIMAS + NIVEL_ANTERIORES) if codigo == "nivel"
                            else (self.encimados, 2 * TRANS_VENTANA))
            if len(datos) - self.desde[codigo] < ancho:
                return []          # todavía es el mismo cambio
        self.activas[codigo] = activa
        self.desde[codigo] = len(self.lufs if codigo == "nivel" else self.encimados)
        return [{"codigo": codigo, "activa": activa, "texto": texto, "valor": valor}]

    def cancion(self, p):
        """Una canción cerrada que (por ahora) es válida."""
        if not self.nivel_aplica or _nan(p.get("lufs")):
            return []
        self.lufs.append(p["lufs"])
        if len(self.lufs) < NIVEL_ULTIMAS + NIVEL_ANTERIORES:
            return []
        ult = self.lufs[-NIVEL_ULTIMAS:]
        ant = self.lufs[-(NIVEL_ULTIMAS + NIVEL_ANTERIORES):-NIVEL_ULTIMAS]
        d = float(np.median(ult) - np.median(ant))
        if not self.activas["nivel"] and abs(d) > NIVEL_ALERTA_LU:
            return self._cambiar("nivel", True, f"El nivel de las canciones cambió {d:+.1f} LU respecto de las "
                                                "anteriores: ¿se tocó el volumen o la normalización?", d)
        if self.activas["nivel"] and abs(d) < NIVEL_REARME_LU:
            return self._cambiar("nivel", False, "", d)
        return []

    def cambio(self, p, q, mom):
        """Un cambio de canción contiguo entre dos canciones con cuerpo."""
        if _nan(p.get("cuerpo")) or _nan(q.get("cuerpo")):
            return []
        b = q["a"]
        zona = mom[max(0, b - 30):b + 30]
        zona = zona[~np.isnan(zona)]
        if not len(zona):
            return []
        self.encimados.append(((p["cuerpo"] + q["cuerpo"]) / 2 - float(zona.min())) < ENCIMADO_LU)
        if len(self.encimados) < 2 * TRANS_VENTANA:
            return []
        ult = np.mean(self.encimados[-TRANS_VENTANA:])
        ant = np.mean(self.encimados[-2 * TRANS_VENTANA:-TRANS_VENTANA])
        d = float(ult - ant)
        if not self.activas["transiciones"] and abs(d) >= TRANS_ALERTA:
            como = "más encimados (con crossfade)" if d > 0 else "con más silencio entre canciones"
            return self._cambiar("transiciones", True, f"Los cambios de canción pasaron a ser {como} "
                                                       f"({100 * ant:.0f} % → {100 * ult:.0f} % encimados): "
                                                       "¿se tocó el crossfade o la configuración?", d)
        if self.activas["transiciones"] and abs(d) < TRANS_REARME:
            return self._cambiar("transiciones", False, "", d)
        return []
