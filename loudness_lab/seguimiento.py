"""Seguimiento en vivo: arma las canciones a partir de los avisos de la metadata y lleva las
estadísticas de la sesión mientras se graba.

Replica la segmentación de analizar.py (mismo ajuste del corte al silencio real, mismas
exclusiones de canciones cortas y de cambios con pausa) para que lo que se ve en vivo coincida
con el informe final. Lo único que no replica es unir dos trozos de una misma canción separados
por un parpadeo de la metadata: eso lo resuelve el análisis final.
"""

import math

import numpy as np

from .bs1770 import SILENCIO, db, integrado, lra, lufs, refinar_corte

NAN = float("nan")


class Serie:
    """Arreglo que crece sin copiarse en cada agregado (duplica su capacidad al llenarse)."""

    def __init__(self, capacidad=36000):
        self._a = np.empty(capacidad)
        self.n = 0

    def agregar(self, valores):
        v = np.asarray(valores, dtype=float).ravel()
        if self.n + len(v) > len(self._a):
            nuevo = np.empty(max(2 * len(self._a), self.n + len(v)))
            nuevo[:self.n] = self._a[:self.n]
            self._a = nuevo
        self._a[self.n:self.n + len(v)] = v
        self.n += len(v)

    @property
    def v(self):
        return self._a[:self.n]

    def __len__(self):
        return self.n


def _ventana_lufs(z, nuevos, ancho):
    """LUFS de la media de `ancho` bloques terminada en cada uno de los últimos `nuevos` bloques.
    NaN donde todavía no hay `ancho` bloques (igual que media_movil)."""
    n = len(z)
    ini = max(0, n - nuevos - (ancho - 1))
    c = np.concatenate([[0.0], np.cumsum(z[ini:])])
    idx = np.arange(n - nuevos, n)
    ok = idx >= ancho - 1
    loc = idx[ok] - ini
    m = np.full(nuevos, np.nan)
    m[ok] = (c[loc + 1] - c[loc + 1 - ancho]) / ancho
    return lufs(m)


def _f(x):
    return float(x) if x is not None else NAN


