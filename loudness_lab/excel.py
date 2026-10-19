"""«Descargar resultados»: un libro de Excel con lo que muestra la pestaña En vivo.

Cuatro hojas: las canciones, los promedios de la sesión, los datos del gráfico de LUFS y el registro.
No usa Qt: recibe los datos ya armados, así que se puede correr en otro hilo.
"""

import datetime as dt
import math
import time

import numpy as np
import xlsxwriter

from .bs1770 import SILENCIO

HOJAS = ("Últimas canciones", "Promedios de la sesión", "Data LUFS", "Registro")
MAX_CELDA = 32000        # Excel no admite más de 32 767 caracteres en una celda
MAX_FILAS = 1_000_000    # y 1 048 576 filas por hoja
EPOCH_EXCEL = dt.datetime(1899, 12, 30)


def serie_excel(epoch):
    """Instantes (segundos desde 1970) → fecha y hora de Excel, en la hora local del PC."""
    e = np.asarray(epoch, dtype=float)
    if not len(e):
        return e
    ini, fin = time.localtime(e[0]).tm_gmtoff, time.localtime(e[-1]).tm_gmtoff
    if ini == fin:
        desfase = ini
    else:   # la sesión cruzó un cambio de hora
        desfase = np.array([time.localtime(t).tm_gmtoff for t in e], dtype=float)
    return (e + desfase) / 86400.0 + 25569.0


def _fecha(epoch):
    return float(serie_excel([epoch])[0])


def _trozos(texto, maximo=MAX_CELDA):
    """El texto en pedazos de a lo más `maximo` caracteres, cortados entre líneas."""
    out, actual, largo = [], [], 0
    for linea in texto.replace("\r\n", "\n").split("\n"):
        while len(linea) > maximo:   # una sola línea que no cabe: se parte
            if actual:
                out.append("\n".join(actual))
                actual, largo = [], 0
            out.append(linea[:maximo])
            linea = linea[maximo:]
        if actual and largo + 1 + len(linea) > maximo:
            out.append("\n".join(actual))
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + (1 if len(actual) > 1 else 0)
    if actual:
        out.append("\n".join(actual))
    return out


def _vacio(v):
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _num(hoja, fila, col, v, fmt=None):
    if _vacio(v):
        hoja.write_blank(fila, col, None, fmt)
    else:
        hoja.write_number(fila, col, float(v), fmt)


