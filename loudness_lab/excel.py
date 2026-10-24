"""«Descargar resultados»: un libro de Excel con lo que muestra la pestaña En vivo.

Cinco hojas: las canciones, los promedios de la sesión, los datos del gráfico de LUFS, el registro y el
espectro por canción (la capa de frecuencias, si la sesión la tiene).
No usa Qt: recibe los datos ya armados, así que se puede correr en otro hilo.
"""

import datetime as dt
import math
import time

import numpy as np
import xlsxwriter

from . import bandas
from .bs1770 import SILENCIO

HOJAS = ("Últimas canciones", "Promedios de la sesión", "Data LUFS", "Registro", "Espectro por canción")
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
              los campos de la ventana (artista, titulo, duracion_s, lufs, lra, tp, salto, corta, tras_pausa)
            y, si la sesión tiene capa de frecuencias, espectro, reparto y desajuste_db.
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
        for c, ancho in enumerate((19, 70, 8, 8, 8, 10, 8, 8, 10, 11, 10, 11, 8, 30, 30, 40, 36)):
            h1.set_column(c, c, ancho)
        h1.set_row(0, 34)
        # Artista, Título y Álbum por separado al final: sirven para armar la misma lista en otra app
        for c, t in enumerate(("Inicio", "Artista – Título", "Dur.", "LUFS", "LRA", "True peak", "Salto", "ΔI",
                               "Salto de cuerpo", "Subida de arranque", "Silencio antes (s)", "LUFS percibido",
                               "LN (phon)", "Nota", "Artista", "Título", "Álbum")):
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
            _num(h1, i, 7, p.get("delta_i"), f_s1)
            _num(h1, i, 8, p.get("salto_cuerpo"), f_s1)
            _num(h1, i, 9, p.get("subida_arranque"), f_s1)
            _num(h1, i, 10, p.get("silencio_previo_s"), f_1)
            _num(h1, i, 11, p.get("lufs_percibido"), f_1)
            _num(h1, i, 12, p.get("ln"), f_1)
            nota = ("corta: no cuenta" if p.get("corta") else "en un tramo excluido: no cuenta" if p.get("excluida")
                    else "empezó tras una pausa o un corte" if p.get("tras_pausa") else "parcial: no cuenta" if p.get("parcial")
                    else "")
            if p.get("corte_incierto"):
                nota = (nota + " · " if nota else "") + "corte incierto"
            if nota:
                h1.write_string(i, 13, nota, f_gris)
            for c, k in ((14, "artista"), (15, "titulo"), (16, "album")):
                if p.get(k):
                    h1.write_string(i, c, str(p[k]), f_txt)
        if pistas:
            h1.autofilter(0, 0, len(pistas), 16)
            # lo mismo que marca la tabla: salto sobre el umbral (amarillo), sobre 5 LU (rojo), true peak sobre −1
            h1.conditional_format(1, 6, len(pistas), 6, {"type": "cell", "criteria": "not between",
                                  "minimum": -5, "maximum": 5, "format": rojo, "stop_if_true": True})
            h1.conditional_format(1, 6, len(pistas), 6, {"type": "cell", "criteria": "not between",
                                  "minimum": -umbral, "maximum": umbral, "format": amarillo})
            h1.conditional_format(1, 5, len(pistas), 5, {"type": "cell", "criteria": ">", "value": -1, "format": pico})
            # salto de cuerpo sobre 3 LU (amarillo) y sobre 5 (rojo), como en la ventana
            h1.conditional_format(1, 8, len(pistas), 8, {"type": "cell", "criteria": "not between",
                                  "minimum": -5, "maximum": 5, "format": rojo, "stop_if_true": True})
            h1.conditional_format(1, 8, len(pistas), 8, {"type": "cell", "criteria": "not between",
                                  "minimum": -3, "maximum": 3, "format": amarillo})

        # ---- hoja 2: promedios de la sesión
        s = stats or {}
        h2 = libro.add_worksheet(HOJAS[1])
        h2.set_column(0, 0, 44)
        h2.set_column(1, 1, 22)
        h2.set_column(2, 2, 12)
        h2.set_column(3, 4, 14)
        for c, t in enumerate(("Concepto", "Valor", "Unidad", "IC 95 % inferior", "IC 95 % superior")):
            h2.write_string(0, c, t, enc)
        ic = s.get("ic") or {}
        filas = [
            ("Sesión", nombre, "", None),
            ("Inicio", _fecha(inicio_epoch) if inicio_epoch else None, "", f_fecha),
            ("Horas grabadas", horas, "h", f_2),
            None,
            ("Canciones válidas (las que cuentan)", s.get("canciones"), "", f_0),
            ("Canciones distintas (sin repetidas)", s.get("unicas"), "", f_0),
            ("Parciales (no cuentan: primera, última, pegadas a pausas o cortes)", s.get("parciales"), "", f_0),
            ("Cortas (no cuentan)", s.get("cortas"), "", f_0),
            ("En tramos excluidos (no cuentan)", s.get("excluidas"), "", f_0),
            ("LUFS por canción: media", s.get("media"), "LUFS", f_1),
            ("LUFS por canción: mediana", s.get("mediana"), "LUFS", f_1),
            ("Dispersión: desviación estándar", s.get("desv"), "LU", f_2, "desv"),
            ("Dispersión: mínimo", s.get("min"), "LUFS", f_1),
            ("Dispersión: máximo", s.get("max"), "LUFS", f_1),
            ("A ±0,5 LU de la mediana", s.get("pct_05"), "%", f_1, "pct_05"),
            ("A ±1 LU de la mediana", s.get("pct_1"), "%", f_1, "pct_1"),
            ("Cambios de canción válidos", s.get("cambios"), "", f_0),
            ("|ΔI| > 1 LU (integrado de la nueva − el de la anterior)", s.get("pct_dI_1"), "%", f_1, "pct_dI_1"),
            ("|ΔI| > 3 LU", s.get("pct_dI_3"), "%", f_1, "pct_dI_3"),
            ("Salto de cuerpo > 3 LU (mediana ST de la nueva − la del final de la anterior)", s.get("pct_cuerpo_3"), "%",
             f_1, "pct_cuerpo_3"),
            ("Salto de cuerpo: p95 (valor absoluto)", s.get("cuerpo_p95"), "LU", f_1),
            ("Subida de arranque > 5 LU (máx ST de los 3 a 15 s − el final de la anterior)", s.get("pct_arranque_5"), "%", f_1),
            ("Subida de arranque: p95", s.get("p95_arranque"), "LU", f_1),
            ("Canciones con short-term máximo > +5 LU sobre la mediana", s.get("pct_stmax_5"), "%", f_1),
            ("Cambios con corte incierto (radio)", s.get("inciertos"), "", f_0),
            ("Silencio entre canciones: mediana", s.get("silencio_mediana_s"), "s", f_1),
            ("Silencio entre canciones > 2 s", s.get("pct_silencio_2"), "%", f_1),
            ("Salto en el borde: medio", s.get("salto_medio"), "LU", f_1),
            ("Salto en el borde: p95", s.get("salto_p95"), "LU", f_1),
            ("Salto en el borde: máximo", s.get("salto_max"), "LU", f_1),
            (f"Saltos en el borde sobre {umbral:g} LU", s.get("sobre_umbral"), "", f_0),
            ("Saltos en el borde sobre el umbral, por hora", s.get("por_hora"), "por hora", f_1),
            ("Canciones con true peak > 0 dBTP", s.get("pct_tp_0"), "%", f_1),
            ("True peak máximo", s.get("tp_max"), "dBTP", f_1),
            ("Horas con música", s.get("horas_musica"), "h", f_2),
            ("Horas fuera de tramos excluidos", s.get("horas_validas"), "h", f_2),
            None,
            ("Canciones válidas con sonoridad percibida (Zwicker)", s.get("con_sonoridad"), "", f_0),
            ("Sonoridad percibida: desviación entre canciones (escala LUFS)", s.get("percibido_desv"), "LU", f_2,
             "percibido_desv"),
            ("LUFS de esas mismas canciones: desviación", s.get("lufs_desv_con_sonoridad"), "LU", f_2),
            ("Sonoridad percibida − LUFS: mediana", s.get("dif_percibido_mediana"), "LU", f_s1),
            ("LN (nivel de sonoridad del N5): mediana", s.get("ln_mediana"), "phon", f_1),
            ("LN: desviación entre canciones", s.get("ln_desv"), "phon", f_2),
        ]
        fila = 1
        for item in filas:
            if item is None:
                fila += 1
                continue
            concepto, valor, unidad, fmt = item[:4]
            h2.write_string(fila, 0, concepto, f_neg if concepto == "Sesión" else f_txt)
            if isinstance(valor, str):
                h2.write_string(fila, 1, valor, f_txt)
            else:
                _num(h2, fila, 1, valor, fmt)
            if unidad:
                h2.write_string(fila, 2, unidad, f_gris)
            if len(item) > 4 and ic.get(item[4]):
                _num(h2, fila, 3, ic[item[4]][0], fmt)
                _num(h2, fila, 4, ic[item[4]][1], fmt)
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

        # ---- hoja 5: la capa de frecuencias, una fila por canción (energía por tercio de octava, en dB)
        h5 = libro.add_worksheet(HOJAS[4])
        con = [(i, p) for i, p in enumerate(pistas) if p.get("espectro")]
        regiones = list(bandas.REGIONES)
        cols = (["Inicio", "Artista – Título", "Válida"] + [f"{bandas.NOMBRES_REGIONES[k]} (%)" for k in regiones]
                + ["Desajuste LUFS–oído (dB)", "LUFS", "LUFS percibido", "LN (phon)", "N5 (sone)", "N mediana (sone)"]
                + [f"{f / 1000:g} kHz" if f >= 1000 else f"{f:g} Hz" for f in bandas.NOMINALES])
        B0 = 13      # primera columna de bandas
        h5.freeze_panes(1, 2)
        h5.set_row(0, 34)
        h5.set_column(0, 0, 19)
        h5.set_column(1, 1, 60)
        h5.set_column(2, 2, 8)
        h5.set_column(3, 7, 12)
        h5.set_column(8, 12, 10)
        h5.set_column(B0, B0 + bandas.N_BANDAS - 1, 8)
        for c, t in enumerate(cols):
            h5.write_string(0, c, t, enc)
        f_pct = libro.add_format({**base, "num_format": "0"})
        for r, (i, p) in enumerate(con, start=1):
            h5.write_number(r, 0, float(inicios[i]), f_fecha)
            h5.write_string(r, 1, f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}", f_txt)
            h5.write_string(r, 2, "sí" if p.get("valida", p.get("por_ahora_valida", not p.get("corta"))) else "no", f_txt)
            rep_ = p.get("reparto") or {}
            for c, k in enumerate(regiones, start=3):
                _num(h5, r, c, rep_.get(k), f_pct)
            _num(h5, r, 7, p.get("desajuste_db"), f_s1)
            _num(h5, r, 8, p.get("lufs"), f_1)
            _num(h5, r, 9, p.get("lufs_percibido"), f_1)
            _num(h5, r, 10, p.get("ln"), f_1)
            _num(h5, r, 11, p.get("n5"), f_2)
            _num(h5, r, 12, p.get("n_mediana"), f_2)
            e = np.asarray(p["espectro"], dtype=float)
            for c, v in enumerate(10 * np.log10(np.maximum(e, 1e-30)), start=B0):
                _num(h5, r, c, v, f_1)
        if con:
            h5.autofilter(0, 0, len(con), len(cols) - 1)
            fila = len(con) + 2
        else:
            fila = 1
        notas = ["Bandas: energía media por tercio de octava en los bloques con música de la canción, en dB relativos a "
                 "escala completa (sin ponderar).",
                 "Desajuste LUFS–oído: + la canción suena más de lo que dice su LUFS a volumen de música de fondo "
                 f"({bandas.PHON_ESCUCHA:g} phon, ISO 226); − suena menos. Ruido rosa = 0. Sirve para ordenar canciones, "
                 "no como valor absoluto.",
                 "Sonoridad percibida: Zwicker (ISO 532-1 estacionario) un valor por segundo; N5 = sonoridad superada el "
                 "5 % del tiempo con música; LN = su nivel de sonoridad; LUFS percibido = el LUFS al que un ruido rosa "
                 "sonaría igual de fuerte (calibración en sesion.json → sonoridad; por defecto −14 LUFS = 65 dB SPL)."]
        if not con:
            notas.insert(0, "Esta sesión no tiene capa de frecuencias: se genera desde su audio en «Sesiones… → "
                            "Frecuencias y archivo…».")
        for t in notas:
            h5.write_string(fila, 1, t, f_gris)
            fila += 1
        escrito = True
    finally:
        if escrito:   # si algo falló antes, el archivo de destino queda como estaba
            libro.close()
    return {"canciones": len(pistas), "puntos": len(epoch), "celdas_registro": max(1, len(partes)),
            "submuestreo": paso, "con_espectro": len(con)}
