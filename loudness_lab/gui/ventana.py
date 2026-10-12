"""Ventana de Loudness Lab.

La ventana no mide nada: lanza el motor en un proceso aparte, le habla por un socket local y
muestra lo que el motor publica. Por eso se puede cerrar sin cortar la grabación; al volver a
abrirla, se reconecta.
"""

import datetime as dt
import json
import math
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QScrollArea, QSlider, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

import pyqtgraph as pg

from .. import VERSION, protocolo, sesiones, sistema
from .. import config as cfgmod
from ..config import APP_POR_FUENTE
from ..seguimiento import Serie

ACENTO = "#2f6db5"
ROJO = "#c0392b"
AMARILLO = "#b7791f"
VERDE = "#2e7d32"
GRIS = "#6b7280"
MENOS = "−"


# ----------------------------------------------------------------------------
# Formato
# ----------------------------------------------------------------------------
def vacio(v):
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def num(v, dec=1, signo=False, unidad=""):
    if vacio(v):
        return "—"
    s = f"{v:+.{dec}f}" if signo else f"{v:.{dec}f}"
    s = s.replace("-", MENOS).replace(".", ",")
    return f"{s} {unidad}" if unidad else s


def lufs_txt(v, unidad="LUFS"):
    if vacio(v) or v < -70:
        return "silencio"
    return num(v, 1, unidad=unidad)