def exportar(ruta, nombre, inicio_epoch, horas, pistas, stats, curva, registro):
    """Escribe el libro en `ruta` y devuelve un resumen de lo escrito.

    pistas  lista de dicts de canciones cerradas, con `inicio_epoch` (instante del reloj) además de
            los campos de la ventana (artista, titulo, duracion_s, lufs, lra, tp, salto, corta, tras_pausa).
    stats   las estadísticas de la sesión (Seguidor.estadisticas), con `umbral`.
    curva   (epoch, segundo_de_audio, short_term, integrado_de_la_cancion): un punto por segundo.
    registro  el texto del registro.
    """
    libro = xlsxwriter.Workbook(str(ruta), {"constant_memory": True, "nan_inf_to_errors": False})
    escrito = False
    try:
        base = {"font_name": "Calibri", "font_size": 11}
        enc = libro.add_format({**base, "bold": True, "bg_color": "#2f6db5", "font_color": "#ffffff",
                                "valign": "vcenter", "text_wrap": True})
        f_fecha = libro.add_format({**base, "num_format": "dd-mm-yyyy hh:mm:ss"})
        f_dur = libro.add_format({**base, "num_format": "[m]:ss", "align": "right"})
        f_1 = libro.add_format({**base, "num_format": "0.0"})
        f_s1 = libro.add_format({**base, "num_format": "+0.0;-0.0;0.0"})
        f_2 = libro.add_format({**base, "num_format": "0.00"})
        f_0 = libro.add_format({**base, "num_format": "0"})
        f_txt = libro.add_format(base)
        f_neg = libro.add_format({**base, "bold": True})
        f_gris = libro.add_format({**base, "font_color": "#6b7280"})
        f_log = libro.add_format({"font_name": "Consolas", "font_size": 9, "text_wrap": True, "valign": "top"})
        amarillo = libro.add_format({"bg_color": "#fdf0d5"})
        rojo = libro.add_format({"bg_color": "#fde2e1"})
        pico = libro.add_format({"font_color": "#c0392b", "bold": True})

        umbral = float(stats.get("umbral") or 3.0) if stats else 3.0

        # ---- hoja 1: canciones (las mismas columnas de la tabla, de la primera a la última)
        h1 = libro.add_worksheet(HOJAS[0])
        h1.freeze_panes(1, 0)
        for c, ancho in enumerate((19, 70, 8, 8, 8, 10, 8, 30, 30, 40, 36)):
            h1.set_column(c, c, ancho)
        h1.set_row(0, 20)
        # Artista, Título y Álbum por separado al final: sirven para armar la misma lista en otra app
        for c, t in enumerate(("Inicio", "Artista – Título", "Dur.", "LUFS", "LRA", "True peak", "Salto", "Nota",
                               "Artista", "Título", "Álbum")):
            h1.write_string(0, c, t, enc)
        inicios = serie_excel([p["inicio_epoch"] for p in pistas])
        for i, p in enumerate(pistas, start=1):
            h1.write_number(i, 0, float(inicios[i - 1]), f_fecha)
            h1.write_string(i, 1, f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}", f_txt)
            h1.write_number(i, 2, float(p["duracion_s"]) / 86400.0, f_dur)
            _num(h1, i, 3, p.get("lufs"), f_1)
            _num(h1, i, 4, p.get("lra"), f_1)
            tp = p.get("tp")
            _num(h1, i, 5, None if not _vacio(tp) and tp <= -200 else tp, f_1)   # −240 = silencio digital
            _num(h1, i, 6, p.get("salto"), f_s1)
            nota = "corta: no cuenta" if p.get("corta") else ("empezó tras una pausa o un corte" if p.get("tras_pausa") else "")
            if nota:
                h1.write_string(i, 7, nota, f_gris)
            for c, k in ((8, "artista"), (9, "titulo"), (10, "album")):
                if p.get(k):
                    h1.write_string(i, c, str(p[k]), f_txt)
        if pistas:
            h1.autofilter(0, 0, len(pistas), 10)
            # lo mismo que marca la tabla: salto sobre el umbral (amarillo), sobre 5 LU (rojo), true peak sobre −1
            h1.conditional_format(1, 6, len(pistas), 6, {"type": "cell", "criteria": "not between",
                                  "minimum": -5, "maximum": 5, "format": rojo, "stop_if_true": True})
            h1.conditional_format(1, 6, len(pistas), 6, {"type": "cell", "criteria": "not between",
                                  "minimum": -umbral, "maximum": umbral, "format": amarillo})
            h1.conditional_format(1, 5, len(pistas), 5, {"type": "cell", "criteria": ">", "value": -1, "format": pico})

        # ---- hoja 2: promedios de la sesión
        s = stats or {}
        h2 = libro.add_worksheet(HOJAS[1])
        h2.set_column(0, 0, 40)
        h2.set_column(1, 1, 22)
        h2.set_column(2, 2, 12)
        for c, t in enumerate(("Concepto", "Valor", "Unidad")):
            h2.write_string(0, c, t, enc)
        filas = [
            ("Sesión", nombre, "", None),
            ("Inicio", _fecha(inicio_epoch) if inicio_epoch else None, "", f_fecha),
            ("Horas grabadas", horas, "h", f_2),
            None,
            ("Canciones medidas", s.get("canciones"), "", f_0),
            ("LUFS por canción: media", s.get("media"), "LUFS", f_1),
            ("LUFS por canción: mediana", s.get("mediana"), "LUFS", f_1),
            ("Dispersión: desviación estándar", s.get("desv"), "LU", f_1),
            ("Dispersión: mínimo", s.get("min"), "LUFS", f_1),
            ("Dispersión: máximo", s.get("max"), "LUFS", f_1),
            ("A ±2 LU de la mediana", s.get("pct_2lu"), "%", f_0),
            ("Cambios de canción", s.get("transiciones"), "", f_0),
            ("Salto medio entre canciones", s.get("salto_medio"), "LU", f_1),
            ("Salto p95", s.get("salto_p95"), "LU", f_1),
            ("Salto máximo", s.get("salto_max"), "LU", f_1),
            (f"Saltos notorios (sobre {umbral:g} LU)", s.get("sobre_umbral"), "", f_0),
            ("Umbral de salto notorio", s.get("umbral"), "LU", f_1),
            ("Saltos notorios por hora", s.get("por_hora"), "por hora", f_1),
            ("True peak máximo", s.get("tp_max"), "dBTP", f_1),
            ("Horas con música", s.get("horas_musica"), "h", f_2),
        ]
        fila = 1
        for item in filas:
            if item is None:
                fila += 1
                continue
            concepto, valor, unidad, fmt = item
            h2.write_string(fila, 0, concepto, f_neg if concepto == "Sesión" else f_txt)
            if isinstance(valor, str):
                h2.write_string(fila, 1, valor, f_txt)
            else:
                _num(h2, fila, 1, valor, fmt)
            if unidad:
                h2.write_string(fila, 2, unidad, f_gris)
            fila += 1

        # ---- hoja 3: los datos del gráfico, un punto por segundo de audio
        epoch, seg_audio, st, integrado = (np.asarray(a, dtype=float) for a in curva)
        paso = max(1, math.ceil(len(epoch) / MAX_FILAS))   # solo si la sesión pasa de ~12 días
        epoch, seg_audio, st, integrado = epoch[::paso], seg_audio[::paso], st[::paso], integrado[::paso]
        h3 = libro.add_worksheet(HOJAS[2])
        h3.freeze_panes(1, 0)
        h3.set_row(0, 34)
        for c, ancho in enumerate((19, 13, 17, 20)):
            h3.set_column(c, c, ancho)
        for c, t in enumerate(("Hora", "Segundo de audio", f"Short-term (LUFS, 3 s; {SILENCIO:g} = silencio)",
                               "Integrado de la canción (LUFS)")):
            h3.write_string(0, c, t, enc)
        horas_x = serie_excel(epoch)
        st = np.where(np.isnan(st), np.nan, np.maximum(st, SILENCIO))
        for i in range(len(epoch)):
            r = i + 1
            h3.write_number(r, 0, float(horas_x[i]), f_fecha)
            h3.write_number(r, 1, float(seg_audio[i]), f_0)
            _num(h3, r, 2, st[i], f_1)
            _num(h3, r, 3, integrado[i], f_1)
        if len(epoch):
            h3.autofilter(0, 0, len(epoch), 3)

        # ---- hoja 4: el registro completo en A1 (en A2, A3… si no cabe: una celda admite 32 767 caracteres)
        h4 = libro.add_worksheet(HOJAS[3])
        h4.set_column(0, 0, 140)
        partes = _trozos(registro or "")
        for i, parte in enumerate(partes):
            h4.write_string(i, 0, parte, f_log)
        if not partes:
            h4.write_string(0, 0, "", f_log)
        escrito = True
    finally:
        if escrito:   # si algo falló antes, el archivo de destino queda como estaba
            libro.close()
    return {"canciones": len(pistas), "puntos": len(epoch), "celdas_registro": max(1, len(partes)),
            "submuestreo": paso}
