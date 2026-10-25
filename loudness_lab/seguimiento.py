"""Seguimiento en vivo: arma las canciones a partir de los avisos de la metadata y lleva las
estadísticas de la sesión mientras se graba.

Replica la segmentación de analizar.py (mismo ajuste del corte al silencio real, mismas
exclusiones de canciones cortas y de cambios con pausa) para que lo que se ve en vivo coincida
con el informe final. Lo único que no replica es unir dos trozos de una misma canción separados
por un parpadeo de la metadata: eso lo resuelve el análisis final.
"""

import math

import numpy as np

from . import estudio
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
    canciones cerradas, los cambios entre ellas y la canción en curso.

    Las estadísticas siguen las definiciones del estudio (estudio.py): solo cuentan las canciones
    válidas (no cortas, no parciales, fuera de los tramos excluidos). `tramos`: [{desde_s, hasta_s,
    motivo}] en segundos de audio. `nivel_aplica`: la alerta de cambio de nivel (no aplica a una radio,
    que no normaliza)."""

    def __init__(self, min_dur=30.0, refinar_s=3.0, umbral_salto=3.0, tramos=None, nivel_aplica=True,
                 marcar_inciertos=True, bandas_de=None, sr=48000, sonoridad=None):
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
        self.tramos = list(tramos or [])
        self.terminada = False     # la última canción solo es parcial cuando la sesión termina
        self.marcar_inciertos = marcar_inciertos
        self.alerta = estudio.AlertaEstado(nivel_aplica)
        self.alertas_nuevas = []   # alertas de cambio de estado que aún no se informan (ver AlertaEstado)
        self._ic = (None, None)    # intervalos de confianza: (clave de lo que se usó, valores)
        self.bandas_de = bandas_de  # (a, b) → filas de bandas de esos bloques (espectro.f32), si la sesión las tiene
        self.sonoridad = sonoridad  # sonoridad.Sonoridad de la sesión (desde espectro.f32), si la tiene
        self.cambios_nuevos = []    # cambios medidos que aún no se informan (para completar picos.csv)
        self.sr = sr

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
        if e.get("evento") in ("pausa", "corte"):
            # La grabación se pausó (o el motor se cortó y se retomó): lo que sigue no continúa lo
            # anterior. La canción en curso se cierra acá y no se calcula ningún salto entre la de
            # antes y la de después.
            if self.pendiente is not None:
                self._resolver_pendiente()
            self._cortar(b, None, False)
            if self.cerradas:     # la canción que se cortó acá (o la última antes) se va empezada: parcial
                self.cerradas[-1]["seguida_de_pausa"] = True
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
        self.terminada = True

    # ------------------------------------------------------------------ cortes
    def _resolver_pendiente(self):
        p, self.pendiente = self.pendiente, None
        if self.ventana:
            b, movido = refinar_corte(self.mom.v, p["b"], self.ventana, desde=self.piso)
        else:
            b, movido = p["b"], False
        # Con una radio web, si el ajuste fino no encontró un hueco, el corte quedó donde llegó el título
        # (más el ajuste de la fuente): puede estar corrido unos segundos.
        incierto = self.marcar_inciertos and not movido and (p["e"].get("app") or "").startswith("radio:")
        self._cortar(b, p["e"], movido, p.get("tras_pausa", False), incierto)

    def _revisar_salto(self):
        """Calcula el salto de la canción en curso con la anterior apenas cumple la duración mínima y,
        si pasa el umbral, lo deja en saltos_notorios (una vez por canción). Se espera a la duración
        mínima, y no solo a los 10 s medidos, para coincidir con las estadísticas: una canción más
        corta no cuenta como cambio, y entonces tampoco debe dejar un recorte."""
        a = self.actual
        if a is None or a.get("salto_revisado") or self.n - a["ini"] < self.min_bloques:
            return
        a["salto_revisado"] = True
        if a.get("corte_incierto"):    # con el corte incierto, el recorte no mostraría el cambio real
            return
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

    def _cortar(self, b, e, movido, tras_pausa=False, incierto=False):
        if self.actual is not None and b > self.actual["ini"]:
            self._cerrar(self.actual, b)
        if e is None:
            self.actual = None
        else:
            self.actual = {"ini": b, "artista": e.get("artista") or "", "titulo": e.get("titulo") or "",
                           "album": e.get("album") or "", "app": e.get("app") or "",
                           "corte_ajustado": movido, "tras_pausa": tras_pausa, "corte_incierto": incierto}

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
            "corte_incierto": pista.get("corte_incierto", False), "seguida_de_pausa": False,
        }
        p.update(estudio.medir_cancion(p, self.st.v, self.mom.v))
        if self.bandas_de is not None and b > a:
            from .bandas import espectro_cancion
            esp = espectro_cancion(self.bandas_de(a, b), self.st.v[a:b], sr=self.sr,
                                   **({"phon": self.sonoridad.phon_indice} if self.sonoridad is not None else {}))
            if esp:
                p.update(esp)
        if self.sonoridad is not None and b > a:
            son = self.sonoridad.cancion(a, b, self.st.v)
            if son:
                p.update(son)
        prev = self.ultima
        i = len(self.cerradas)
        # por ahora válida: lo que se sabe al cerrarla (si después viene una pausa, pasa a parcial)
        p["por_ahora_valida"] = not (p["corta"] or i == 0 or p["tras_pausa"] or math.isnan(p["lufs"])
                                     or estudio.excluida(p["inicio_s"], p["inicio_s"] + dur, self.tramos))
        t = estudio.medir_cambio(prev, p, self.st.v) if prev is not None else None
        if t is not None:
            # (mismas condiciones que analizar.py: ninguna corta y no más de 1 s entre ellas)
            p["salto"] = t["salto_borde"]
            p["delta_i"] = t["delta_i"]
            p["salto_cuerpo"] = t["salto_cuerpo"]
            # el arranque y el silencio dependen de dónde cae el corte: no en un corte incierto
            p["subida_arranque"] = NAN if t["incierto"] else t["subida_arranque"]
            p["silencio_previo_s"] = NAN if t["incierto"] else t["silencio_s"]
            t.update(salto=t["salto_borde"], salto_integrado=t["delta_i"], i_de=i - 1, i_a=i)
            self.transiciones.append(t)
            self.cambios_nuevos.append(t)
        self.cerradas.append(p)
        self.nuevas.append(p)
        self.ultima = p
        if p["por_ahora_valida"]:
            self._alertas(self.alerta.cancion(p), p)
            if t is not None and prev.get("por_ahora_valida") and not prev.get("seguida_de_pausa"):
                self._alertas(self.alerta.cambio(prev, p, self.mom.v), p)
        return p

    def _alertas(self, eventos, p):
        for e in eventos:
            e["b"] = p["a"]
            e["t_s"] = p["inicio_s"]
            self.alertas_nuevas.append(e)

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

    def estadisticas(self, intervalos=True):
        """Promedios de la sesión con las definiciones del estudio: solo canciones válidas y cambios
        entre canciones válidas. Los saltos en el borde y el silencio no cuentan los cortes inciertos."""
        P = estudio.marcar(self.cerradas, self.tramos, self.terminada)
        T = estudio.marcar_cambios(self.transiciones, P)
        V = [p for p in P if p["valida"]]
        li, med = estudio.niveles(P)
        TV = [t for t in T if t["valida"]]
        seguros = [t for t in TV if not t.get("incierto")]
        sal = np.abs(estudio.limpio([t["salto_borde"] for t in seguros]))
        dI = estudio.limpio([t["delta_i"] for t in TV])
        gap = estudio.limpio([t["silencio_s"] for t in seguros])
        horas = self.bloques_musica / 36000
        sobre = int((sal > self.umbral).sum())
        unicas = len({estudio.clave(p) for p in V})
        clave_ic = (len(li), round(med, 9) if len(li) else None, len(dI),
                    sum(1 for p in V if p.get("lufs_percibido") is not None))
        if intervalos and self._ic[0] != clave_ic:
            self._ic = (clave_ic, estudio.intervalos(P, T))
        bloques_excl = sum(max(0, min(self.n, int(t["hasta_s"] * 10)) - max(0, int(t["desde_s"] * 10)))
                           for t in self.tramos)
        return {
            "canciones": int(len(li)),
            "media": float(li.mean()) if len(li) else NAN,
            "mediana": med,
            "desv": float(li.std(ddof=1)) if len(li) > 1 else NAN,
            "min": float(li.min()) if len(li) else NAN,
            "max": float(li.max()) if len(li) else NAN,
            "pct_2lu": float(np.mean(np.abs(li - med) <= 2) * 100) if len(li) else NAN,
            "pct_05": estudio.pct(np.abs(li - med) <= 0.5) if len(li) else NAN,
            "pct_1": estudio.pct(np.abs(li - med) <= 1) if len(li) else NAN,
            "unicas": unicas,
            "repetidas": int(len(li)) - unicas,
            "parciales": sum(1 for p in P if p["parcial"] and not p["corta"]),
            "excluidas": sum(1 for p in P if p["excluida"] and not p["corta"]),
            "cortas": sum(1 for p in P if p["corta"]),
            "transiciones": int(len(sal)),
            "salto_medio": float(sal.mean()) if len(sal) else NAN,
            "salto_p95": float(np.percentile(sal, 95)) if len(sal) else NAN,
            "salto_max": float(sal.max()) if len(sal) else NAN,
            "sobre_umbral": sobre,
            "umbral": self.umbral,
            "por_hora": sobre / horas if horas >= 0.05 else NAN,
            "cambios": int(len(dI)),
            "pct_dI_1": estudio.pct(np.abs(dI) > 1) if len(dI) else NAN,
            "inciertos": sum(1 for t in TV if t.get("incierto")),
            "silencio_mediana_s": float(np.median(gap)) if len(gap) else NAN,
            "pct_tp_0": estudio.pct(np.array([p["tp"] for p in V]) > 0) if V else NAN,
            "ic": dict(self._ic[1]) if self._ic[1] else None,
            "horas_musica": horas,
            "horas_validas": (self.n - bloques_excl) / 36000,
            "tp_max": float(db(self.tp_max)) if self.n else NAN,
            "techo_cadena": estudio.techo_cadena(self.sp.v, P),
            **estudio.metricas_cambios(V, TV, med),
            **estudio.metricas_sonoridad(V),
            "nivel_escucha_db_spl": self.sonoridad.nivel if self.sonoridad is not None else NAN,
            "phon_indice": self.sonoridad.phon_indice if self.sonoridad is not None else NAN,
        }