def hms(s):
    s = int(max(0, s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def mmss(s):
    s = int(max(0, s or 0))
    return f"{s // 60}:{s % 60:02d}"


def icono():
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(ACENTO))
    p.drawRoundedRect(8, 8, 240, 240, 48, 48)
    p.setBrush(QColor("white"))
    for i, h in enumerate((70, 130, 175, 110, 150)):
        x = 40 + i * 38
        p.drawRoundedRect(x, 208 - h, 26, h, 8, 8)
    p.end()
    return QIcon(pm)


class Tarea(QObject):
    """Corre una función lenta (dispositivos, Windows) en otro hilo y avisa al terminar."""

    listo = Signal(object)
    fallo = Signal(str)
    activas = set()

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def iniciar(self, al_listo, al_fallo=None):
        Tarea.activas.add(self)
        self.listo.connect(al_listo)
        if al_fallo:
            self.fallo.connect(al_fallo)
        self.listo.connect(lambda _: Tarea.activas.discard(self))
        self.fallo.connect(lambda _: Tarea.activas.discard(self))

        def correr():
            try:
                r = self.fn(*self.args)
            except Exception as e:
                self.fallo.emit(str(e))
                return
            self.listo.emit(r)

        self.hilo = threading.Thread(target=correr, daemon=True)
        self.hilo.start()

    @classmethod
    def esperar_todas(cls, limite_s=3.0):
        """Al cerrar, espera a que terminen las tareas en curso: cortarlas a mitad de código
        nativo (audio de Windows, metadata) puede tumbar el proceso al salir."""
        fin = time.time() + limite_s
        for t in list(cls.activas):
            h = getattr(t, "hilo", None)
            if h is not None:
                h.join(max(0.0, fin - time.time()))


# ----------------------------------------------------------------------------
# Medidor
# ----------------------------------------------------------------------------
class BarraLUFS(QWidget):
    MIN, MAX = -40.0, 0.0
    MARCAS = (-30, -23, -14, -10, -5)

    def __init__(self, titulo):
        super().__init__()
        self.titulo = titulo
        self.valor = float("nan")
        self.texto = None
        self.setMinimumHeight(40)

    def poner(self, v, texto=None):
        """`texto` reemplaza al valor cuando no se está midiendo (en pausa, sesión guardada)."""
        self.valor = v
        self.texto = texto
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        x0, x1 = 96, w - 110
        bh, y = 14, 6
        p.setPen(QColor("#374151"))
        p.drawText(0, y - 4, x0 - 10, bh + 8, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.titulo)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#e5e7eb"))
        p.drawRoundedRect(x0, y, x1 - x0, bh, 4, 4)
        v = self.valor
        if not vacio(v) and v > self.MIN:
            f = (min(v, self.MAX) - self.MIN) / (self.MAX - self.MIN)
            p.setBrush(QColor(ACENTO if v < -9 else (AMARILLO if v < -5 else ROJO)))
            p.drawRoundedRect(x0, y, max(4.0, (x1 - x0) * f), bh, 4, 4)
        fuente = QFont(p.font())
        fuente.setPointSizeF(max(6.5, fuente.pointSizeF() * 0.8))
        p.setFont(fuente)
        p.setPen(QPen(QColor("#9ca3af"), 1))
        for m in self.MARCAS:
            xm = x0 + (x1 - x0) * (m - self.MIN) / (self.MAX - self.MIN)
            p.drawLine(int(xm), y + bh, int(xm), y + bh + 3)
            p.drawText(int(xm) - 16, y + bh + 3, 32, 14, Qt.AlignmentFlag.AlignHCenter, str(m).replace("-", MENOS))
        grande = QFont(p.font())
        grande.setPointSizeF(fuente.pointSizeF() / 0.8 * 1.25)
        grande.setBold(True)
        p.setFont(grande)
        p.setPen(QColor("#111827"))
        p.drawText(x1 + 10, 0, 100, bh + 12, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.texto or lufs_txt(v))
        p.end()


# ----------------------------------------------------------------------------
# Pestaña: en vivo
# ----------------------------------------------------------------------------
COLUMNAS = ["Inicio", "Artista – Título", "Dur.", "LUFS", "LRA", "True peak", "Salto"]


class PestanaVivo(QWidget):
    def __init__(self):
        super().__init__()
        self.inicio_epoch = None
        self.umbral = 3.0
        self.rango = "1 h"
        self.curva_t, self.curva_st = Serie(3600), Serie(3600)
        self.seg_cortes, self.seg_niveles = [], []
        self.pausas = []   # [segundo de audio donde empezó, duración] de cada pausa de la grabación
        self.sucio = False

        self.lbl_cancion = QLabel("Sin grabación en curso")
        f = self.lbl_cancion.font()
        f.setPointSizeF(f.pointSizeF() * 1.45)
        f.setBold(True)
        self.lbl_cancion.setFont(f)
        self.lbl_detalle = QLabel("Configura la sesión y presiona «Iniciar grabación».")
        self.lbl_detalle.setStyleSheet(f"color:{GRIS}")
        self.bar_mom = BarraLUFS("Momentary")
        self.bar_st = BarraLUFS("Short-term")
        self.lbl_med = QLabel("")
        self.lbl_med.setStyleSheet("font-size: 10.5pt")

        arriba = QVBoxLayout()
        arriba.addWidget(self.lbl_cancion)
        arriba.addWidget(self.lbl_detalle)
        arriba.addSpacing(4)
        arriba.addWidget(self.bar_mom)
        arriba.addWidget(self.bar_st)
        arriba.addWidget(self.lbl_med)

        # gráfico
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setYRange(-45, 0, padding=0)
        self.plot.setLabel("left", "LUFS")
        self.plot.setMinimumHeight(170)
        self.cortes = self.plot.plot(pen=pg.mkPen((140, 140, 140, 110), width=1), connect="pairs")
        self.curva = self.plot.plot(pen=pg.mkPen("#4a78b5", width=1.2))
        self.curva.setDownsampling(auto=True, method="peak")
        self.curva.setClipToView(True)
        self.niveles = self.plot.plot(pen=pg.mkPen("#d1495b", width=2.2), connect="pairs")
        # la canción en curso: su nivel provisorio, punteado hasta que termine
        self.nivel_actual = self.plot.plot(pen=pg.mkPen("#d1495b", width=2.2, style=Qt.PenStyle.DashLine))
        self.pista_actual = None
        leyenda = QLabel("<span style='color:#4a78b5'>━</span> short-term (3 s) &nbsp; "
                         "<span style='color:#d1495b'>━</span> integrado de cada canción &nbsp; "
                         "<span style='color:#999'>│</span> cambio de canción &nbsp; "
                         "<span style='color:#999'>⋯</span> hueco = pausa")
        leyenda.setStyleSheet(f"color:{GRIS}")
        rangos = QHBoxLayout()
        rangos.addWidget(leyenda)
        rangos.addStretch()
        self.grupo_rango = QButtonGroup(self)
        for r in ("1 h", "6 h", "Toda"):
            b = QRadioButton(r)
            b.setChecked(r == self.rango)
            b.toggled.connect(lambda on, r=r: on and self._cambiar_rango(r))
            self.grupo_rango.addButton(b)
            rangos.addWidget(b)
        caja_plot = QVBoxLayout()
        caja_plot.setContentsMargins(0, 0, 0, 0)
        caja_plot.addWidget(self.plot)
        caja_plot.addLayout(rangos)
        w_plot = QWidget()
        w_plot.setLayout(caja_plot)

        # tabla de canciones
        self.tabla = QTableWidget(0, len(COLUMNAS))
        self.tabla.setHorizontalHeaderLabels(COLUMNAS)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabla.setAlternatingRowColors(True)
        hdr = self.tabla.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        caja_tabla = QGroupBox("Últimas canciones")
        lt = QVBoxLayout(caja_tabla)
        lt.addWidget(self.tabla)

        # promedios de la sesión
        self.stats = {}
        grid = QGridLayout()
        filas = [("canciones", "Canciones medidas"), ("lufs", "LUFS por canción"),
                 ("dispersion", "Dispersión"), ("pct", "A ±2 LU de la mediana"),
                 ("cambios", "Cambios de canción"), ("saltos", "Saltos notorios"),
                 ("tp", "True peak máximo"), ("horas", "Horas con música")]
        for i, (k, txt) in enumerate(filas):
            a = QLabel(txt)
            a.setStyleSheet(f"color:{GRIS}")
            b = QLabel("—")
            b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(a, i, 0)
            grid.addWidget(b, i, 1)
            self.stats[k] = b
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(len(filas), 1)
        caja_stats = QGroupBox("Promedios de la sesión")
        caja_stats.setLayout(grid)
        caja_stats.setMinimumWidth(330)

        medio = QSplitter(Qt.Orientation.Horizontal)
        medio.addWidget(caja_tabla)
        medio.addWidget(caja_stats)
        medio.setStretchFactor(0, 3)
        medio.setStretchFactor(1, 1)

        # salud y registro
        self.lbl_salud = QLabel("")
        self.lbl_salud.setStyleSheet(f"color:{GRIS}")
        self.lbl_salud.setWordWrap(True)
        self.registro = QPlainTextEdit()
        self.registro.setReadOnly(True)
        self.registro.setMaximumBlockCount(3000)
        self.registro.setFont(QFont("Consolas", 9))
        caja_log = QGroupBox("Registro")
        ll = QVBoxLayout(caja_log)
        ll.addWidget(self.lbl_salud)
        ll.addWidget(self.registro)

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(w_plot)
        vertical.addWidget(medio)
        vertical.addWidget(caja_log)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 3)
        vertical.setStretchFactor(2, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(arriba)
        lay.addWidget(vertical, 1)

    # ------------------------------------------------------------------ datos
    def _a_reloj(self, t, incluye_borde):
        """Instante del reloj (epoch) de uno o varios segundos de audio: el audio no incluye las
        pausas, así que se suma lo que duró cada una de las anteriores. Una pausa que empieza justo
        en t cuenta (incluye_borde) para el inicio de lo que viene después, y no para el último
        punto de antes."""
        t = np.asarray(t, dtype=float)
        if not self.pausas:
            return self.inicio_epoch + t
        ini = np.array([p[0] for p in self.pausas])
        acumulado = np.concatenate([[0.0], np.cumsum([p[1] for p in self.pausas])])
        return self.inicio_epoch + t + acumulado[np.searchsorted(ini, t, side="right" if incluye_borde else "left")]

    def poner_pausas(self, pausas):
        self.pausas = [list(p) for p in pausas or []]
        self.sucio = True

    def limpiar(self):
        """Deja la pestaña como al abrir la app, sin ninguna sesión."""
        self.reiniciar({})
        self.lbl_cancion.setText("Sin grabación en curso")
        self.lbl_detalle.setText("Configura la sesión y presiona «Iniciar grabación».")
        self.lbl_med.setText("")
        self.lbl_salud.setText("")
        self.bar_mom.poner(float("nan"), "—")
        self.bar_st.poner(float("nan"), "—")
        self.pista_actual = None
        self._poner_stats({})
        for item in (self.curva, self.cortes, self.niveles, self.nivel_actual):
            item.setData([], [])

    def mostrar_sesion(self, snap):
        """Una sesión guardada, reconstruida desde sus archivos (ver sesiones.reconstruir)."""
        self.reiniciar(snap)
        c = snap["cerrada"]
        inicio = dt.datetime.fromtimestamp(snap["inicio_epoch"]).strftime("%d-%m-%Y %H:%M")
        estado = "terminada" if c["terminada"] else "interrumpida (la grabación no se cerró bien)"
        self.lbl_cancion.setText(c["nombre"])
        detalle = (f"Sesión {estado} · empezó el {inicio} · {num(c['horas'], 2)} h grabadas · "
                   f"{c['canciones']} canciones medidas")
        if c["pausas"]:
            detalle += f" · {c['pausas']} pausa{'s' if c['pausas'] != 1 else ''} ({mmss(c['segundos_en_pausa'])} sin grabar)"
        self.lbl_detalle.setText(detalle)
        self.lbl_med.setText("")
        self.bar_mom.poner(float("nan"), "—")
        self.bar_st.poner(float("nan"), "—")
        self.pista_actual = None
        self._poner_stats(snap["estado"].get("sesion") or {})
        partes = [f"{c['dispositivo'] or '?'}", f"pérdidas {c['perdidas']}"]
        if c["app"]:
            partes.append(f"app {c['app']}")
        if c["motivo"]:
            partes.append(f"terminó: {c['motivo']}")
        self.lbl_salud.setText(" · ".join(partes))

    def reiniciar(self, snap):
        self.inicio_epoch = snap.get("inicio_epoch")
        self.pausas = [list(p) for p in snap.get("pausas") or []]
        cfg = snap.get("config") or {}
        self.umbral = float(cfg.get("umbral_salto", 3.0))
        self.curva_t, self.curva_st = Serie(3600), Serie(3600)
        self.curva_t.agregar(snap.get("curva_t", []))
        self.curva_st.agregar(snap.get("curva_st", []))
        self.seg_cortes, self.seg_niveles = [], []
        self.tabla.setRowCount(0)
        for p in snap.get("pistas", []):
            self.agregar_pista(p)
        self.registro.clear()
        for item in snap.get("log", []):
            self.agregar_log(item.get("linea", ""), item.get("nivel", "INFO"))
        self.sucio = True

    def agregar_puntos(self, puntos):
        if puntos:
            t, v = zip(*puntos)
            self.curva_t.agregar(t)
            self.curva_st.agregar(v)
            self.sucio = True

    def agregar_pista(self, p):
        if self.inicio_epoch is None:
            return
        x0 = float(self._a_reloj(p["inicio_s"], True))
        x1 = x0 + p["duracion_s"]
        self.seg_cortes += [(x0, -45.0), (x0, 0.0)]
        if not vacio(p["lufs"]):
            self.seg_niveles += [(x0, p["lufs"]), (x1, p["lufs"])]
        self.sucio = True

        self.tabla.insertRow(0)
        corta = p.get("corta")
        hora = dt.datetime.fromtimestamp(x0).strftime("%H:%M:%S")
        nombre = f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}"
        if corta:
            nombre += "  (corta: no cuenta)"
        elif p.get("tras_pausa"):
            nombre += "  (empezó tras una pausa)"
        valores = [hora, nombre, mmss(p["duracion_s"]), num(p["lufs"]), num(p["lra"]), num(p["tp"]),
                   num(p["salto"], signo=True)]
        for c, v in enumerate(valores):
            it = QTableWidgetItem(v)
            if c >= 2:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if corta:
                it.setForeground(QBrush(QColor("#9ca3af")))
            self.tabla.setItem(0, c, it)
        if not corta and not vacio(p["salto"]) and abs(p["salto"]) > self.umbral:
            it = self.tabla.item(0, 6)
            it.setBackground(QBrush(QColor("#fde2e1" if abs(p["salto"]) > 5 else "#fdf0d5")))
            it.setToolTip(f"Salto de más de {num(self.umbral)} LU respecto de la canción anterior")
        if not vacio(p["tp"]) and p["tp"] > -1:
            it = self.tabla.item(0, 5)
            it.setForeground(QBrush(QColor(ROJO)))
            it.setToolTip("Sobre −1 dBTP: riesgo de distorsión")
        if self.tabla.rowCount() > 1000:
            self.tabla.setRowCount(1000)

    def agregar_log(self, linea, nivel="INFO"):
        color = {"WARNING": AMARILLO, "ERROR": ROJO, "CRITICAL": ROJO}.get(nivel)
        if color:
            self.registro.appendHtml(f"<span style='color:{color}'>{_html(linea)}</span>")
        else:
            self.registro.appendPlainText(linea)

    def _poner_stats(self, s):
        if not s:
            for lbl in self.stats.values():
                lbl.setText("—")
            return
        self.umbral = s.get("umbral", self.umbral)
        self.stats["canciones"].setText(str(s.get("canciones", 0)))
        self.stats["lufs"].setText(f"media {num(s.get('media'))} · mediana {num(s.get('mediana'))}")
        self.stats["dispersion"].setText(f"desv. {num(s.get('desv'))} LU · rango {num(s.get('min'))} a {num(s.get('max'))}")
        self.stats["pct"].setText(num(s.get("pct_2lu"), 0, unidad="%"))
        self.stats["cambios"].setText(f"{s.get('transiciones', 0)} · salto medio {num(s.get('salto_medio'))} LU · "
                                      f"p95 {num(s.get('salto_p95'))} · máx {num(s.get('salto_max'))}")
        por_hora = "" if vacio(s.get("por_hora")) else f" ({num(s.get('por_hora'))} por hora)"
        self.stats["saltos"].setText(f"{s.get('sobre_umbral', 0)} sobre {num(s.get('umbral'), 0)} LU{por_hora}")
        self.stats["tp"].setText(num(s.get("tp_max"), unidad="dBTP"))
        self.stats["horas"].setText(num(s.get("horas_musica"), 2))

    def actualizar(self, e):
        p = e.get("pista")
        self.pista_actual = p
        pausado = bool(e.get("pausado"))
        if pausado:
            self.lbl_cancion.setText("Grabación en pausa")
            self.lbl_detalle.setText(f"Hace {mmss(e.get('pausa_s') or 0)}. El audio no se registra: lo que suene "
                                     "ahora no entra en la medición.")
            self.lbl_med.setText("")
        elif p:
            self.lbl_cancion.setText(f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}")
            app = p.get("app") or ""
            self.lbl_detalle.setText(f"{app} · {e.get('estado_app') or '—'} · {mmss(p['t_s'])} en esta canción")
            partes = [f"<b>Esta canción:</b> {lufs_txt(p['lufs'])}", f"true peak {num(p['tp'], unidad='dBTP')}"]
            if not vacio(p.get("salto")):
                color = ROJO if abs(p["salto"]) > self.umbral else "#111827"
                partes.append(f"salto <span style='color:{color}'><b>{num(p['salto'], signo=True, unidad='LU')}</b></span>")
            self.lbl_med.setText(" · ".join(partes))
        else:
            self.lbl_cancion.setText("Sin canción identificada")
            self.lbl_detalle.setText("La app elegida no está publicando qué suena (o la app de música está en pausa).")
            self.lbl_med.setText("")
        sin_medir = "—" if pausado else None
        self.bar_mom.poner(e.get("momentary"), sin_medir)
        self.bar_st.poner(e.get("shortterm"), sin_medir)

        s = e.get("sesion") or {}
        if s:
            self._poner_stats(s)

        h = e.get("salud") or {}
        partes = [f"{'En pausa' if pausado else 'Grabando'}: {h.get('dispositivo', '?')}",
                  f"disco {num(h.get('libre_gb'), 0)} GB libres (≈ {num(h.get('horas_restantes'), 0)} h)",
                  f"reloj {num(h.get('desfase_s'), 2, signo=True)} s",
                  f"pérdidas {h.get('perdidas', 0)}"]
        if h.get("reaperturas"):
            partes.append(f"reaperturas {h['reaperturas']}")
        if h.get("app"):
            partes.append(f"metadata {'✓' if h.get('metadata_ok') else '✗'} {h['app']}")
        if h.get("monitor"):
            partes.append(f"escuchando en {h.get('monitor_dispositivo')} ({round(100 * h.get('monitor_vol', 0))} %)")
        self.lbl_salud.setText(" · ".join(partes))

    def _cambiar_rango(self, r):
        self.rango = r
        self.sucio = True
        self.redibujar()

    def redibujar(self):
        if not self.sucio or self.inicio_epoch is None:
            return
        self.sucio = False
        t = self.curva_t.v
        x = self._a_reloj(t, False)
        y = np.maximum(self.curva_st.v, -60.0)
        if self.pausas and len(t) > 1:
            # un hueco donde hubo una pausa: sin esto, la línea uniría el último punto de antes con el
            # primero de después
            k = np.searchsorted(t, [p[0] for p in self.pausas], side="right")
            k = np.unique(k[(k > 0) & (k < len(t))])
            x, y = np.insert(x, k, np.nan), np.insert(y, k, np.nan)
        self.curva.setData(x, y, connect="finite")
        if self.seg_cortes:
            c = np.array(self.seg_cortes)
            self.cortes.setData(c[:, 0], c[:, 1])
        else:
            self.cortes.setData([], [])
        if self.seg_niveles:
            n = np.array(self.seg_niveles)
            self.niveles.setData(n[:, 0], n[:, 1])
        else:
            self.niveles.setData([], [])
        p = self.pista_actual
        if p and not vacio(p.get("lufs")) and len(x):
            self.nivel_actual.setData([float(self._a_reloj(p["inicio_s"], True)), x[-1]], [p["lufs"], p["lufs"]])
        else:
            self.nivel_actual.setData([], [])
        fin = x[-1] if len(x) else self.inicio_epoch + 60
        ini = {"1 h": fin - 3600, "6 h": fin - 6 * 3600}.get(self.rango, self.inicio_epoch)
        self.plot.setXRange(max(ini, self.inicio_epoch), max(fin, self.inicio_epoch + 60), padding=0.01)