class Seguidor:
    """Recibe bloques de 100 ms (energía, picos) y avisos de cambio de canción; mantiene las
    canciones cerradas, los cambios entre ellas y la canción en curso."""

    def __init__(self, min_dur=30.0, refinar_s=3.0, umbral_salto=3.0):
        self.min_dur = min_dur
        self.ventana = int(round(refinar_s * 10))
        self.umbral = umbral_salto
        self.z, self.sp, self.tp = Serie(), Serie(), Serie()
        self.mom, self.st = Serie(), Serie()
        self.actual = None       # canción en curso (desde su corte ya ajustado)
        self.pendiente = None    # aviso de cambio esperando unos segundos para ajustar el corte
        self.cerradas = []
        self.transiciones = []
        self.ultima = None       # última canción cerrada, para medir el salto con la siguiente
        self.nuevas = []         # canciones cerradas que aún no se informan a la ventana
        self.bloques_musica = 0
        self.silencio_bloques = 0
        self.tp_max = 0.0
        self.saltos_notorios = []  # saltos sobre el umbral que aún no se informan (ver _revisar_salto)
        self.min_bloques = max(100, math.ceil(min_dur * 10))
        self.piso = 0              # bloque de la última pausa: el ajuste de un corte no puede pasar de ahí
        self.tras_pausa = False    # la próxima canción empieza al reanudar: llega empezada, no desde su inicio

    @property
    def n(self):
        return len(self.z)

    # ------------------------------------------------------------------ entrada
    def agregar(self, z, sp, tp):
        k = len(z)
        if not k:
            return
        self.z.agregar(z)
        self.sp.agregar(sp)
        self.tp.agregar(tp)
        self.mom.agregar(_ventana_lufs(self.z.v, k, 4))
        self.st.agregar(_ventana_lufs(self.z.v, k, 30))
        musica = self.st.v[-k:] > SILENCIO
        self.bloques_musica += int(musica.sum())
        if musica.any():
            self.silencio_bloques = k - 1 - int(np.nonzero(musica)[0][-1])
        else:
            self.silencio_bloques += k
        self.tp_max = max(self.tp_max, float(np.max(tp)))
        if self.pendiente is not None and self.n >= self.pendiente["limite"]:
            self._resolver_pendiente()
        self._revisar_salto()

    def evento(self, e, b):
        """Un aviso de la metadata en el bloque b (misma lógica que analizar.segmentar)."""
        b = max(0, min(int(b), self.n))
        if e.get("evento") == "pausa":
            # La grabación se pausó: lo que sigue no continúa lo anterior. La canción en curso se cierra
            # acá y no se calcula ningún salto entre la de antes y la de después.
            if self.pendiente is not None:
                self._resolver_pendiente()
            self._cortar(b, None, False)
            self.ultima = None
            self.piso = b
            self.tras_pausa = True
        elif e.get("evento") == "cambio_pista" and e.get("titulo"):
            if self.pendiente is not None:
                self._resolver_pendiente()
            self.pendiente = {"b": b, "e": e, "limite": b + self.ventana, "tras_pausa": self.tras_pausa}
            self.tras_pausa = False
        elif e.get("evento") in ("cambio_estado", "sin_sesion") and e.get("estado") != "reproduciendo":
            if self.pendiente is not None:
                self._resolver_pendiente()
            self._cortar(b, None, False)
            self.tras_pausa = False

    def terminar(self):
        """Cierra lo que esté abierto al final de la sesión (la última canción queda incompleta,
        igual que en el análisis)."""
        if self.pendiente is not None:
            self._resolver_pendiente()
        self._cortar(self.n, None, False)

    # ------------------------------------------------------------------ cortes
    def _resolver_pendiente(self):
        p, self.pendiente = self.pendiente, None
        if self.ventana:
            b, movido = refinar_corte(self.mom.v, p["b"], self.ventana, desde=self.piso)
        else:
            b, movido = p["b"], False
        self._cortar(b, p["e"], movido, p.get("tras_pausa", False))

    def _revisar_salto(self):
        """Calcula el salto de la canción en curso con la anterior apenas cumple la duración mínima y,
        si pasa el umbral, lo deja en saltos_notorios (una vez por canción). Se espera a la duración
        mínima, y no solo a los 10 s medidos, para coincidir con las estadísticas: una canción más
        corta no cuenta como cambio, y entonces tampoco debe dejar un recorte."""
        a = self.actual
        if a is None or a.get("salto_revisado") or self.n - a["ini"] < self.min_bloques:
            return
        a["salto_revisado"] = True
        prev = self.ultima
        if prev is None or prev["corta"] or a["ini"] / 10 - (prev["inicio_s"] + prev["duracion_s"]) > 1:
            return
        primeros = integrado(self.z.v, a["ini"], a["ini"] + 100)
        salto = primeros - prev["ultimos_10s"]
        if not math.isnan(salto) and abs(salto) > self.umbral:
            self.saltos_notorios.append({
                "corte": a["ini"], "salto": salto, "ultimos_10s": prev["ultimos_10s"], "primeros_10s": primeros,
                "de": f"{prev['artista']} - {prev['titulo']}", "a": f"{a['artista']} - {a['titulo']}",
                "corte_ajustado": a["corte_ajustado"],
            })

    def _cortar(self, b, e, movido, tras_pausa=False):
        if self.actual is not None and b > self.actual["ini"]:
            self._cerrar(self.actual, b)
        if e is None:
            self.actual = None
        else:
            self.actual = {"ini": b, "artista": e.get("artista") or "", "titulo": e.get("titulo") or "",
                           "album": e.get("album") or "", "app": e.get("app") or "",
                           "corte_ajustado": movido, "tras_pausa": tras_pausa}

    def _cerrar(self, pista, fin):
        a, b = pista["ini"], fin
        z = self.z.v
        dur = (b - a) / 10
        p = {
            "inicio_s": a / 10, "duracion_s": dur,
            "artista": pista["artista"], "titulo": pista["titulo"], "album": pista["album"],
            "app": pista["app"], "corte_ajustado": pista["corte_ajustado"],
            "tras_pausa": pista.get("tras_pausa", False),
            "lufs": integrado(z, a, b), "lra": lra(z, a, b),
            "tp": float(db(self.tp.v[a:b].max())), "sp": float(db(self.sp.v[a:b].max())),
            "primeros_10s": integrado(z, a, min(b, a + 100)),
            "ultimos_10s": integrado(z, max(a, b - 100), b),
            "corta": dur < self.min_dur, "salto": NAN,
        }
        prev = self.ultima
        if self._contiguas(prev, p):
            salto = p["primeros_10s"] - prev["ultimos_10s"]
            p["salto"] = salto
            if not math.isnan(salto):
                self.transiciones.append({
                    "t_s": p["inicio_s"], "salto": salto, "salto_integrado": p["lufs"] - prev["lufs"],
                    "de": f"{prev['artista']} - {prev['titulo']}", "a": f"{p['artista']} - {p['titulo']}",
                })
        self.cerradas.append(p)
        self.nuevas.append(p)
        self.ultima = p
        return p

    @staticmethod
    def _contiguas(prev, p):
        """Mismas condiciones que analizar.py para contar un cambio de canción: ninguna de las dos
        es corta y no hubo una pausa de más de 1 s entre ellas."""
        return (prev is not None and not prev["corta"] and not p["corta"]
                and p["inicio_s"] - (prev["inicio_s"] + prev["duracion_s"]) <= 1)

    # ------------------------------------------------------------------ consultas
    def momentary(self):
        return _f(self.mom.v[-1]) if self.n else NAN

    def shortterm(self):
        return _f(self.st.v[-1]) if self.n else NAN

    def pista_actual(self):
        """La canción que suena. Si acaba de cambiar, se muestra la nueva aunque su corte aún
        espere el ajuste (unos segundos), para que la pantalla no quede atrasada."""
        if self.pendiente is not None:
            e = self.pendiente["e"]
            base = {"ini": self.pendiente["b"], "artista": e.get("artista") or "", "titulo": e.get("titulo") or "",
                    "album": e.get("album") or "", "app": e.get("app") or ""}
        elif self.actual is not None:
            base = self.actual
        else:
            return None
        a, n = base["ini"], self.n
        z = self.z.v
        d = {k: base[k] for k in ("artista", "titulo", "album", "app")}
        d["inicio_s"] = a / 10
        d["t_s"] = max(0, n - a) / 10
        d["lufs"] = integrado(z, a, n) if n - a >= 4 else NAN
        d["tp"] = float(db(self.tp.v[a:n].max())) if n > a else NAN
        d["salto"] = NAN
        prev = self.ultima
        if n - a >= 100 and prev is not None and not prev["corta"] \
                and a / 10 - (prev["inicio_s"] + prev["duracion_s"]) <= 1:
            d["salto"] = integrado(z, a, a + 100) - prev["ultimos_10s"]
        return d

    def estadisticas(self):
        li = np.array([p["lufs"] for p in self.cerradas if not p["corta"]], dtype=float)
        li = li[~np.isnan(li)]
        sal = np.abs(np.array([t["salto"] for t in self.transiciones], dtype=float))
        horas = self.bloques_musica / 36000
        med = float(np.median(li)) if len(li) else NAN
        sobre = int((sal > self.umbral).sum())
        return {
            "canciones": int(len(li)),
            "media": float(li.mean()) if len(li) else NAN,
            "mediana": med,
            "desv": float(li.std(ddof=1)) if len(li) > 1 else NAN,
            "min": float(li.min()) if len(li) else NAN,
            "max": float(li.max()) if len(li) else NAN,
            "pct_2lu": float(np.mean(np.abs(li - med) <= 2) * 100) if len(li) else NAN,
            "transiciones": int(len(sal)),
            "salto_medio": float(sal.mean()) if len(sal) else NAN,
            "salto_p95": float(np.percentile(sal, 95)) if len(sal) else NAN,
            "salto_max": float(sal.max()) if len(sal) else NAN,
            "sobre_umbral": sobre,
            "umbral": self.umbral,
            "por_hora": sobre / horas if horas >= 0.05 else NAN,
            "horas_musica": horas,
            "tp_max": float(db(self.tp_max)) if self.n else NAN,
        }