def _html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------------------
# Pestaña: configuración
# ----------------------------------------------------------------------------
class PestanaConfig(QWidget):
    cambio = Signal()

    def __init__(self, cfg):
        super().__init__()
        self._cargando = True
        self.salidas = []

        self.carpeta = QLineEdit(cfg.carpeta_salida)
        b_carpeta = QPushButton("Elegir…")
        b_carpeta.clicked.connect(self._elegir_carpeta)
        fila_carpeta = QHBoxLayout()
        fila_carpeta.addWidget(self.carpeta, 1)
        fila_carpeta.addWidget(b_carpeta)

        self.fuente = QComboBox()
        self.fuente.addItems(list(APP_POR_FUENTE))
        self.fuente.setCurrentText(cfg.fuente)
        self.fuente.currentTextChanged.connect(self._fuente_cambio)
        self.playlist = QLineEdit(cfg.playlist)
        self.playlist.setPlaceholderText("nombre de la playlist o del canal")
        self.normalizacion = QComboBox()
        self.normalizacion.setEditable(True)
        self.normalizacion.addItems(["", "Activada", "Activada (Alto)", "Activada (Silencioso)", "Desactivada", "No aplica"])
        self.normalizacion.setCurrentText(cfg.normalizacion)
        self.crossfade = QLineEdit(cfg.crossfade)
        self.crossfade.setPlaceholderText("ej.: no, 5 s")
        self.calidad = QLineEdit(cfg.calidad)
        self.calidad.setPlaceholderText("ej.: Muy alta, sin ajuste automático")
        self.notas = QPlainTextEdit(cfg.notas)
        self.notas.setFixedHeight(64)
        self.etiqueta = QLineEdit(cfg.etiqueta)
        self.etiqueta.setPlaceholderText("nombre corto de la sesión, ej.: spotify_normalizado")
        b_sugerir = QPushButton("Sugerir")
        b_sugerir.clicked.connect(self._sugerir_etiqueta)
        fila_etq = QHBoxLayout()
        fila_etq.addWidget(self.etiqueta, 1)
        fila_etq.addWidget(b_sugerir)

        self.app = QComboBox()
        self.app.setEditable(True)
        self.app.addItems(sorted({v for v in APP_POR_FUENTE.values() if v}))
        self.app.setCurrentText(cfg.app)
        b_detectar = QPushButton("Detectar")
        b_detectar.setToolTip("Lista las apps que hoy publican qué están reproduciendo")
        b_detectar.clicked.connect(self.detectar_apps)
        fila_app = QHBoxLayout()
        fila_app.addWidget(self.app, 1)
        fila_app.addWidget(b_detectar)
        self.lbl_apps = QLabel("")
        self.lbl_apps.setStyleSheet(f"color:{GRIS}")
        self.lbl_apps.setWordWrap(True)
        self.lbl_apps.hide()

        self.dispositivo = QComboBox()
        b_actualizar = QPushButton("Actualizar")
        b_actualizar.clicked.connect(self.cargar_dispositivos)
        fila_disp = QHBoxLayout()
        fila_disp.addWidget(self.dispositivo, 1)
        fila_disp.addWidget(b_actualizar)
        self._disp_guardado = cfg.dispositivo
        self.monitor_disp = QComboBox()
        self._monitor_guardado = cfg.monitor_dispositivo

        self.horas = QDoubleSpinBox()
        self.horas.setRange(0, 24 * 14)
        self.horas.setDecimals(1)
        self.horas.setSuffix(" h")
        self.horas.setSpecialValueText("hasta detener")
        self.horas.setValue(cfg.horas)
        self.umbral = QDoubleSpinBox()
        self.umbral.setRange(0.5, 20)
        self.umbral.setSingleStep(0.5)
        self.umbral.setSuffix(" LU")
        self.umbral.setValue(cfg.umbral_salto)
        self.silencio = QDoubleSpinBox()
        self.silencio.setRange(0.5, 120)
        self.silencio.setSuffix(" min")
        self.silencio.setValue(cfg.alerta_silencio_min)
        self.min_dur = QDoubleSpinBox()
        self.min_dur.setRange(5, 600)
        self.min_dur.setDecimals(0)
        self.min_dur.setSuffix(" s")
        self.min_dur.setValue(cfg.min_dur)

        self.monitor_vol = cfg.monitor_vol
        self.monitor = cfg.monitor
        self.checklist = dict(cfg.checklist)

        g_destino = QGroupBox("Dónde se guarda")
        f0 = QFormLayout(g_destino)
        f0.addRow("Carpeta de salida", fila_carpeta)
        f0.addRow("Nombre de la sesión", fila_etq)

        g_cond = QGroupBox("Condiciones del experimento (quedan registradas en la sesión)")
        f1 = QFormLayout(g_cond)
        f1.addRow("Fuente", self.fuente)
        f1.addRow("Playlist / canal", self.playlist)
        f1.addRow("Normalización", self.normalizacion)
        f1.addRow("Crossfade", self.crossfade)
        f1.addRow("Calidad de streaming", self.calidad)
        f1.addRow("Notas", self.notas)

        g_cap = QGroupBox("Captura")
        f2 = QFormLayout(g_cap)
        f2.addRow("Grabar desde", fila_disp)
        f2.addRow("App a seguir", fila_app)
        f2.addRow("", self.lbl_apps)
        f2.addRow("Escuchar en", self.monitor_disp)
        f2.addRow("Duración", self.horas)
        ayuda = QLabel("Para grabar sin que suene, en Windows envía la app de música a «CABLE Input» "
                       "(Configuración › Sistema › Sonido › Mezclador de volumen) y graba desde ese mismo "
                       "dispositivo. Para oírla igual, activa «Escuchar» arriba: lo que sale por los "
                       "parlantes es una copia y no afecta la medición.")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet(f"color:{GRIS}")
        f2.addRow(ayuda)

        g_umb = QGroupBox("Umbrales")
        f3 = QFormLayout(g_umb)
        f3.addRow("Marcar saltos desde", self.umbral)
        f3.addRow("Avisar si hay silencio por", self.silencio)
        f3.addRow("Duración mínima de una canción", self.min_dur)

        cuerpo = QWidget()
        lay = QVBoxLayout(cuerpo)
        for g in (g_destino, g_cond, g_cap, g_umb):
            lay.addWidget(g)
        self.lbl_bloqueo = QLabel("")
        self.lbl_bloqueo.setStyleSheet(f"color:{AMARILLO}")
        lay.addWidget(self.lbl_bloqueo)
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cuerpo)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        externo = QVBoxLayout(self)
        externo.setContentsMargins(0, 0, 0, 0)
        externo.addWidget(scroll)
        self.grupos = (g_destino, g_cond, g_cap, g_umb)

        for w in (self.carpeta, self.playlist, self.crossfade, self.calidad, self.etiqueta):
            w.textChanged.connect(self._avisar)
        self.notas.textChanged.connect(self._avisar)
        for w in (self.normalizacion, self.app):
            w.currentTextChanged.connect(self._avisar)
        for w in (self.dispositivo, self.monitor_disp):
            w.currentIndexChanged.connect(self._avisar)
        for w in (self.horas, self.umbral, self.silencio, self.min_dur):
            w.valueChanged.connect(self._avisar)
        self._cargando = False

    def _avisar(self, *_):
        if not self._cargando:
            self.cambio.emit()

    def _elegir_carpeta(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta donde guardar las sesiones", self.carpeta.text())
        if d:
            self.carpeta.setText(os.path.normpath(d))

    def _fuente_cambio(self, fuente):
        app = APP_POR_FUENTE.get(fuente)
        if app is not None:
            self.app.setCurrentText(app)
        self._avisar()

    def _sugerir_etiqueta(self):
        partes = [self.fuente.currentText().split(" ")[0].lower()]
        norm = self.normalizacion.currentText().strip().lower()
        if norm:
            partes.append(norm.replace("activada", "norm").replace("desactivada", "sinnorm").replace(" ", ""))
        if self.playlist.text().strip():
            partes.append(self.playlist.text().strip().lower().replace(" ", "-")[:24])
        self.etiqueta.setText(sistema.limpiar_nombre("_".join(partes)))

    def cargar_dispositivos(self):
        from .. import dispositivos
        self.dispositivo.setEnabled(False)
        Tarea(dispositivos.listar).iniciar(self._dispositivos_listos, self._dispositivos_fallo)

    def _dispositivos_listos(self, info):
        self._cargando = True
        self.salidas = info["salidas"]
        elegido = self.dispositivo.currentData() or self._disp_guardado
        self.dispositivo.clear()
        por_defecto = None
        for s in self.salidas:
            if not s["grabable"]:
                continue
            txt = s["nombre"] + ("   — cable virtual" if s["cable"] else "")
            self.dispositivo.addItem(txt, s["nombre"])
            if por_defecto is None and s["nombre"].startswith("CABLE Input"):
                por_defecto = s["nombre"]
        objetivo = elegido or por_defecto or info["predeterminada"]
        i = self.dispositivo.findData(objetivo)
        self.dispositivo.setCurrentIndex(max(0, i))
        mon = self.monitor_disp.currentData() if self.monitor_disp.count() else self._monitor_guardado
        self.monitor_disp.clear()
        self.monitor_disp.addItem(f"Salida predeterminada de Windows ({info['predeterminada']})", "")
        for s in self.salidas:
            if not s["cable"]:
                self.monitor_disp.addItem(s["nombre"], s["nombre"])
        self.monitor_disp.setCurrentIndex(max(0, self.monitor_disp.findData(mon or "")))
        self.dispositivo.setEnabled(True)
        self._cargando = False
        self.cambio.emit()

    def _dispositivos_fallo(self, msg):
        self.dispositivo.setEnabled(True)
        self._aviso_apps(f"No pude listar los dispositivos de audio: {msg}")

    def _aviso_apps(self, texto):
        self.lbl_apps.setText(texto)
        self.lbl_apps.setVisible(bool(texto))

    def detectar_apps(self):
        from .. import metadata
        self._aviso_apps("Buscando apps…")
        Tarea(metadata.listar_apps).iniciar(self._apps_listas, lambda m: self._aviso_apps(f"Error: {m}"))

    def _apps_listas(self, apps):
        if apps is None:
            self._aviso_apps("Falta el componente de Windows para leer la metadata.")
            return
        if not apps:
            self._aviso_apps("Ninguna app está publicando qué reproduce. Pon música y vuelve a detectar.")
            return
        actual = self.app.currentText()
        for a in apps:
            if self.app.findText(a["app"]) < 0:
                self.app.addItem(a["app"])
        self.app.setCurrentText(actual)
        self._aviso_apps("Detectadas: " + "; ".join(
            f"{a['app']} ({a['estado']}: {a['artista']} – {a['titulo']})" for a in apps))

    def bloquear(self, si, texto=""):
        for g in self.grupos:
            g.setEnabled(not si)
        self.lbl_bloqueo.setText(texto if si else "")

    def leer(self):
        return cfgmod.Config(
            carpeta_salida=self.carpeta.text().strip(), fuente=self.fuente.currentText(),
            playlist=self.playlist.text().strip(), normalizacion=self.normalizacion.currentText().strip(),
            crossfade=self.crossfade.text().strip(), calidad=self.calidad.text().strip(),
            notas=self.notas.toPlainText().strip(), etiqueta=self.etiqueta.text().strip(),
            app=self.app.currentText().strip(), dispositivo=self.dispositivo.currentData() or self._disp_guardado,
            horas=self.horas.value(), monitor=self.monitor,
            monitor_dispositivo=(self.monitor_disp.currentData() if self.monitor_disp.count() else self._monitor_guardado) or "",
            monitor_vol=self.monitor_vol, umbral_salto=self.umbral.value(),
            alerta_silencio_min=self.silencio.value(), min_dur=self.min_dur.value(), checklist=self.checklist)

    def mostrar(self, c):
        """Muestra la configuración de una sesión: la de una grabación en curso (al reconectarse) o la
        de una guardada."""
        self._cargando = True
        self.carpeta.setText(c.get("carpeta_salida", ""))
        self.fuente.setCurrentText(c.get("fuente", ""))
        self.playlist.setText(c.get("playlist", ""))
        self.normalizacion.setCurrentText(c.get("normalizacion", ""))
        self.crossfade.setText(c.get("crossfade", ""))
        self.calidad.setText(c.get("calidad", ""))
        self.notas.setPlainText(c.get("notas", ""))
        self.etiqueta.setText(c.get("etiqueta", ""))
        self.app.setCurrentText(c.get("app", ""))
        self.horas.setValue(float(c.get("horas") or 0))
        self.umbral.setValue(float(c.get("umbral_salto") or self.umbral.value()))
        self.silencio.setValue(float(c.get("alerta_silencio_min") or self.silencio.value()))
        self.min_dur.setValue(float(c.get("min_dur") or self.min_dur.value()))
        disp = c.get("dispositivo")
        if disp:
            i = self.dispositivo.findData(disp)
            if i >= 0:
                self.dispositivo.setCurrentIndex(i)
            self._disp_guardado = disp
        self._cargando = False


# ----------------------------------------------------------------------------
# Pestaña: chequeo previo
# ----------------------------------------------------------------------------
MANUAL = [
    "La app de música sale por CABLE Input (Configuración › Sistema › Sonido › Mezclador de volumen).",
    "Volumen de la app al 100 %, tanto dentro de la app como en el mezclador de Windows.",
    "Calidad de streaming fija (Spotify: «Muy alta» y sin ajuste automático).",
    "Normalización y crossfade de la app configurados según la condición y anotados en la sesión.",
    "Windows no baja el volumen en llamadas (Panel de sonido › Comunicaciones › «No hacer nada»).",
    "CABLE Input sin mejoras de audio ni sonido espacial.",
]


class PestanaChequeo(QWidget):
    cambio = Signal()

    def __init__(self, ventana):
        super().__init__()
        self.v = ventana
        self.filas = QVBoxLayout()
        self.ultima_cadena = None

        self.b_revisar = QPushButton("Revisar ahora")
        self.b_revisar.clicked.connect(self.revisar)
        self.b_cadena = QPushButton("Probar la cadena de medición")
        self.b_cadena.setToolTip("Reproduce un tono de −20 LUFS en el dispositivo que se graba (no suena en "
                                 "los parlantes si es el cable) y comprueba que se mida −20,0.")
        self.b_cadena.clicked.connect(self.probar_cadena)
        self.b_niveles = QPushButton("¿A dónde está sonando la música?")
        self.b_niveles.setToolTip("Mide 1,5 s de cada salida: con la música sonando, debería llegar solo al cable.")
        self.b_niveles.clicked.connect(self.medir_niveles)
        botones = QHBoxLayout()
        for b in (self.b_revisar, self.b_cadena, self.b_niveles):
            botones.addWidget(b)
        botones.addStretch()

        g_auto = QGroupBox("Lo que la app revisa sola")
        la = QVBoxLayout(g_auto)
        la.addLayout(botones)
        la.addLayout(self.filas)
        self.lbl_cadena = QLabel("")
        self.lbl_cadena.setWordWrap(True)
        self.lbl_niveles = QLabel("")
        self.lbl_niveles.setWordWrap(True)
        la.addWidget(self.lbl_cadena)
        la.addWidget(self.lbl_niveles)

        g_man = QGroupBox("Lo que hay que revisar a mano (queda registrado en la sesión)")
        lm = QVBoxLayout(g_man)
        self.checks = []
        for txt in MANUAL:
            c = QCheckBox(txt)
            c.setChecked(bool(ventana.cfg.checklist.get(txt)))
            c.toggled.connect(self._check_cambio)
            lm.addWidget(c)
            self.checks.append(c)

        lay = QVBoxLayout(self)
        lay.addWidget(g_auto)
        lay.addWidget(g_man)
        lay.addStretch()

    def _check_cambio(self, *_):
        self.v.pestana_config.checklist = {c.text(): c.isChecked() for c in self.checks}
        self.cambio.emit()

    def _limpiar(self):
        while self.filas.count():
            w = self.filas.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _fila(self, estado, texto):
        simbolo, color = {"ok": ("✓", VERDE), "aviso": ("!", AMARILLO), "mal": ("✗", ROJO)}[estado]
        l = QLabel(f"<span style='color:{color}; font-weight:bold'>{simbolo}</span>&nbsp; {texto}")
        l.setWordWrap(True)
        self.filas.addWidget(l)

    def bloquear(self, si):
        for b in (self.b_cadena, self.b_niveles):
            b.setEnabled(not si)
            b.setToolTip("No disponible mientras se graba: usaría el mismo dispositivo." if si else b.toolTip())

    def revisar(self):
        from .. import dispositivos, metadata
        cfg = self.v.pestana_config.leer()

        def trabajo():
            return {"disp": dispositivos.listar(), "apps": metadata.listar_apps(),
                    "libre": sistema.espacio_libre_gb(cfg.carpeta_salida)}

        self._limpiar()
        self._fila("aviso", "Revisando…")
        Tarea(trabajo).iniciar(lambda r: self._mostrar(r, cfg), lambda m: (self._limpiar(), self._fila("mal", m)))

    def _mostrar(self, r, cfg):
        from .. import metadata
        self._limpiar()
        disp = r["disp"]
        cables = [s for s in disp["salidas"] if s["cable"]]
        self._fila("ok" if cables else "mal", "Cable virtual instalado" if cables else
                   "No hay cable virtual: la música tendrá que sonar por los parlantes y cualquier sonido del "
                   "PC entrará en la medición. Instala VB-Cable (vb-audio.com).")
        sel = next((s for s in disp["salidas"] if s["nombre"] == cfg.dispositivo), None)
        if sel is None:
            self._fila("mal", f"El dispositivo elegido para grabar no existe: {cfg.dispositivo or '(ninguno)'}")
        elif sel["cable"]:
            self._fila("ok", f"Se graba desde el cable ({sel['nombre']}, {sel['sr']} Hz, {sel['canales']} canales)")
        else:
            self._fila("aviso", f"Se graba desde {sel['nombre']}, que no es el cable: lo que se grabe también "
                                "sonará, y cualquier sonido del PC entrará en la medición.")
        if sel and sel["canales"] > 2:
            self._fila("aviso", f"{sel['nombre']} está configurado con {sel['canales']} canales: con 2 basta y "
                                "ocupa la mitad de disco.")
        pred = disp["predeterminada"]
        if any(s["nombre"] == pred and s["cable"] for s in disp["salidas"]):
            self._fila("mal", f"La salida predeterminada de Windows es el cable ({pred}): las notificaciones y "
                              "cualquier otro sonido entrarían en la medición. Pon los parlantes como predeterminados.")
        else:
            self._fila("ok", f"La salida predeterminada es {pred}: los demás sonidos del PC no se graban")
        apps = r["apps"]
        if apps is None:
            self._fila("mal", "Falta el componente de Windows para leer qué canción suena.")
        elif not cfg.app:
            self._fila("aviso", "No elegiste qué app seguir: se tomará la que Windows considere activa.")
        else:
            a = metadata.elegir_info(apps, cfg.app)
            if a:
                self._fila("ok", f"{a['app']} publica qué suena ({a['estado']}: {a['artista']} – {a['titulo']})")
            else:
                otras = ", ".join(a["app"] for a in apps) or "ninguna"
                self._fila("aviso", f"«{cfg.app}» no está publicando qué suena todavía (abre la app y pon "
                                    f"música). Apps detectadas: {otras}.")
        libre = r["libre"]
        horas = libre / 1.38 if not vacio(libre) else float("nan")
        self._fila("ok" if libre > 20 else "aviso", f"Espacio libre en el destino: {num(libre, 0)} GB "
                                                    f"(≈ {num(horas, 0)} h de grabación)")
        self._fila("ok", "Mientras graba, el PC no se suspende (lo bloquea la app)")

    def probar_cadena(self):
        from .. import dispositivos
        cfg = self.v.pestana_config.leer()
        self.lbl_cadena.setText("Probando la cadena (4 s)…")
        self.b_cadena.setEnabled(False)

        def listo(r):
            self.b_cadena.setEnabled(not self.v.grabando())
            r["hora"] = dt.datetime.now().isoformat(timespec="seconds")
            r["dispositivo"] = cfg.dispositivo
            self.ultima_cadena = r
            color = VERDE if r.get("ok") else ROJO
            self.lbl_cadena.setText(f"<span style='color:{color}'><b>{'✓' if r.get('ok') else '✗'} Cadena:</b></span> "
                                    f"{_html(r.get('mensaje', ''))}")

        def fallo(m):
            self.b_cadena.setEnabled(not self.v.grabando())
            self.lbl_cadena.setText(f"<span style='color:{ROJO}'><b>✗ Cadena:</b></span> {_html(m)}")

        Tarea(dispositivos.probar_cadena, cfg.dispositivo).iniciar(listo, fallo)

    def medir_niveles(self):
        from .. import dispositivos
        self.lbl_niveles.setText("Midiendo cada salida…")
        self.b_niveles.setEnabled(False)

        def listo(r):
            self.b_niveles.setEnabled(not self.v.grabando())
            partes = [f"{x['nombre']}: <b>{'sin señal' if x['lufs'] is None else lufs_txt(x['lufs'])}</b>" for x in r]
            self.lbl_niveles.setText("Ahora suena en → " + " · ".join(partes))

        Tarea(dispositivos.medir_niveles).iniciar(listo, lambda m: self.lbl_niveles.setText(f"Error: {_html(m)}"))


# ----------------------------------------------------------------------------
# Ventana
# ----------------------------------------------------------------------------
class DialogoSesiones(QDialog):
    """Las sesiones guardadas en la carpeta de salida; se abre una con doble clic."""

    def __init__(self, padre, base, actual=None):
        super().__init__(padre)
        self.setWindowTitle("Sesiones guardadas")
        self.resize(780, 440)
        self.elegida = None
        self.filas = sesiones.listar(base)

        self.tabla = QTableWidget(len(self.filas), 5)
        self.tabla.setHorizontalHeaderLabels(["Sesión", "Empezó", "Horas", "Canciones", "Estado"])
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.tabla.setAlternatingRowColors(True)
        hdr = self.tabla.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i, s in enumerate(self.filas):
            empezo = dt.datetime.fromtimestamp(s["inicio_epoch"]).strftime("%d-%m-%Y %H:%M")
            # la carpeta es la identidad de la sesión (las anteriores a los nombres únicos traen la fecha)
            carpeta = os.path.basename(os.path.normpath(s["carpeta"]))
            for c, v in enumerate([carpeta, empezo, num(s["horas"], 2), str(s["canciones"]), s["estado"]]):
                it = QTableWidgetItem(v)
                if c in (2, 3):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if actual and sesiones._misma_carpeta(s["carpeta"], actual):
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if s["estado"] != "terminada":
                    it.setForeground(QBrush(QColor(AMARILLO)))
                self.tabla.setItem(i, c, it)
        self.tabla.itemSelectionChanged.connect(self._seleccion)
        self.tabla.cellDoubleClicked.connect(lambda *_: self._abrir())

        vacia = QLabel("No hay sesiones guardadas en esta carpeta.") if not self.filas else None
        self.b_abrir = QPushButton("Abrir")
        self.b_abrir.setDefault(True)
        self.b_abrir.setEnabled(False)
        self.b_abrir.clicked.connect(self._abrir)
        b_otra = QPushButton("Otra carpeta…")
        b_otra.setToolTip("Abrir una sesión que está en otro lugar (elige la carpeta de la sesión)")
        b_otra.clicked.connect(self._otra)
        b_cerrar = QPushButton("Cerrar")
        b_cerrar.clicked.connect(self.reject)
        botones = QHBoxLayout()
        botones.addWidget(b_otra)
        botones.addStretch()
        botones.addWidget(b_cerrar)
        botones.addWidget(self.b_abrir)

        lay = QVBoxLayout(self)
        carpeta = QLabel(f"En {base}")
        carpeta.setStyleSheet(f"color:{GRIS}")
        lay.addWidget(carpeta)
        lay.addWidget(self.tabla, 1)
        if vacia:
            lay.addWidget(vacia)
        lay.addLayout(botones)
        if self.filas:
            self.tabla.selectRow(0)

    def _seleccion(self):
        filas = self.tabla.selectionModel().selectedRows()
        ok = bool(filas) and self.filas[filas[0].row()]["estado"] != "en curso"
        self.b_abrir.setEnabled(ok)
        self.b_abrir.setToolTip("" if ok or not filas else "Se está grabando: la ventana se conecta sola a esa grabación.")

    def _abrir(self):
        filas = self.tabla.selectionModel().selectedRows()
        if filas and self.filas[filas[0].row()]["estado"] != "en curso":
            self.elegida = self.filas[filas[0].row()]["carpeta"]
            self.accept()

    def _otra(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de la sesión", os.path.dirname(os.path.normpath(self.filas[0]["carpeta"])) if self.filas else "")
        if not d:
            return
        if not sesiones.es_sesion(d):
            QMessageBox.warning(self, "No es una sesión", f"«{d}» no tiene un sesion.json: no es una sesión de Loudness Lab.")
            return
        self.elegida = os.path.normpath(d)
        self.accept()


class Ventana(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.cargar()
        self.plantilla = self.cfg          # lo que se guarda como «última configuración»
        self.ultima_sesion = self.cfg.ultima_sesion
        self.estado_ui = "detenido"
        self.conn = None
        self.proc = None
        self.esperando = None       # momento en que se lanzó el motor y aún no se conecta
        self.en_curso = False
        self.recibio_fin = False
        self.carpeta_sesion = None
        self.ult_estado = None
        self.setWindowTitle(f"Loudness Lab {VERSION}")
        self.setWindowIcon(icono())
        self.resize(1280, 900)

        # barra superior
        self.lbl_estado = QLabel("○ Sin grabar")
        f = self.lbl_estado.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        self.lbl_estado.setFont(f)
        self.lbl_tiempo = QLabel("")
        self.lbl_tiempo.setFont(QFont("Consolas", int(f.pointSizeF())))
        self.lbl_etiqueta = QLabel("")
        self.lbl_etiqueta.setStyleSheet(f"color:{GRIS}")
        self.b_grabar = QPushButton("▶  Iniciar grabación")
        self.b_grabar.setMinimumHeight(34)
        self.b_grabar.clicked.connect(self.grabar_o_detener)
        self.b_pausa = QPushButton("⏸  Pausar")
        self.b_pausa.setMinimumHeight(34)
        self.b_pausa.setToolTip("Pausa momentánea: mientras dura, el audio no se graba ni se mide. "
                                "Al reanudar, la sesión sigue.")
        self.b_pausa.clicked.connect(self.pausar_o_reanudar)
        self.b_pausa.hide()
        self.b_sesiones = QPushButton("Sesiones…")
        self.b_sesiones.setToolTip("Abrir una sesión guardada")
        self.b_sesiones.clicked.connect(self.elegir_sesion)
        self.b_carpeta = QPushButton("Abrir carpeta")
        self.b_carpeta.clicked.connect(self.abrir_carpeta)
        self.chk_escuchar = QCheckBox("Escuchar")
        self.chk_escuchar.setToolTip("Reproduce en los parlantes lo que se graba. No afecta la medición.")
        self.chk_escuchar.setChecked(self.cfg.monitor)
        self.chk_escuchar.toggled.connect(self.cambiar_escucha)
        self.sld_vol = QSlider(Qt.Orientation.Horizontal)
        self.sld_vol.setRange(0, 100)
        self.sld_vol.setValue(int(self.cfg.monitor_vol * 100))
        self.sld_vol.setFixedWidth(120)
        self.sld_vol.setToolTip("Volumen de escucha (solo parlantes)")
        self.sld_vol.valueChanged.connect(lambda _: self._vol_timer.start(250))
        self._vol_timer = QTimer(self)
        self._vol_timer.setSingleShot(True)
        self._vol_timer.timeout.connect(self.cambiar_escucha)

        barra = QHBoxLayout()
        barra.addWidget(self.lbl_estado)
        barra.addSpacing(10)
        barra.addWidget(self.lbl_tiempo)
        barra.addSpacing(10)
        barra.addWidget(self.lbl_etiqueta, 1)
        barra.addWidget(self.chk_escuchar)
        barra.addWidget(self.sld_vol)
        barra.addSpacing(14)
        barra.addWidget(self.b_sesiones)
        barra.addWidget(self.b_carpeta)
        barra.addWidget(self.b_pausa)
        barra.addWidget(self.b_grabar)

        self.lbl_alertas = QLabel("")
        self.lbl_alertas.setWordWrap(True)
        self.lbl_alertas.setStyleSheet(f"background:#fdecea; color:{ROJO}; border:1px solid #f5c2c0;"
                                       "border-radius:6px; padding:8px; font-weight:bold")
        self.lbl_alertas.hide()

        self.pestana_vivo = PestanaVivo()
        self.pestana_config = PestanaConfig(self.cfg)
        self.pestana_chequeo = PestanaChequeo(self)
        self.pestana_config.cambio.connect(self.guardar_config)
        self.pestana_chequeo.cambio.connect(self.guardar_config)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.pestana_vivo, "En vivo")
        self.tabs.addTab(self.pestana_config, "Configuración")
        self.tabs.addTab(self.pestana_chequeo, "Chequeo previo")

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(barra)
        lay.addWidget(self.lbl_alertas)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Sin motor")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.leer_motor)
        self.timer.start(100)
        self.timer_dibujo = QTimer(self)
        self.timer_dibujo.timeout.connect(self.pestana_vivo.redibujar)
        self.timer_dibujo.start(1000)

        self.pestana_config.cargar_dispositivos()
        info = protocolo.motor_vivo()
        if info:
            self.esperando = time.time()
            self._poner_estado("conectando")
        else:
            self._sin_motor()

    # ------------------------------------------------------------------ config
    def guardar_config(self):
        if self.estado_ui in ("viendo", "terminada"):
            # La pestaña muestra una sesión guardada: lo que hay en ella no es la configuración
            # para la próxima grabación y no la reemplaza.
            self.plantilla.monitor = self.chk_escuchar.isChecked()
            self.plantilla.monitor_vol = self.sld_vol.value() / 100
        else:
            self.cfg = self.pestana_config.leer()
            self.cfg.monitor = self.chk_escuchar.isChecked()
            self.cfg.monitor_vol = self.sld_vol.value() / 100
            self.plantilla = self.cfg
        self.plantilla.ultima_sesion = self.ultima_sesion
        try:
            cfgmod.guardar(self.plantilla)
        except OSError:
            pass

    def _recordar_sesion(self, carpeta):
        self.ultima_sesion = carpeta
        self.guardar_config()

    def _poner_nombre(self, nombre):
        self.lbl_etiqueta.setText(nombre)
        self.setWindowTitle(f"{nombre} — Loudness Lab {VERSION}" if nombre else f"Loudness Lab {VERSION}")

    def grabando(self):
        return self.en_curso or self.esperando is not None

    # ------------------------------------------------------------------ motor
    def grabar_o_detener(self):
        if self.estado_ui in ("viendo", "terminada"):
            self.nueva_sesion()
            return
        if self.conn is not None and self.en_curso:
            if not self._confirmar_detener():
                return
            self.conn.send({"cmd": "detener"})
            self.b_grabar.setEnabled(False)
            self.b_pausa.setEnabled(False)
            self.lbl_estado.setText("◌ Deteniendo…")
            return
        self.iniciar()

    def _confirmar_detener(self):
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Detener la sesión")
        caja.setText(f"¿Detener la sesión «{self.lbl_etiqueta.text()}»?")
        caja.setInformativeText("Detener es final: la sesión queda guardada, pero no se puede continuar. "
                                "Si solo necesitas parar un rato, usa «Pausar».")
        detener = caja.addButton("Detener la sesión", QMessageBox.ButtonRole.DestructiveRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is detener

    def pausar_o_reanudar(self):
        if self.conn is None or not self.en_curso:
            return
        try:
            self.conn.send({"cmd": "reanudar" if self.estado_ui == "pausada" else "pausar"})
        except Exception:
            return
        self.b_pausa.setEnabled(False)   # se vuelve a habilitar con el próximo estado del motor

    # ------------------------------------------------------------------ sesiones
    def _sin_motor(self):
        """No hay ninguna grabación en curso: se abre donde se quedó, en la última sesión."""
        if sesiones.es_sesion(self.ultima_sesion):
            self.abrir_sesion(self.ultima_sesion, al_inicio=True)
        else:
            self._modo_nueva(revisar=True)

    def _modo_nueva(self, revisar=False):
        self.carpeta_sesion = None
        self.statusBar().showMessage("Sin motor")
        self._poner_estado("detenido")
        self.tabs.setCurrentWidget(self.pestana_config)
        if revisar:
            QTimer.singleShot(400, self.pestana_chequeo.revisar)

    def nueva_sesion(self):
        """Prepara una sesión nueva con los mismos ajustes de la que se está viendo, con un nombre libre."""
        self.pestana_vivo.limpiar()
        self.lbl_tiempo.setText("")
        self._poner_nombre("")
        base = self.pestana_config.carpeta.text().strip()
        self.pestana_config._sugerir_etiqueta()
        self.pestana_config.etiqueta.setText(sesiones.nombre_libre(base, self.pestana_config.etiqueta.text()))
        self._modo_nueva()
        self.guardar_config()
        QTimer.singleShot(200, self.pestana_chequeo.revisar)

    def elegir_sesion(self):
        base = self.pestana_config.carpeta.text().strip() or self.plantilla.carpeta_salida
        d = DialogoSesiones(self, base, self.carpeta_sesion)
        if d.exec() == QDialog.DialogCode.Accepted and d.elegida:
            self.abrir_sesion(d.elegida)

    def abrir_sesion(self, carpeta, al_inicio=False):
        """Muestra una sesión guardada. Se reconstruye desde sus archivos, sin su audio, en otro hilo."""
        self.statusBar().showMessage("Abriendo la sesión…")
        self.b_sesiones.setEnabled(False)
        Tarea(sesiones.reconstruir, carpeta).iniciar(
            self._sesion_abierta, lambda msg: self._abrir_fallo(carpeta, msg, al_inicio))

    def _sesion_abierta(self, snap):
        self.b_sesiones.setEnabled(True)
        if self.grabando():   # mientras se abría, la ventana se conectó a una grabación
            return
        nombre = snap["cerrada"]["nombre"]
        self.carpeta_sesion = snap["carpeta"]
        self.pestana_vivo.mostrar_sesion(snap)
        self.pestana_config.mostrar(snap["config"])
        self._poner_nombre(nombre)
        self.lbl_tiempo.setText(hms(snap["estado"]["segundos"]))
        self._poner_estado("viendo")
        self._recordar_sesion(snap["carpeta"])
        self.tabs.setCurrentWidget(self.pestana_vivo)
        self.pestana_vivo.sucio = True
        self.pestana_vivo.redibujar()
        self.statusBar().showMessage(f"Sesión «{nombre}» · {snap['carpeta']}")

    def _abrir_fallo(self, carpeta, msg, al_inicio):
        self.b_sesiones.setEnabled(True)
        self.statusBar().clearMessage()
        if al_inicio and not self.grabando():
            self.ultima_sesion = ""
            self._modo_nueva(revisar=True)
        QMessageBox.warning(self, "No pude abrir la sesión", f"{carpeta}\n\n{msg}")

    def iniciar(self):
        self.guardar_config()
        cfg = self.cfg
        if not cfg.dispositivo:
            QMessageBox.warning(self, "Falta el dispositivo", "Elige desde dónde grabar en «Configuración».")
            return
        try:
            os.makedirs(cfg.carpeta_salida, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Carpeta de salida", f"No puedo usar «{cfg.carpeta_salida}»:\n{e}")
            return
        salida = next((s for s in self.pestana_config.salidas if s["nombre"] == cfg.dispositivo), None)
        if salida is not None and not salida["cable"]:
            r = QMessageBox.question(self, "No es el cable virtual",
                                     f"Vas a grabar «{cfg.dispositivo}», que no es el cable virtual: la música "
                                     "sonará por ahí y cualquier sonido del PC entrará en la medición.\n\n"
                                     "¿Grabar igual?")
            if r != QMessageBox.StandardButton.Yes:
                return
        if not cfg.etiqueta:
            self.pestana_config._sugerir_etiqueta()
            self.guardar_config()
            cfg = self.cfg
        nombre = sistema.limpiar_nombre(cfg.etiqueta)
        if os.path.exists(os.path.join(cfg.carpeta_salida, nombre)):
            libre = sesiones.nombre_libre(cfg.carpeta_salida, nombre)
            caja = QMessageBox(self)
            caja.setIcon(QMessageBox.Icon.Question)
            caja.setWindowTitle("Ya existe una sesión con ese nombre")
            caja.setText(f"Ya hay una sesión llamada «{nombre}» en esa carpeta.")
            caja.setInformativeText("Una sesión terminada no se puede continuar: cada grabación necesita su "
                                    "propio nombre.")
            crear = caja.addButton(f"Crear «{libre}»", QMessageBox.ButtonRole.AcceptRole)
            caja.addButton("Cambiar el nombre", QMessageBox.ButtonRole.RejectRole)
            caja.exec()
            if caja.clickedButton() is not crear:
                self.tabs.setCurrentWidget(self.pestana_config)
                self.pestana_config.etiqueta.setFocus()
                return
            self.pestana_config.etiqueta.setText(libre)
            self.guardar_config()
            cfg = self.cfg
        datos = asdict(cfg)
        datos["prueba_cadena"] = self.pestana_chequeo.ultima_cadena
        ruta = sistema.carpeta_app() / "inicio.json"
        ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        self.proc = sistema.lanzar_desacoplado(sistema.comando_propio("--motor", "--config", str(ruta)))
        self.esperando = time.time()
        self.recibio_fin = False
        self._poner_estado("iniciando")

    def leer_motor(self):
        if self.conn is None:
            if self.esperando is not None:
                self._intentar_conectar()
            return
        try:
            n = 0
            while self.conn is not None and self.conn.poll() and n < 300:
                self._manejar(self.conn.recv())
                n += 1
        except (EOFError, OSError, ConnectionError):
            self._desconectado()

    def _intentar_conectar(self):
        info = protocolo.motor_vivo()
        if info:
            try:
                self.conn = protocolo.conectar(info)
                self.esperando = None
                self.statusBar().showMessage(f"Motor conectado · sesión en {info.get('carpeta')}")
                return
            except Exception:
                pass
        elif self.proc is None:
            # El aviso de la grabación a la que había que conectarse ya no está: terminó mientras
            # tanto (por ejemplo, se cerró justo antes de abrir la app). No queda a qué conectarse.
            self.esperando = None
            if self.en_curso and not self.recibio_fin:
                self._terminar("el motor se cerró inesperadamente; revisa registro.log en la carpeta de la sesión")
            else:
                self._sin_motor()
            return
        if self.proc is not None and self.proc.poll() is not None:
            self.esperando = None
            err = ""
            try:
                err = protocolo.ruta_error().read_text(encoding="utf-8")
            except OSError:
                pass
            self._poner_estado("detenido")
            QMessageBox.critical(self, "No se pudo iniciar la grabación",
                                 (err.split("\n\n")[0] if err else "El motor terminó sin dar detalles.")
                                 + f"\n\nDetalle en {sistema.carpeta_app() / 'motor.log'}")
        elif time.time() - self.esperando > 25:
            self.esperando = None
            self._poner_estado("detenido")
            QMessageBox.critical(self, "No se pudo iniciar la grabación", "El motor no respondió a tiempo.")

    def _manejar(self, m):
        tipo = m.get("tipo")
        if tipo == "snapshot":
            self.en_curso = True
            self.carpeta_sesion = m.get("carpeta")
            self.pestana_vivo.reiniciar(m)
            self.pestana_config.mostrar(m.get("config") or {})
            self._poner_estado("pausada" if (m.get("estado") or {}).get("pausado") else "grabando")
            self._recordar_sesion(self.carpeta_sesion)
            self.tabs.setCurrentWidget(self.pestana_vivo)
            if m.get("estado"):
                # al reconectarse, la escucha se toma del motor: es el único momento en que la
                # ventana no sabe cómo quedó
                h = m["estado"].get("salud") or {}
                self.chk_escuchar.blockSignals(True)
                self.chk_escuchar.setChecked(bool(h.get("monitor")))
                self.chk_escuchar.blockSignals(False)
                self.sld_vol.blockSignals(True)
                self.sld_vol.setValue(int(round(100 * h.get("monitor_vol", 0.5))))
                self.sld_vol.blockSignals(False)
                self._estado(m["estado"])
        elif tipo == "estado":
            self._estado(m)
        elif tipo == "pausas":
            self.pestana_vivo.poner_pausas(m.get("pausas"))
        elif tipo == "curva":
            self.pestana_vivo.agregar_puntos(m.get("puntos", []))
        elif tipo == "pista":
            self.pestana_vivo.agregar_pista(m["pista"])
        elif tipo == "log":
            self.pestana_vivo.agregar_log(m.get("linea", ""), m.get("nivel", "INFO"))
        elif tipo == "fin":
            self.recibio_fin = True
            if m.get("estado"):
                self._estado(m["estado"])
            self.pestana_vivo.sucio = True
            self.pestana_vivo.redibujar()
            self._terminar(m.get("motivo", ""))

    def _estado(self, e):
        self.ult_estado = e
        self.lbl_tiempo.setText(hms(e.get("segundos")))
        self._poner_nombre(e.get("etiqueta", ""))
        if self.en_curso:
            pausado = bool(e.get("pausado"))
            if pausado and self.estado_ui == "grabando":
                self._poner_estado("pausada")
            elif not pausado and self.estado_ui == "pausada":
                self._poner_estado("grabando")
            if pausado:
                self.lbl_estado.setText(f"⏸ En pausa · {mmss(e.get('pausa_s') or 0)}")
            if self.estado_ui in ("grabando", "pausada"):
                self.b_pausa.setEnabled(True)
        self.pestana_vivo.actualizar(e)
        alertas = e.get("alertas") or []
        self.lbl_alertas.setText("\n".join(f"⚠  {a}" for a in alertas))
        self.lbl_alertas.setVisible(bool(alertas))
        # La casilla «Escuchar» refleja lo que eligió el usuario, no el último estado del motor: un
        # estado emitido antes de procesar la orden la desmarcaba, y el siguiente cambio de volumen
        # mandaba «apagar». Lo que pasó de verdad se ve en la línea de salud.

    def _desconectado(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = None
        if protocolo.motor_vivo():
            self.esperando = time.time()   # sigue grabando: reconectar
            return
        if self.en_curso and not self.recibio_fin:
            self._terminar("el motor se cerró inesperadamente; revisa registro.log en la carpeta de la sesión")
        elif not self.en_curso and self.estado_ui == "conectando":
            # Se conectó a una grabación que justo se estaba cerrando y nunca llegó a mostrarla.
            self._sin_motor()

    def _terminar(self, motivo):
        self.en_curso = False
        self.esperando = None
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self._poner_estado("terminada", motivo)
        self.lbl_alertas.hide()
        self.statusBar().showMessage(f"Grabación terminada: {motivo}")

    def _poner_estado(self, estado, motivo=""):
        self.estado_ui = estado
        textos = {"detenido": ("○ Sin grabar", GRIS), "iniciando": ("◌ Iniciando…", AMARILLO),
                  "conectando": ("◌ Conectando con la grabación en curso…", AMARILLO),
                  "grabando": ("● Grabando", ROJO), "pausada": ("⏸ En pausa", AMARILLO),
                  "terminada": ("■ Terminada", GRIS), "viendo": ("▣ Sesión guardada", GRIS)}
        txt, color = textos[estado]
        self.lbl_estado.setText(txt + (f" ({motivo})" if motivo and estado == "terminada" else ""))
        self.lbl_estado.setStyleSheet(f"color:{color}")
        activo = estado in ("iniciando", "conectando", "grabando", "pausada")
        solo_lectura = estado in ("viendo", "terminada")
        self.b_grabar.setText("■  Detener" if activo else ("+  Nueva sesión" if solo_lectura else "▶  Iniciar grabación"))
        self.b_grabar.setEnabled(estado in ("detenido", "terminada", "viendo", "grabando", "pausada"))
        self.b_pausa.setVisible(activo)
        self.b_pausa.setEnabled(estado in ("grabando", "pausada"))
        self.b_pausa.setText("▶  Reanudar" if estado == "pausada" else "⏸  Pausar")
        self.b_sesiones.setEnabled(not activo)
        self.pestana_config.bloquear(
            activo or solo_lectura,
            "Grabación en curso: la configuración queda fija hasta que termine. Solo se puede cambiar «Escuchar»."
            if activo else "Sesión guardada: su configuración es de solo lectura. Con «Nueva sesión» puedes "
                           "grabar otra con estos mismos ajustes.")
        self.pestana_chequeo.bloquear(activo)

    # ------------------------------------------------------------------ escucha
    def cambiar_escucha(self, *_):
        self.guardar_config()
        if self.conn is not None and self.en_curso:
            try:
                self.conn.send({"cmd": "monitor", "on": self.chk_escuchar.isChecked(),
                                "vol": self.sld_vol.value() / 100, "dispositivo": self.cfg.monitor_dispositivo})
            except Exception:
                pass

    def abrir_carpeta(self):
        destino = self.carpeta_sesion or self.cfg.carpeta_salida
        try:
            os.makedirs(destino, exist_ok=True)
            sistema.abrir(destino)
        except OSError as e:
            QMessageBox.warning(self, "Abrir carpeta", str(e))

    # ------------------------------------------------------------------ cierre
    def closeEvent(self, ev):
        if self.en_curso or self.esperando is not None:
            caja = QMessageBox(self)
            caja.setWindowTitle("Hay una grabación en curso")
            caja.setText("La grabación corre aparte de esta ventana: puede seguir aunque la cierres, y al "
                         "volver a abrir Loudness Lab te reconectas a ella.")
            seguir = caja.addButton("Cerrar la ventana y seguir grabando", QMessageBox.ButtonRole.AcceptRole)
            detener = caja.addButton("Detener la sesión (es final) y cerrar", QMessageBox.ButtonRole.DestructiveRole)
            caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
            caja.exec()
            b = caja.clickedButton()
            if b == detener and self.conn is not None:
                try:
                    self.conn.send({"cmd": "detener"})
                except Exception:
                    pass
            elif b != seguir:
                ev.ignore()
                return
        self.guardar_config()
        ev.accept()


def _paleta(app):
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#f6f7f9"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#f3f5f8"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#111827"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#111827"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#111827"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACENTO))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#111827"))
    app.setPalette(pal)
    pg.setConfigOptions(antialias=True, background="w", foreground="#4b5563")
    # Con esta paleta clara, Fusion dibuja las casillas con un borde casi invisible: se marcan
    # con borde y ticket propios para que se note que son casillas.
    ticket = sistema.carpeta_app() / "ticket.svg"
    try:
        ticket.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 '
                          '6-7" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" '
                          'stroke-linejoin="round"/></svg>', encoding="utf-8")
    except OSError:
        pass
    app.setStyleSheet(
        "QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #7b8494; border-radius: 3px;"
        " background: white; }"
        f"QCheckBox::indicator:hover {{ border-color: {ACENTO}; }}"
        f"QCheckBox::indicator:checked {{ background: {ACENTO}; border-color: {ACENTO};"
        f" image: url('{ticket.as_posix()}'); }}"
        "QCheckBox::indicator:disabled { background: #eceef1; border-color: #c3c8d0; }")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Loudness Lab")
    _paleta(app)
    v = Ventana()
    v.show()
    # Solo para revisión automática: LOUDNESS_CAPTURA="ruta.png|segundos[|pestaña]" guarda una imagen
    # de la ventana y la cierra (sin detener el motor).
    captura = os.environ.get("LOUDNESS_CAPTURA")
    if captura:
        partes = captura.split("|")
        ruta, seg = partes[0], float(partes[1]) if len(partes) > 1 else 5

        def capturar():
            if len(partes) > 2:
                v.tabs.setCurrentIndex(int(partes[2]))
                QApplication.processEvents()
                v.pestana_vivo.sucio = True
                v.pestana_vivo.redibujar()
                QApplication.processEvents()
            v.grab().save(ruta)
            v.en_curso = False
            v.esperando = None
            app.quit()

        QTimer.singleShot(int(seg * 1000), capturar)
    codigo = app.exec()
    Tarea.esperar_todas()
    return codigo
