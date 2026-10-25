"""Ventana de Loudness Lab.

La ventana no mide nada: lanza el motor en un proceso aparte, le habla por un socket local y
muestra lo que el motor publica. Por eso se puede cerrar sin cortar la grabación; al volver a
abrirla, se reconecta.
"""

import bisect
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
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QScrollArea, QSlider, QSplitter, QTableWidget,
                               QProgressBar, QTableWidgetItem, QTabWidget, QToolButton, QToolTip, QVBoxLayout, QWidget)

import pyqtgraph as pg

from .. import VERSION, bandas, capa, comparador, estudio, excel, guardian, protocolo, sesiones, sistema
from .. import config as cfgmod
from ..config import (APP_POR_FUENTE, CONDICIONES_POR_FUENTE, NORMALIZACIONES, STREAM_POR_FUENTE,
                      normalizacion_de, servicio_de)
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


def ic_txt(ic, dec=1, unidad=""):
    """« [a–b]» de un intervalo de confianza al 95 %, o nada si no hay."""
    if not ic or vacio(ic[0]) or vacio(ic[1]):
        return ""
    return f" [{num(ic[0], dec)}–{num(ic[1], dec, unidad=unidad)}]"


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
COLUMNAS = ["Inicio", "Artista – Título", "Dur.", "LUFS", "LRA", "True peak", "Salto", "ΔI", "Cuerpo", "Percib.",
            "Graves", "Desaj."]
AYUDA_COLUMNAS = {
    6: "Salto en el borde: LUFS de los primeros 10 s de la canción − los últimos 10 s de la anterior",
    7: "ΔI: LUFS integrado de la canción − el de la anterior",
    8: "Salto de cuerpo: mediana del short-term de la canción (de los 10 a los 70 s) − la del final de la anterior "
       "(sin sus últimos 10 s). No depende de intros ni finales",
    9: "Sonoridad percibida (Zwicker, ISO 532-1) en escala LUFS: el LUFS al que un ruido rosa sonaría igual de fuerte. "
       "Mayor que su LUFS: suena más de lo que dice su LUFS",
    10: "Energía en graves (25–200 Hz), % del total sin ponderar",
    11: "Desajuste LUFS–oído (dB, índice rápido ISO 226): + suena más de lo que dice su LUFS a volumen de fondo, "
        "− suena menos",
}
AYUDA_CUERPO = ("Salto de cuerpo: mediana del short-term de la canción nueva (de los 10 a los 70 s) − la del final de "
                "la anterior (sin sus últimos 10 s). Arranque: máximo short-term de los 3 a los 15 s de la nueva − esa "
                "misma referencia. En los cambios entre canciones válidas.")
AYUDA_SONORIDAD = ("Dispersión entre canciones válidas de la sonoridad percibida (Zwicker, ISO 532-1) en escala LUFS, "
                   "junto a la de sus LUFS. Si es mayor, a igual LUFS las canciones suenan distinto. LN: mediana del nivel "
                   "de sonoridad (del N5 de cada canción), en phon.")
COLOR_PERCIBIDO = "#2a9d8f"


def _texto_reparto(esp, con_desajuste=True):
    """«graves 32 % · medios-bajos 9 % · medios 52 % · agudos 7 %» y el desajuste, de un espectro de canción."""
    r = esp.get("reparto") or {}
    partes = [f"{bandas.NOMBRES_REGIONES[k].lower()} {num(r.get(k), 0, unidad='%')}" for k in bandas.REGIONES]
    txt = " · ".join(partes)
    d = esp.get("desajuste_db")
    if con_desajuste and not vacio(d):
        como = "suena más de lo que dice su LUFS" if d > 0.5 else "suena menos de lo que dice su LUFS" if d < -0.5 \
            else "suena como dice su LUFS"
        txt += f"\nDesajuste LUFS–oído: {num(d, 1, signo=True, unidad='dB')} ({como})"
    return txt


class PanelFrecuencias(QGroupBox):
    """La capa de frecuencias: energía por tercio de octava, con lo que suma LUFS (filtro K) y lo que oye
    el oído al nivel de escucha del índice rápido (ISO 226, 60 phon por defecto). En vivo, los últimos 3 s
    y la canción en curso; en una sesión guardada, el promedio de sus canciones válidas, con controles para
    recalcular la percepción a otro nivel de escucha (y guardarlo en la sesión). Si la sesión no tiene la
    capa, lo dice y ofrece generarla."""

    pedir_capa = Signal()
    recalcular = Signal(float, float)     # (dB SPL de −14 LUFS, phon del índice rápido)
    guardar = Signal(float, float)
    MARCAS = (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
    # como el visor verde de un equipo (VFD): barras anchas de segmentos delgados, 24 de 2,5 dB (−70 a −10 dB).
    # El nivel en verde claro casi blanco; encima, hasta el pico reciente, el rastro en verde intenso que se
    # desvanece hacia arriba y cae lento; los segmentos apagados apenas se ven.
    BLOQUES, DB_BLOQUE, DB_PISO = 24, 2.5, -70.0
    COLOR_NIVEL, COLOR_RASTRO, COLOR_APAGADO, COLOR_TEXTO = "#d4fbe0", "#1fe38a", "#0e2a1a", "#a9e8c1"
    PICO_ESPERA_S, PICO_CAIDA = 0.4, 10.0   # el pico espera 0,4 s y cae 10 segmentos por segundo
    COLOR_K, COLOR_OIDO = "#ffb020", "#ff4fd8"

    def __init__(self):
        super().__init__("Frecuencias")
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#000000")
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.setMinimumHeight(120)
        eje = self.plot.getAxis("bottom")
        eje.setTicks([[(bandas.NOMINALES.index(f), (f"{f / 1000:g}K" if f >= 1000 else f"{f:g}")) for f in self.MARCAS]])
        izq = self.plot.getAxis("left")
        izq.setTicks([[(k, f"{self.DB_PISO + k * self.DB_BLOQUE:g}") for k in range(0, self.BLOQUES + 1, 4)],
                      [(k, "·") for k in range(2, self.BLOQUES, 4)]])
        izq.setLabel("dB", color=self.COLOR_TEXTO)
        eje.setLabel("Hz", color=self.COLOR_TEXTO)
        for a in (eje, izq):
            a.setPen(pg.mkPen(None))
            a.setTextPen(pg.mkPen(self.COLOR_TEXTO))
            a.setStyle(tickLength=0)
        n, b = bandas.N_BANDAS, self.BLOQUES
        self._xs = np.repeat(np.arange(n), b).astype(float)
        self._ys = np.tile(np.arange(b), n)
        # pinceles: 0 apagado, 1 nivel, 2… rastro (de más intenso abajo a más tenue arriba)
        rastro = []
        for a in np.linspace(235, 70, 8):
            c = QColor(self.COLOR_RASTRO)
            c.setAlpha(int(a))
            rastro.append(pg.mkBrush(c))
        self._pinceles = np.array([pg.mkBrush(self.COLOR_APAGADO), pg.mkBrush(self.COLOR_NIVEL)] + rastro, dtype=object)
        self.segmentos = pg.BarGraphItem(x=self._xs, y0=self._ys + 0.22, height=0.56, width=0.82, pen=pg.mkPen(None),
                                         brushes=list(self._pinceles[np.zeros(n * b, dtype=int)]))
        self.plot.addItem(self.segmentos)
        self.linea_k = self.plot.plot(pen=pg.mkPen(self.COLOR_K, width=1.6))
        self.linea_oido = self.plot.plot(pen=pg.mkPen(self.COLOR_OIDO, width=1.8, style=Qt.PenStyle.DashLine))
        self.plot.setYRange(0, self.BLOQUES, padding=0.02)
        self.plot.setXRange(-0.6, n - 0.4, padding=0)
        self.nivel = np.zeros(n)                # altura de cada barra, en segmentos (0 a 24)
        self.pico = np.zeros(n)                 # hasta dónde llega el rastro del pico de cada banda
        self.pico_t = np.zeros(n)               # cuándo subió el pico por última vez
        self._ahora = time.monotonic
        self._t_animacion = None
        self.timer_pico = QTimer(self)
        self.timer_pico.setInterval(50)
        self.timer_pico.timeout.connect(self._animar_picos)
        k = bandas.ganancia_k(48000)
        self.rel_k = k - k[bandas.NOMINALES.index(1000)]
        self.phon = bandas.PHON_ESCUCHA
        self.rel_oido = bandas.sensibilidad_oido(self.phon)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        self.leyenda = QLabel("")
        self.leyenda.setWordWrap(True)
        self.leyenda.setStyleSheet(f"color:{GRIS}")
        self._poner_leyenda()
        # recalcular una sesión guardada a otro nivel de escucha
        self.sp_nivel = QDoubleSpinBox()
        self.sp_nivel.setRange(40, 95)
        self.sp_nivel.setDecimals(0)
        self.sp_nivel.setSuffix(" dB SPL")
        self.sp_nivel.setToolTip("Sonoridad percibida (Zwicker): a qué nivel suena una canción a −14 LUFS en el lugar "
                                 "de escucha (dB SPL sin ponderar)")
        self.sp_phon = QDoubleSpinBox()
        self.sp_phon.setRange(20, 90)
        self.sp_phon.setDecimals(0)
        self.sp_phon.setSuffix(" phon")
        self.sp_phon.setToolTip("Índice rápido LUFS–oído (ISO 226) y línea magenta: nivel de escucha en phon")
        self.b_recalcular = QPushButton("Recalcular")
        self.b_recalcular.setToolTip("Vuelve a calcular la sesión con estos niveles, sin cambiarla")
        self.b_recalcular.clicked.connect(lambda: self.recalcular.emit(self.sp_nivel.value(), self.sp_phon.value()))
        self.b_guardar = QPushButton("Guardar en la sesión")
        self.b_guardar.setToolTip("Deja estos niveles como los de la sesión (queda registro del cambio)")
        self.b_guardar.clicked.connect(lambda: self.guardar.emit(self.sp_nivel.value(), self.sp_phon.value()))
        self.lbl_niveles = QLabel("")
        self.lbl_niveles.setWordWrap(True)
        self.lbl_niveles.setStyleSheet(f"color:{GRIS}")
        self.fila_niveles = QWidget()
        fn = QHBoxLayout(self.fila_niveles)
        fn.setContentsMargins(0, 0, 0, 0)
        fn.addWidget(QLabel("Calcular a: −14 LUFS ="))
        fn.addWidget(self.sp_nivel)
        fn.addWidget(QLabel("· índice rápido a"))
        fn.addWidget(self.sp_phon)
        fn.addWidget(self.b_recalcular)
        fn.addWidget(self.b_guardar)
        fn.addStretch()
        self.lbl_falta = QLabel("")
        self.lbl_falta.setWordWrap(True)
        self.lbl_falta.setStyleSheet(f"color:{AMARILLO}")
        self.b_capa = QPushButton("Generar capa de frecuencias…")
        self.b_capa.clicked.connect(self.pedir_capa.emit)
        lado = QVBoxLayout()
        lado.addWidget(self.lbl)
        lado.addWidget(self.leyenda)
        lado.addWidget(self.fila_niveles)
        lado.addWidget(self.lbl_niveles)
        lado.addWidget(self.lbl_falta)
        lado.addWidget(self.b_capa)
        lado.addStretch()
        lay = QHBoxLayout(self)
        lay.addWidget(self.plot, 3)
        lay.addLayout(lado, 2)
        self.limpiar()

    def _poner_leyenda(self):
        self.leyenda.setText(f"<span style='color:#7fd8a0'>▮</span> energía por banda (2,5 dB por segmento; en verde "
                             f"intenso, el pico reciente que va cayendo) &nbsp; <span style='color:{self.COLOR_K}'>━</span> "
                             f"ámbar: lo que suma LUFS (filtro K) &nbsp; <span style='color:{self.COLOR_OIDO}'>┅</span> "
                             f"magenta: lo que oye el oído a volumen de fondo ({self.phon:g} phon)")

    def poner_phon(self, phon):
        """El nivel de escucha (phon) de la línea verde: el del índice rápido de la sesión."""
        if vacio(phon) or float(phon) == self.phon:
            return
        self.phon = float(phon)
        self.rel_oido = bandas.sensibilidad_oido(self.phon)
        self._poner_leyenda()

    def _a_bloques(self, db_):
        """dB → altura en bloques (con fracción), para las líneas."""
        return (np.asarray(db_, dtype=float) - self.DB_PISO) / self.DB_BLOQUE

    def _pintar(self):
        """Cada segmento: encendido (bajo el nivel), rastro del pico (entre el nivel y el pico, más tenue hacia
        arriba) o apagado."""
        b = self.BLOQUES
        nivel = np.repeat(np.round(self.nivel), b)
        pico = np.repeat(np.round(self.pico), b)
        y = self._ys
        tramo = np.maximum(pico - nivel, 1)
        rastro = 2 + np.clip(((y - nivel) / tramo * 8).astype(int), 0, 7)
        indice = np.where(y < nivel, 1, np.where(y < pico, rastro, 0))
        self.segmentos.setOpts(brushes=list(self._pinceles[indice]))

    def _poner_barras(self):
        self._pintar()

    def _poner_picos(self):
        self._pintar()

    def _animar_picos(self):
        """El bloque de pico: espera un momento arriba y después cae lento hasta el nivel nuevo."""
        ahora = self._ahora()
        dt_ = ahora - (self._t_animacion if self._t_animacion is not None else ahora)
        self._t_animacion = ahora
        cae = (self.pico > self.nivel) & (ahora - self.pico_t >= self.PICO_ESPERA_S)
        if cae.any():
            antes = np.round(self.pico)
            self.pico[cae] = np.maximum(self.nivel[cae], self.pico[cae] - self.PICO_CAIDA * dt_)
            if not np.array_equal(antes, np.round(self.pico)):      # solo se redibuja si cambió un segmento
                self._pintar()
        if not np.any(self.pico > self.nivel):
            self.timer_pico.stop()
            self._t_animacion = None

    def limpiar(self):
        self.nivel[:] = 0
        self.pico[:] = 0
        self.timer_pico.stop()
        self._t_animacion = None
        self._pintar()
        self.linea_k.setData([], [])
        self.linea_oido.setData([], [])
        self.lbl.setText("")
        self.lbl_falta.hide()
        self.b_capa.hide()
        self.fila_niveles.hide()
        self.lbl_niveles.hide()

    def mostrar(self, energia, texto, animar=True):
        """La energía de cada banda en los bloques. `animar`: el pico cae lento (en vivo); si no, queda en el
        nivel (una sesión guardada)."""
        e = np.asarray(energia, dtype=float)
        if not len(e) or not np.any(e > 0):
            self.limpiar()
            self.lbl.setText(texto or "Sin música en este momento.")
            return
        db_ = 10 * np.log10(np.maximum(e, 1e-30))
        self.nivel = np.clip(self._a_bloques(db_), 0, self.BLOQUES)
        ahora = self._ahora()
        sube = (self.nivel >= self.pico) | (not animar)
        self.pico[sube] = self.nivel[sube]
        self.pico_t[sube] = ahora
        self._pintar()
        if animar and np.any(self.pico > self.nivel) and not self.timer_pico.isActive():
            self._t_animacion = ahora
            self.timer_pico.start()
        x = np.arange(bandas.N_BANDAS)
        tope = self.BLOQUES + 0.15
        self.linea_k.setData(x, np.clip(self._a_bloques(db_ + self.rel_k), 0, tope))
        oido = self._a_bloques(db_ + self.rel_oido)
        ok = ~np.isnan(oido)
        self.linea_oido.setData(x[ok], np.clip(oido[ok], 0, tope))
        self.lbl.setText(texto)
        self.lbl_falta.hide()
        self.b_capa.hide()

    def _mostrar_niveles(self, stats, guardada, carpeta):
        nivel, phon = stats.get("nivel_escucha_db_spl"), stats.get("phon_indice")
        if vacio(nivel) or vacio(phon):
            return
        self.sp_nivel.setValue(float(nivel))
        self.sp_phon.setValue(float(phon))
        self.fila_niveles.show()
        g = guardada or {}
        otra = g and (float(g.get("nivel", nivel)) != float(nivel) or float(g.get("phon", phon)) != float(phon))
        if otra:
            self.lbl_niveles.setText(f"Vista recalculada a {num(nivel, 0)} dB SPL y {num(phon, 0)} phon; la sesión tiene "
                                     f"guardados {num(g.get('nivel'), 0)} dB SPL y {num(g.get('phon'), 0)} phon.")
            self.lbl_niveles.setStyleSheet(f"color:{AMARILLO}")
        else:
            self.lbl_niveles.setText("Los niveles guardados en la sesión.")
            self.lbl_niveles.setStyleSheet(f"color:{GRIS}")
        self.lbl_niveles.show()
        self.b_guardar.setEnabled(bool(carpeta) and sesiones.editable(carpeta))

    def falta(self, texto, con_boton):
        self.limpiar()
        self.lbl_falta.setText(texto)
        self.lbl_falta.show()
        self.b_capa.setVisible(con_boton)

    def mostrar_sesion(self, canciones, carpeta, stats=None, guardada=None):
        """El promedio de las canciones válidas de una sesión guardada, o qué falta para tenerlo. `stats`:
        las estadísticas con que se calculó (nivel de escucha y phon); `guardada`: los niveles que la sesión
        tiene guardados (si son otros, la vista está recalculada y lo dice)."""
        stats = stats or {}
        self.poner_phon(stats.get("phon_indice"))
        con = [p for p in canciones
               if p.get("espectro") and p.get("valida", p.get("por_ahora_valida", not p.get("corta")))]
        if con:
            e = np.mean([p["espectro"] for p in con], axis=0)
            prom = {"reparto": bandas.reparto_regiones(e),
                    "desajuste_db": float(bandas.desajuste_lufs_oido(e, phon=self.phon))}
            d = np.array([p["desajuste_db"] for p in con], dtype=float)
            texto = (f"Promedio de las {len(con)} canciones válidas: {_texto_reparto(prom, False)}\n"
                     f"Desajuste LUFS–oído entre canciones: de {num(d.min(), 1, signo=True)} a "
                     f"{num(d.max(), 1, signo=True, unidad='dB')} (desv. {num(d.std(ddof=1) if len(d) > 1 else 0, 1)} dB)")
            per = [p for p in con if not vacio(p.get("lufs_percibido"))]
            if len(per) > 1:
                dif = np.array([p["lufs_percibido"] - p["lufs"] for p in per])
                texto += (f"\nSonoridad percibida (Zwicker) − LUFS: de {num(dif.min(), 1, signo=True)} a "
                          f"{num(dif.max(), 1, signo=True, unidad='LU')} (desv. {num(dif.std(ddof=1), 1)} LU)")
            self.mostrar(e, texto, animar=False)
            self._mostrar_niveles(stats, guardada, carpeta)
            return
        s = sesiones._json(os.path.join(carpeta, "sesion.json")) or {} if carpeta else {}
        if s.get("audio_archivado"):
            self.falta("Esta sesión no tiene capa de frecuencias y su audio ya fue archivado: no se puede generar.", False)
        elif carpeta and capa.wavs(carpeta, s):
            prog = capa.leer_progreso(carpeta)
            if prog:
                self.falta(f"Generando la capa de frecuencias: {100 * prog['fraccion']:.0f} % ({prog['texto']}).", False)
            else:
                horas = (s.get("segundos_totales") or 0) / 3600
                self.falta(f"Esta sesión no tiene capa de frecuencias. Se puede generar desde su audio "
                           f"(≈ {max(1, round(horas * 2.3))} min, en segundo plano).", True)
        else:
            self.falta("Esta sesión no tiene capa de frecuencias ni audio para generarla.", False)

Y_MIN, Y_MAX = -45.0, 0.0     # el rango vertical con que se abre el gráfico
AYUDA_ZOOM = ("Rueda del mouse: acercar o alejar el tiempo · Ctrl + rueda: acercar o alejar los LUFS\n"
              "Arrastrar: mover el gráfico · Doble clic: restablecer la vista")
AYUDA_GRAFICO = "Clic: ver la canción en la tabla · Rueda: zoom (Ctrl: LUFS) · Doble clic: restablecer la vista"


class VistaTiempo(pg.ViewBox):
    """La rueda acerca y aleja el tiempo (eje horizontal); con Ctrl, los LUFS (eje vertical)."""

    def wheelEvent(self, ev, axis=None):
        if axis is None:
            axis = 1 if ev.modifiers() & Qt.KeyboardModifier.ControlModifier else 0
        super().wheelEvent(ev, axis)


class PestanaVivo(QWidget):
    def __init__(self):
        super().__init__()
        self.inicio_epoch = None
        self.umbral = 3.0
        self.rango = "1 h"
        self.curva_t, self.curva_st = Serie(3600), Serie(3600)
        self.seg_cortes, self.seg_niveles, self.seg_percibidos = [], [], []
        self.pistas = []   # todas las canciones cerradas de la sesión (la tabla muestra solo las últimas 1000)
        self.x_ini, self.x_fin = [], []   # dónde está cada canción en el gráfico (instante del reloj)
        self._techo = None                # techo de la cadena de la sesión (dBFS), si lo tiene (S10)
        self._bajo_mouse = None           # la canción cuyo detalle muestra el tooltip del gráfico
        self.stats_datos = {}
        self.segundos = 0.0
        self.manual = False   # el usuario movió o acercó el gráfico: deja de seguir el rango elegido
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
        self.plot = pg.PlotWidget(viewBox=VistaTiempo(), axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.vista = self.plot.getViewBox()
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
        self.vista.setLimits(yMin=-75, yMax=10, minYRange=3, minXRange=20)
        self.vista.sigRangeChangedManually.connect(self._movido_a_mano)
        self.plot.scene().sigMouseClicked.connect(self._clic_grafico)
        self.plot.scene().sigMouseMoved.connect(self._sobre_grafico)
        self.plot.setLabel("left", "LUFS")
        self.plot.setMinimumHeight(170)
        self.cortes = self.plot.plot(pen=pg.mkPen((140, 140, 140, 110), width=1), connect="pairs")
        self.curva = self.plot.plot(pen=pg.mkPen("#4a78b5", width=1.2))
        self.curva.setDownsampling(auto=True, method="peak")
        self.curva.setClipToView(True)
        self.niveles = self.plot.plot(pen=pg.mkPen("#d1495b", width=2.2), connect="pairs")
        self.percibidos = self.plot.plot(pen=pg.mkPen(COLOR_PERCIBIDO, width=2.2), connect="pairs")
        # la canción en curso: su nivel provisorio, punteado hasta que termine
        self.nivel_actual = self.plot.plot(pen=pg.mkPen("#d1495b", width=2.2, style=Qt.PenStyle.DashLine))
        # la canción elegida en la tabla (o con un clic en el gráfico)
        self.resaltado = pg.LinearRegionItem(values=(0, 1), movable=False, brush=pg.mkBrush(250, 204, 21, 55))
        for borde in self.resaltado.lines:
            borde.setPen(pg.mkPen((217, 160, 0, 160), width=1))
        self.resaltado.setZValue(-10)
        self.resaltado.hide()
        self.plot.addItem(self.resaltado)
        self.pista_actual = None
        leyenda = QLabel("<span style='color:#4a78b5'>━</span> short-term (3 s) &nbsp; "
                         "<span style='color:#d1495b'>━</span> integrado de cada canción &nbsp; "
                         "<span style='color:#999'>│</span> cambio de canción &nbsp; "
                         "<span style='color:#999'>⋯</span> hueco = pausa")
        leyenda.setStyleSheet(f"color:{GRIS}")
        self.chk_percibido = QCheckBox("percibido")
        self.chk_percibido.setChecked(True)
        self.chk_percibido.setStyleSheet(f"color:{COLOR_PERCIBIDO}")
        self.chk_percibido.setToolTip("Sonoridad percibida de cada canción (Zwicker, ISO 532-1) en escala LUFS: el LUFS "
                                      "al que un ruido rosa sonaría igual de fuerte. Necesita la capa de frecuencias.")
        self.chk_percibido.toggled.connect(self.percibidos.setVisible)
        rangos = QHBoxLayout()
        rangos.addWidget(leyenda)
        rangos.addWidget(self.chk_percibido)
        rangos.addStretch()
        for texto, eje in (("Tiempo", 0), ("LUFS", 1)):
            rangos.addWidget(QLabel(texto))
            for signo, factor in (("−", 2.0), ("+", 0.5)):
                b = QToolButton()
                b.setText(signo)
                b.setFixedWidth(26)
                b.setToolTip(f"{'Acercar' if factor < 1 else 'Alejar'} {'el tiempo' if eje == 0 else 'los LUFS'}\n{AYUDA_ZOOM}")
                b.clicked.connect(lambda _=False, eje=eje, factor=factor: self._zoom(eje, factor))
                rangos.addWidget(b)
            rangos.addSpacing(6)
        self.b_restablecer = QPushButton("Restablecer vista")
        self.b_restablecer.setToolTip("Vuelve al rango elegido (1 h, 6 h o toda la sesión) y a la escala vertical inicial. "
                                      "En una grabación en curso, el gráfico vuelve a seguir lo último que se mide.")
        self.b_restablecer.clicked.connect(self.restablecer_vista)
        rangos.addWidget(self.b_restablecer)
        rangos.addSpacing(10)
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
        for c, ayuda in AYUDA_COLUMNAS.items():
            self.tabla.horizontalHeaderItem(c).setToolTip(ayuda)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabla.setAlternatingRowColors(True)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla.setToolTip("Clic en una canción: se marca en el gráfico, con la anterior y la siguiente")
        self.tabla.cellClicked.connect(self._fila_elegida)
        hdr = self.tabla.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        caja_tabla = QGroupBox("Últimas canciones")
        lt = QVBoxLayout(caja_tabla)
        lt.addWidget(self.tabla)

        # promedios de la sesión
        self.stats = {}
        grid = QGridLayout()
        filas = [("canciones", "Canciones válidas"), ("lufs", "LUFS por canción"),
                 ("dispersion", "Dispersión"), ("pct", "Cerca de la mediana"),
                 ("cambios", "Cambios de canción"), ("cuerpo", "Cuerpo y arranque"),
                 ("saltos", "Saltos en el borde"), ("silencio", "Silencio entre canciones"),
                 ("sonoridad", "Sonoridad percibida"), ("tp", "True peak máximo"), ("horas", "Horas con música")]
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
        for k in ("canciones", "dispersion", "pct", "cambios", "cuerpo", "sonoridad"):
            self.stats[k].setWordWrap(True)
        self.stats["cuerpo"].setToolTip(AYUDA_CUERPO)
        self.stats["sonoridad"].setToolTip(AYUDA_SONORIDAD)
        self.stats["canciones"].setToolTip("Solo cuentan las canciones válidas: no cortas, no parciales (la primera, la "
                                           "última y las pegadas a una pausa o corte) y fuera de los tramos excluidos.")
        self.stats["pct"].setToolTip("% de canciones a ±0,5 LU y a ±1 LU de la mediana de la sesión. Entre corchetes, "
                                     "el intervalo de confianza al 95 % (bootstrap).")
        self.stats["cambios"].setToolTip("ΔI: LUFS integrado de la canción nueva − el de la anterior, en los cambios "
                                         "entre canciones válidas.")
        caja_stats = QGroupBox("Promedios de la sesión")
        caja_stats.setLayout(grid)
        caja_stats.setMinimumWidth(400)
        caja_stats.setToolTip("Entre corchetes: intervalo de confianza al 95 % (bootstrap). Solo cuentan las canciones "
                              "válidas y los cambios entre canciones válidas.")

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

        self.panel_frec = PanelFrecuencias()
        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(w_plot)
        vertical.addWidget(self.panel_frec)
        vertical.addWidget(medio)
        vertical.addWidget(caja_log)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        vertical.setStretchFactor(2, 3)
        vertical.setStretchFactor(3, 2)

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
        self.panel_frec.limpiar()
        for item in (self.curva, self.cortes, self.niveles, self.percibidos, self.nivel_actual):
            item.setData([], [])

    def mostrar_sesion(self, snap):
        """Una sesión guardada, reconstruida desde sus archivos (ver sesiones.reconstruir)."""
        self.reiniciar(snap)
        c = snap["cerrada"]
        inicio = dt.datetime.fromtimestamp(snap["inicio_epoch"]).strftime("%d-%m-%Y %H:%M")
        if c["terminada"]:
            estado = "terminada"
        elif c.get("abierta"):
            estado = "cortada: sigue abierta y se retoma sola"
        else:
            estado = "interrumpida (la grabación no se cerró bien)"
        self.lbl_cancion.setText(c["nombre"])
        detalle = (f"Sesión {estado} · empezó el {inicio} · {num(c['horas'], 2)} h grabadas · "
                   f"{c['canciones']} canciones medidas")
        if c["pausas"]:
            detalle += f" · {c['pausas']} pausa{'s' if c['pausas'] != 1 else ''} ({mmss(c['segundos_en_pausa'])} sin grabar)"
        cortes = c.get("cortes") or []
        if cortes:
            detalle += (f" · {len(cortes)} corte{'s' if len(cortes) != 1 else ''} "
                        f"({hms(sum(x['duracion_s'] for x in cortes))} sin grabar)")
        self.lbl_detalle.setText(detalle)
        self.lbl_med.setText("")
        self.bar_mom.poner(float("nan"), "—")
        self.bar_st.poner(float("nan"), "—")
        self.pista_actual = None
        self._poner_stats(snap["estado"].get("sesion") or {})
        self.panel_frec.mostrar_sesion(snap.get("pistas") or [], snap.get("carpeta"), snap["estado"].get("sesion") or {},
                                       snap.get("percepcion_guardada"))
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
        self.seg_cortes, self.seg_niveles, self.seg_percibidos = [], [], []
        self.pistas = []
        self.x_ini, self.x_fin = [], []
        self._bajo_mouse = None
        self._techo = None
        self.resaltado.hide()
        self.stats_datos = {}
        self.segundos = float((snap.get("estado") or {}).get("segundos") or 0)
        self.manual = False
        self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
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
        self.pistas.append(dict(p))
        x0 = float(self._a_reloj(p["inicio_s"], True))
        x1 = x0 + p["duracion_s"]
        self.x_ini.append(x0)
        self.x_fin.append(x1)
        self.seg_cortes += [(x0, Y_MIN), (x0, Y_MAX)]
        if not vacio(p["lufs"]):
            self.seg_niveles += [(x0, p["lufs"]), (x1, p["lufs"])]
        if not vacio(p.get("lufs_percibido")):
            self.seg_percibidos += [(x0, p["lufs_percibido"]), (x1, p["lufs_percibido"])]
        self.sucio = True

        self.tabla.insertRow(0)
        corta = p.get("corta")
        for c, v in enumerate(self._valores(p, x0)):
            it = QTableWidgetItem(v)
            if c >= 2:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if corta or p.get("excluida") or p.get("parcial") or p.get("tras_pausa"):
                it.setForeground(QBrush(QColor("#9ca3af")))
            self.tabla.setItem(0, c, it)
        if not corta and not vacio(p["salto"]) and abs(p["salto"]) > self.umbral:
            it = self.tabla.item(0, 6)
            it.setBackground(QBrush(QColor("#fde2e1" if abs(p["salto"]) > 5 else "#fdf0d5")))
            it.setToolTip(f"Salto de más de {num(self.umbral)} LU respecto de la canción anterior")
        if not corta and not vacio(p.get("salto_cuerpo")) and abs(p["salto_cuerpo"]) > 3:
            it = self.tabla.item(0, 8)
            it.setBackground(QBrush(QColor("#fde2e1" if abs(p["salto_cuerpo"]) > 5 else "#fdf0d5")))
            it.setToolTip("Salto de cuerpo de más de 3 LU: la canción entera suena distinto de la anterior")
        if not vacio(p.get("ln")):
            self.tabla.item(0, 9).setToolTip(f"LN {num(p['ln'], 1)} phon · N5 {num(p.get('n5'), 2)} sone · "
                                             f"mediana {num(p.get('n_mediana'), 2)} sone")
        if not vacio(p["tp"]) and p["tp"] > -1:
            it = self.tabla.item(0, 5)
            it.setForeground(QBrush(QColor(ROJO)))
            it.setToolTip("Sobre −1 dBTP: riesgo de distorsión")
        if self.tabla.rowCount() > 1000:
            self.tabla.setRowCount(1000)

    @staticmethod
    def _nota(p):
        """Por qué una canción no cuenta (o «corte incierto»), como en la tabla."""
        partes = []
        if p.get("corta"):
            partes.append("corta: no cuenta")
        elif p.get("excluida"):
            partes.append("en un tramo excluido: no cuenta")
        elif p.get("tras_pausa"):
            partes.append("empezó tras una pausa o un corte")
        elif p.get("parcial"):
            partes.append("parcial: no cuenta")
        if p.get("corte_incierto"):
            partes.append("corte incierto")
        return " · ".join(partes)

    def _valores(self, p, x0):
        """Una canción en las columnas de la tabla (COLUMNAS). Con techo de cadena, el true peak de una canción que
        llega a él se muestra «≥ 0»: la cadena la recortó y su valor real no se puede medir."""
        nombre = f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}"
        motivo = PestanaVivo._nota({**p, "corte_incierto": False})
        if motivo:
            nombre += f"  ({motivo})"
        if p.get("corte_incierto"):
            nombre += "  · corte incierto"
        tp = "≥ 0" if estudio.en_techo(p, self._techo) else num(p["tp"])
        return [dt.datetime.fromtimestamp(x0).strftime("%H:%M:%S"), nombre, mmss(p["duracion_s"]), num(p["lufs"]),
                num(p["lra"]), tp, num(p["salto"], signo=True), num(p.get("delta_i"), signo=True),
                num(p.get("salto_cuerpo"), signo=True), num(p.get("lufs_percibido")),
                num((p.get("reparto") or {}).get("graves"), 0, unidad="%"), num(p.get("desajuste_db"), 1, signo=True)]

    def _marcar_techo(self):
        """El true peak de las canciones que llegan al techo de la cadena, en las filas que ya están en la tabla."""
        for fila in range(self.tabla.rowCount()):
            i = len(self.pistas) - 1 - fila
            it = self.tabla.item(fila, 5)
            if not 0 <= i < len(self.pistas) or it is None:
                continue
            p = self.pistas[i]
            if estudio.en_techo(p, self._techo):
                it.setText("≥ 0")
                it.setToolTip(f"Su pico llega al techo de la cadena ({num(self._techo, 2)} dBFS): la fuente llegaba a "
                              f"escala completa, así que su true peak es al menos 0 dBTP (medido: {num(p['tp'])} dBTP)")
            else:
                it.setText(num(p["tp"]))

    # ------------------------------------------------------------------ canciones en el gráfico
    def _pista_en(self, x):
        """La canción que está en el instante x del gráfico: su índice en self.pistas, «actual» si es la que
        suena (todavía sin cerrar), o None."""
        i = bisect.bisect_right(self.x_ini, x) - 1
        if i >= 0 and x < self.x_fin[i]:
            return i
        p = self.pista_actual
        if p and self.inicio_epoch is not None and x >= float(self._a_reloj(p["inicio_s"], True)):
            fin = self._a_reloj(self.curva_t.v[-1], False) if len(self.curva_t) else x
            if x <= fin + 1:
                return "actual"
        return None

    def _detalle(self, i):
        """El detalle de una canción para el tooltip del gráfico: lo mismo que su fila de la tabla."""
        if i == "actual":
            p = self.pista_actual
            x0 = float(self._a_reloj(p["inicio_s"], True))
            datos = [("Inicio", dt.datetime.fromtimestamp(x0).strftime("%H:%M:%S")), ("Lleva", mmss(p.get("t_s") or 0)),
                     ("LUFS", num(p.get("lufs"))), ("True peak", num(p.get("tp"))), ("Salto", num(p.get("salto"), signo=True))]
            titulo = f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}"
            nota = "sonando ahora: valores provisorios"
        else:
            p = self.pistas[i]
            v = self._valores(p, self.x_ini[i])
            datos = [(c, v[k]) for k, c in enumerate(COLUMNAS) if k != 1]
            titulo = f"{p.get('artista') or '?'} – {p.get('titulo') or '?'}"
            nota = self._nota(p)
        celdas = "".join(f"<td style='padding:0 6px'><span style='color:{GRIS}'>{_html(c)}</span><br><b>{_html(val)}</b></td>"
                         for c, val in datos)
        album = f"<br><span style='color:{GRIS}'>{_html(p.get('album'))}</span>" if p.get("album") else ""
        nota = f" <span style='color:{GRIS}'>({_html(nota)})</span>" if nota else ""
        return (f"<b>{_html(titulo)}</b>{nota}{album}<table style='margin-top:4px'><tr>{celdas}</tr></table>"
                f"<span style='color:{GRIS}; font-size:8pt'>{AYUDA_GRAFICO}</span>")

    def _sobre_grafico(self, pos):
        if not self.vista.sceneBoundingRect().contains(pos) or not (self.pistas or self.pista_actual):
            if self._bajo_mouse is not None:
                QToolTip.hideText()
                self._bajo_mouse = None
            return
        i = self._pista_en(self.vista.mapSceneToView(pos).x())
        if i is None:
            if self._bajo_mouse is not None:
                QToolTip.hideText()
            self._bajo_mouse = None
            return
        if i != self._bajo_mouse or i == "actual":
            self._bajo_mouse = i
            QToolTip.showText(QCursor.pos(), self._detalle(i), self.plot)

    def _resaltar(self, i):
        if i is None or i == "actual":
            self.resaltado.hide()
            return
        self.resaltado.setRegion((self.x_ini[i], self.x_fin[i]))
        self.resaltado.show()

    def ir_a_tabla(self, i):
        """Selecciona en la tabla la canción i (la tabla va de la última a la primera, hasta 1000 filas)."""
        fila = len(self.pistas) - 1 - i
        if not 0 <= fila < self.tabla.rowCount():
            return False
        self.tabla.selectRow(fila)
        self.tabla.scrollToItem(self.tabla.item(fila, 0), QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def _fila_elegida(self, fila, _col=0):
        """Clic en la tabla: la canción se marca en el gráfico, que muestra también la anterior y la siguiente."""
        i = len(self.pistas) - 1 - fila
        if not 0 <= i < len(self.pistas):
            return
        self._resaltar(i)
        a = self.x_ini[max(0, i - 1)]
        if i + 1 < len(self.pistas):
            b = self.x_fin[i + 1]
        elif self.pista_actual and len(self.curva_t):      # la siguiente es la que suena
            b = max(self.x_fin[i], float(self._a_reloj(self.curva_t.v[-1], False)))
        else:
            b = self.x_fin[i]
        self.manual = True        # deja de seguir lo último, como con un zoom a mano
        self.plot.setXRange(a, b, padding=0.03)

    def agregar_log(self, linea, nivel="INFO"):
        color = {"WARNING": AMARILLO, "ERROR": ROJO, "CRITICAL": ROJO}.get(nivel)
        if color:
            self.registro.appendHtml(f"<span style='color:{color}'>{_html(linea)}</span>")
        else:
            self.registro.appendPlainText(linea)

    def _poner_stats(self, s):
        self.stats_datos = dict(s or {})
        if not s:
            for lbl in self.stats.values():
                lbl.setText("—")
            return
        self.umbral = s.get("umbral", self.umbral)
        ic = s.get("ic") or {}
        n = s.get("canciones", 0)
        partes = [f"{n}"]
        if "unicas" in s:     # (las sesiones de antes de v0.2 guardaban menos datos)
            par, cor = s.get("parciales", 0), s.get("cortas", 0)
            partes = [f"{n} ({s.get('unicas', 0)} distintas)", f"{par} parcial{'es' if par != 1 else ''}",
                      f"{cor} corta{'s' if cor != 1 else ''}"]
            if s.get("excluidas"):
                partes.append(f"{s['excluidas']} excluidas")
        self.stats["canciones"].setText(" · ".join(partes))
        self.stats["lufs"].setText(f"media {num(s.get('media'))} · mediana {num(s.get('mediana'))}")
        self.stats["dispersion"].setText(f"desv. {num(s.get('desv'), 2)} LU{ic_txt(ic.get('desv'), 2)} · "
                                         f"rango {num(s.get('min'))} a {num(s.get('max'))}")
        if "pct_05" in s:
            self.stats["pct"].setText(f"±0,5 LU: {num(s.get('pct_05'), 1, unidad='%')}{ic_txt(ic.get('pct_05'))} · "
                                      f"±1 LU: {num(s.get('pct_1'), 1, unidad='%')}{ic_txt(ic.get('pct_1'))} (n = {n})")
        else:
            self.stats["pct"].setText(f"±2 LU: {num(s.get('pct_2lu'), 0, unidad='%')}")
        if "cambios" in s:
            mas3 = f" · > 3 LU: {num(s.get('pct_dI_3'), 1, unidad='%')}" if "pct_dI_3" in s else ""
            self.stats["cambios"].setText(f"{s.get('cambios', 0)} válidos · |ΔI| > 1 LU: "
                                          f"{num(s.get('pct_dI_1'), 1, unidad='%')}{ic_txt(ic.get('pct_dI_1'))}{mas3}")
        else:
            self.stats["cambios"].setText(f"{s.get('transiciones', 0)}")
        if "pct_cuerpo_3" in s:
            self.stats["cuerpo"].setText(
                f"cuerpo > 3 LU: {num(s.get('pct_cuerpo_3'), 1, unidad='%')}{ic_txt(ic.get('pct_cuerpo_3'))} · "
                f"arranque > 5 LU: {num(s.get('pct_arranque_5'), 1, unidad='%')}")
            self.stats["cuerpo"].setToolTip(
                f"{AYUDA_CUERPO}\n\nSalto de cuerpo: p95 {num(s.get('cuerpo_p95'))} LU en {s.get('cambios_cuerpo', 0)} cambios. "
                f"Subida de arranque: p95 {num(s.get('p95_arranque'))} LU en {s.get('cambios_arranque', 0)} cambios. "
                f"Short-term máximo más de 5 LU sobre la mediana: {num(s.get('pct_stmax_5'), 1, unidad='%')} de las "
                f"canciones.")
            self.stats["silencio"].setText(f"mediana {num(s.get('silencio_mediana_s'), 1, unidad='s')} · "
                                           f"> 2 s: {num(s.get('pct_silencio_2'), 1, unidad='%')}")
        else:
            self.stats["cuerpo"].setText("—")
            self.stats["silencio"].setText(num(s.get("silencio_mediana_s"), 1, unidad="s"))
        if s.get("con_sonoridad"):
            self.stats["sonoridad"].setText(
                f"desv. {num(s.get('percibido_desv'), 2)} LU{ic_txt(ic.get('percibido_desv'), 2)} · LUFS: "
                f"{num(s.get('lufs_desv_con_sonoridad'), 2)} · LN {num(s.get('ln_mediana'), 1)} phon")
            self.stats["sonoridad"].setToolTip(
                f"{AYUDA_SONORIDAD}\n\nEn {s['con_sonoridad']} canciones válidas: desviación de la sonoridad percibida "
                f"{num(s.get('percibido_desv'), 2)} LU contra {num(s.get('lufs_desv_con_sonoridad'), 2)} LU de sus LUFS. "
                f"Percibido − LUFS: mediana {num(s.get('dif_percibido_mediana'), 1, signo=True)} LU. "
                f"LN: mediana {num(s.get('ln_mediana'), 1)} phon, desv. {num(s.get('ln_desv'), 1)} phon.")
        else:
            self.stats["sonoridad"].setText("sin capa de frecuencias")
        por_hora = "" if vacio(s.get("por_hora")) else f" ({num(s.get('por_hora'))} por hora)"
        inciertos = f" · {s['inciertos']} con corte incierto (no cuentan)" if s.get("inciertos") else ""
        self.stats["saltos"].setText(f"{s.get('sobre_umbral', 0)} sobre {num(s.get('umbral'), 0)} LU{por_hora} · "
                                     f"medio {num(s.get('salto_medio'))} · máx {num(s.get('salto_max'))}{inciertos}")
        techo = s.get("techo_cadena")
        if techo:
            self.stats["tp"].setText(f"{num(s.get('tp_max'), unidad='dBTP')} · la cadena no deja pasar los picos de "
                                     f"{num(techo['dbfs'], 2, unidad='dBFS')}")
            self.stats["tp"].setStyleSheet(f"color:{AMARILLO}")
            self.stats["tp"].setToolTip(
                f"Los picos de muestra nunca llegan a 0 dBFS y {techo['bloques']} bloques se amontonan justo bajo "
                f"{num(techo['dbfs'], 2)} dBFS: algo en la cadena los limita (un volumen bajo 100 % en Windows, en la app "
                f"o en el cable, o un limitador). {techo['canciones']} canciones llegan a ese techo: su true peak real no "
                "se puede medir y la tabla lo muestra «≥ 0». Si es un volumen, todos los LUFS quedan esa misma cantidad "
                "más bajos (igual para todas las canciones y servicios).")
        else:
            self.stats["tp"].setText(num(s.get("tp_max"), unidad="dBTP"))
            self.stats["tp"].setStyleSheet("")
            self.stats["tp"].setToolTip("")
        nuevo = techo["dbfs"] if techo else None
        if nuevo != self._techo:
            self._techo = nuevo
            self._marcar_techo()
        horas = num(s.get("horas_musica"), 2)
        if not vacio(s.get("horas_validas")) and s.get("excluidas"):
            horas += f" · {num(s.get('horas_validas'), 2)} h fuera de tramos excluidos"
        self.stats["horas"].setText(horas)

    def actualizar(self, e):
        p = e.get("pista")
        self.pista_actual = p
        self.segundos = float(e.get("segundos") or self.segundos)
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
        frec = e.get("frecuencias")
        self.panel_frec.poner_phon((e.get("sesion") or {}).get("phon_indice"))
        if pausado:
            self.panel_frec.mostrar([], "En pausa: no se mide.")
        elif frec:
            c = frec.get("cancion")
            texto = ("Canción en curso: " + _texto_reparto(c)) if c else "Últimos 3 s (todavía sin canción identificada)."
            son = frec.get("cancion_son")
            if son:
                texto += (f"\nSonoridad (Zwicker): LN {num(son['ln'], 1)} phon = {num(son['lufs_percibido'])} LUFS "
                          f"percibidos (su LUFS: {num((p or {}).get('lufs'))})")
            if not vacio(frec.get("ln_ahora")):
                texto += f"\nAhora: LN {num(frec['ln_ahora'], 1)} phon ({num(frec.get('n_ahora'), 1)} sone)"
            self.panel_frec.mostrar(frec.get("bandas_3s") or [], texto + "\nBloques: los últimos 3 s.")
        elif e.get("grabando"):
            self.panel_frec.falta("Esta grabación no mide la capa de frecuencias (se retomó una sesión de antes de "
                                  "v0.2): se genera al terminar, desde el audio.", False)

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
        self.manual = False
        self.sucio = True
        self.redibujar()

    # ------------------------------------------------------------------ zoom
    def _movido_a_mano(self, *_):
        self.manual = True

    def _clic_grafico(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or not self.vista.sceneBoundingRect().contains(ev.scenePos()):
            return
        if ev.double():
            self.restablecer_vista()
            return
        i = self._pista_en(self.vista.mapSceneToView(ev.scenePos()).x())
        self._resaltar(i)
        if i is None or i == "actual":
            self.tabla.clearSelection()
        else:
            self.ir_a_tabla(i)

    def _zoom(self, eje, factor):
        """Acerca (factor < 1) o aleja (factor > 1) un eje, alrededor del centro de lo que se ve."""
        (x0, x1), (y0, y1) = self.vista.viewRange()
        centro = pg.Point((x0 + x1) / 2, (y0 + y1) / 2)
        self.vista.scaleBy(x=factor if eje == 0 else None, y=factor if eje == 1 else None, center=centro)
        self.manual = True

    def restablecer_vista(self):
        """Vuelve al rango elegido (1 h, 6 h, toda) y a la escala vertical de siempre; en una
        grabación en curso, el gráfico vuelve a seguir lo último."""
        self.manual = False
        self.plot.setYRange(Y_MIN, Y_MAX, padding=0)
        self.sucio = True
        self.redibujar()

    # ------------------------------------------------------------------ exportar
    def datos_exportacion(self, carpeta=None):
        """Lo que va al Excel (ver excel.exportar). Las canciones y la curva son todas las de la sesión,
        no solo las que caben en la tabla; el registro, el archivo completo de la sesión si está."""
        t = self.curva_t.v.copy()
        st = self.curva_st.v.copy()
        epoch = self._a_reloj(t, False) if len(t) else np.zeros(0)
        integrado = np.full(len(t), np.nan)
        pistas = []
        for p in self.pistas:
            d = dict(p)
            d["inicio_epoch"] = float(self._a_reloj(p["inicio_s"], True))
            pistas.append(d)
            if not vacio(p.get("lufs")) and len(t):
                a = np.searchsorted(t, p["inicio_s"], side="right")
                b = np.searchsorted(t, p["inicio_s"] + p["duracion_s"], side="right")
                integrado[a:b] = p["lufs"]
        registro = None
        if carpeta:
            try:
                with open(os.path.join(carpeta, "registro.log"), encoding="utf-8", errors="replace") as f:
                    registro = f.read()
            except OSError:
                pass
            from .. import radio
            alb = radio.albumes(carpeta)    # una radio web: el álbum que la API informó después
            for d in pistas:
                if not d.get("album"):
                    d["album"] = alb.get(f"{d.get('artista', '')} - {d.get('titulo', '')}".strip(), "")
        if registro is None:
            registro = self.registro.toPlainText()
        return {"inicio_epoch": self.inicio_epoch, "horas": self.segundos / 3600, "pistas": pistas,
                "stats": dict(self.stats_datos), "curva": (epoch, t, st, integrado), "registro": registro}

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
        for item, segs in ((self.niveles, self.seg_niveles), (self.percibidos, self.seg_percibidos)):
            if segs:
                n = np.array(segs)
                item.setData(n[:, 0], n[:, 1])
            else:
                item.setData([], [])
        p = self.pista_actual
        if p and not vacio(p.get("lufs")) and len(x):
            self.nivel_actual.setData([float(self._a_reloj(p["inicio_s"], True)), x[-1]], [p["lufs"], p["lufs"]])
        else:
            self.nivel_actual.setData([], [])
        fin = x[-1] if len(x) else self.inicio_epoch + 60
        # no se puede alejar más allá de lo que hay (con un margen)
        margen = 0.02 * (fin - self.inicio_epoch) + 120
        self.vista.setLimits(xMin=self.inicio_epoch - margen, xMax=max(fin, self.inicio_epoch + 60) + margen)
        if not self.manual:
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
        self.normalizacion.addItems(list(NORMALIZACIONES))
        self.normalizacion.setCurrentText(normalizacion_de(cfg.normalizacion_activa, cfg.nivel_normalizacion))
        self.crossfade = QDoubleSpinBox()
        self.crossfade.setRange(0, 12)
        self.crossfade.setSingleStep(0.5)
        self.crossfade.setDecimals(1)
        self.crossfade.setSuffix(" s")
        self.crossfade.setSpecialValueText("sin crossfade")
        self.crossfade.setValue(cfg.crossfade_s)
        self.automix = QComboBox()
        self.automix.addItems(["Sí", "No", "No aplica"])
        self.automix.setCurrentText(cfg.automix)
        self.automix.setToolTip("Automix de Spotify (viene activado por defecto; solo actúa en algunas listas de Spotify)")
        self.volumen_app = QDoubleSpinBox()
        self.volumen_app.setRange(0, 100)
        self.volumen_app.setDecimals(0)
        self.volumen_app.setSuffix(" %")
        self.volumen_app.setValue(cfg.volumen_app_pct)
        self.volumen_app.setToolTip("Volumen dentro de la app o de la zona (Soundtrack). Lo normal: 100 %.")
        self.orden = QComboBox()
        self.orden.addItems(["Fijo", "Aleatorio"])
        self.orden.setCurrentText(cfg.orden)
        self.captura = QLineEdit(cfg.captura_ajustes)
        self.captura.setPlaceholderText("opcional: captura de pantalla con los ajustes del servicio")
        b_captura = QPushButton("Elegir…")
        b_captura.clicked.connect(self._elegir_captura)
        fila_captura = QHBoxLayout()
        fila_captura.addWidget(self.captura, 1)
        fila_captura.addWidget(b_captura)
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
        self.stream_url = QLineEdit(cfg.stream_url)
        self.stream_url.setPlaceholderText("vacío: los títulos se leen del panel multimedia de Windows")
        self.stream_url.setToolTip("Para una radio web que no publica qué suena en Windows (Mystify): la dirección "
                                   "de su stream. El motor lee de ahí los títulos y los ubica en la grabación "
                                   "comparando ese stream con el audio que se graba.")
        self.ajuste_titulo = QDoubleSpinBox()
        self.ajuste_titulo.setRange(-10, 10)
        self.ajuste_titulo.setDecimals(1)
        self.ajuste_titulo.setSingleStep(0.1)
        self.ajuste_titulo.setSuffix(" s")
        self.ajuste_titulo.setValue(cfg.ajuste_titulo_s)
        self.ajuste_titulo.setToolTip("Cuánto después del título suele estar el borde real entre canciones en esta "
                                      "radio (medido). El cambio se anota ahí y el ajuste fino busca el hueco en ±3 s, "
                                      "como con las otras fuentes. Se completa solo al elegir la fuente.")
        fila_stream = QHBoxLayout()
        fila_stream.addWidget(self.stream_url, 1)
        fila_stream.addWidget(QLabel("ajuste"))
        fila_stream.addWidget(self.ajuste_titulo)

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
        self.nivel_escucha = QDoubleSpinBox()
        self.nivel_escucha.setRange(40, 95)
        self.nivel_escucha.setDecimals(0)
        self.nivel_escucha.setSuffix(" dB SPL")
        self.nivel_escucha.setValue(cfg.nivel_escucha_db_spl)
        self.nivel_escucha.setToolTip("Para la sonoridad percibida (Zwicker): a qué nivel suena en la mesa una canción "
                                      "a −14 LUFS. Por defecto 65 dB SPL; se ajusta con una medición en la sala.")
        self.phon_indice = QDoubleSpinBox()
        self.phon_indice.setRange(20, 90)
        self.phon_indice.setDecimals(0)
        self.phon_indice.setSuffix(" phon")
        self.phon_indice.setValue(cfg.phon_indice)
        self.phon_indice.setToolTip("Índice rápido LUFS–oído (columna Desaj. y línea verde del panel Frecuencias): "
                                    "nivel de escucha en phon (ISO 226). Por defecto 60.")

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
        f1.addRow("Automix", self.automix)
        f1.addRow("Volumen de la app o zona", self.volumen_app)
        f1.addRow("Orden de la lista", self.orden)
        f1.addRow("Calidad de streaming", self.calidad)
        f1.addRow("Captura de los ajustes", fila_captura)
        f1.addRow("Notas", self.notas)

        g_cap = QGroupBox("Captura")
        f2 = QFormLayout(g_cap)
        f2.addRow("Grabar desde", fila_disp)
        f2.addRow("App a seguir", fila_app)
        f2.addRow("", self.lbl_apps)
        f2.addRow("Títulos de la radio (stream)", fila_stream)
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
        f3.addRow("Nivel de escucha (−14 LUFS =)", self.nivel_escucha)
        f3.addRow("Índice rápido LUFS–oído a", self.phon_indice)

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

        for w in (self.carpeta, self.playlist, self.calidad, self.etiqueta, self.stream_url, self.captura):
            w.textChanged.connect(self._avisar)
        self.notas.textChanged.connect(self._avisar)
        for w in (self.normalizacion, self.app, self.automix, self.orden):
            w.currentTextChanged.connect(self._avisar)
        for w in (self.dispositivo, self.monitor_disp):
            w.currentIndexChanged.connect(self._avisar)
        for w in (self.horas, self.umbral, self.silencio, self.min_dur, self.ajuste_titulo, self.crossfade,
                  self.volumen_app, self.nivel_escucha, self.phon_indice):
            w.valueChanged.connect(self._avisar)
        self._cargando = False

    def _avisar(self, *_):
        if not self._cargando:
            self.cambio.emit()

    def _elegir_carpeta(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta donde guardar las sesiones", self.carpeta.text())
        if d:
            self.carpeta.setText(os.path.normpath(d))

    def _elegir_captura(self):
        r, _ = QFileDialog.getOpenFileName(self, "Captura de los ajustes del servicio", self.captura.text(),
                                           "Imágenes (*.png *.jpg *.jpeg *.bmp *.webp)")
        if r:
            self.captura.setText(os.path.normpath(r))

    def _fuente_cambio(self, fuente):
        app = APP_POR_FUENTE.get(fuente)
        if app is not None:
            self.app.setCurrentText(app)
        habitual = CONDICIONES_POR_FUENTE.get(fuente)
        if habitual:      # lo habitual de ese servicio (se puede cambiar)
            self.normalizacion.setCurrentText(habitual[0])
            self.automix.setCurrentText(habitual[1])
            self.crossfade.setValue(habitual[2])
        radio = STREAM_POR_FUENTE.get(fuente) or {}
        self.stream_url.setText(radio.get("url", ""))
        self.ajuste_titulo.setValue(float(radio.get("ajuste_s", 0.0)))
        self._avisar()

    def _sugerir_etiqueta(self):
        partes = [self.fuente.currentText().split(" ")[0].lower()]
        activa, nivel = NORMALIZACIONES.get(self.normalizacion.currentText(), (None, ""))
        if activa is True:
            partes.append("norm" + ("" if nivel in ("Normal", "Sin niveles") else "-" + nivel.lower()))
        elif activa is False:
            partes.append("sinnorm")
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
        activa, nivel = NORMALIZACIONES.get(self.normalizacion.currentText(), (True, "Normal"))
        return cfgmod.Config(
            carpeta_salida=self.carpeta.text().strip(), fuente=self.fuente.currentText(),
            playlist=self.playlist.text().strip(), normalizacion=self.normalizacion.currentText(),
            crossfade=f"{self.crossfade.value():g} s", calidad=self.calidad.text().strip(),
            normalizacion_activa=activa, nivel_normalizacion=nivel, crossfade_s=self.crossfade.value(),
            automix=self.automix.currentText(), volumen_app_pct=self.volumen_app.value(),
            orden=self.orden.currentText(), captura_ajustes=self.captura.text().strip(),
            notas=self.notas.toPlainText().strip(), etiqueta=self.etiqueta.text().strip(),
            app=self.app.currentText().strip(), stream_url=self.stream_url.text().strip(),
            ajuste_titulo_s=self.ajuste_titulo.value(),
            dispositivo=self.dispositivo.currentData() or self._disp_guardado,
            horas=self.horas.value(), monitor=self.monitor,
            monitor_dispositivo=(self.monitor_disp.currentData() if self.monitor_disp.count() else self._monitor_guardado) or "",
            monitor_vol=self.monitor_vol, umbral_salto=self.umbral.value(),
            alerta_silencio_min=self.silencio.value(), min_dur=self.min_dur.value(),
            nivel_escucha_db_spl=self.nivel_escucha.value(), phon_indice=self.phon_indice.value(),
            checklist=self.checklist)

    def mostrar(self, c):
        """Muestra la configuración de una sesión: la de una grabación en curso (al reconectarse) o la
        de una guardada."""
        self._cargando = True
        self.carpeta.setText(c.get("carpeta_salida", ""))
        self.fuente.setCurrentText(c.get("fuente", ""))
        self.playlist.setText(c.get("playlist", ""))
        if "nivel_normalizacion" in c:
            self.normalizacion.setCurrentText(normalizacion_de(c.get("normalizacion_activa"), c.get("nivel_normalizacion")))
        else:          # una sesión de antes de v0.2: la normalización estaba como texto
            t = (c.get("normalizacion") or "").lower()
            self.normalizacion.setCurrentText(
                "Desactivada" if t.startswith("desact") else "No aplica" if "no aplica" in t
                else "Activada · Fuerte" if ("alto" in t or "fuerte" in t) else "Activada · Silencioso" if "silen" in t
                else "Activada (sin niveles)" if (t and c.get("fuente") == "Soundtrack") else "Activada · Normal")
        if "crossfade_s" in c:
            self.crossfade.setValue(float(c.get("crossfade_s") or 0))
        else:
            import re as _re
            m = _re.search(r"\d+(?:[.,]\d+)?", c.get("crossfade") or "")
            self.crossfade.setValue(float(m.group(0).replace(",", ".")) if m else 0.0)
        self.automix.setCurrentText(c.get("automix") or "No aplica")
        self.volumen_app.setValue(float(c.get("volumen_app_pct") if c.get("volumen_app_pct") is not None else 100))
        self.orden.setCurrentText(c.get("orden") or "Fijo")
        self.captura.setText(c.get("captura_ajustes", ""))
        self.calidad.setText(c.get("calidad", ""))
        self.notas.setPlainText(c.get("notas", ""))
        self.etiqueta.setText(c.get("etiqueta", ""))
        self.app.setCurrentText(c.get("app", ""))
        self.stream_url.setText(c.get("stream_url", ""))
        self.ajuste_titulo.setValue(float(c.get("ajuste_titulo_s") or 0.0))
        self.horas.setValue(float(c.get("horas") or 0))
        self.umbral.setValue(float(c.get("umbral_salto") or self.umbral.value()))
        self.silencio.setValue(float(c.get("alerta_silencio_min") or self.silencio.value()))
        self.min_dur.setValue(float(c.get("min_dur") or self.min_dur.value()))
        self.nivel_escucha.setValue(float(c.get("nivel_escucha_db_spl") or self.nivel_escucha.value()))
        self.phon_indice.setValue(float(c.get("phon_indice") or self.phon_indice.value()))
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
    "Volumen de la app o de la zona anotado (con captura de pantalla).",
    "Crossfade y Automix anotados.",
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
            r = {"disp": dispositivos.listar(), "apps": metadata.listar_apps(),
                 "libre": sistema.espacio_libre_gb(cfg.carpeta_salida)}
            if cfg.stream_url:
                from .. import radio
                try:
                    r["radio"] = ("ok", radio.probar(cfg.stream_url))
                except Exception as e:
                    r["radio"] = ("mal", str(e))
            return r

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
        if cfg.stream_url:
            estado, texto = r.get("radio", ("mal", "sin respuesta"))
            if estado == "ok":
                self._fila("ok", f"La radio manda los títulos en su stream (suena: {texto}). Se ubican en la "
                                 "grabación comparando ese stream con el audio que llega al cable.")
            else:
                self._fila("mal", f"No pude leer los títulos del stream de la radio ({cfg.stream_url}): {texto}")
        elif apps is None:
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
        self.resize(980, 460)
        self.elegida = None
        self.filas = sesiones.listar(base)

        self.base = base
        self.tabla = QTableWidget(len(self.filas), 7)
        self.tabla.setHorizontalHeaderLabels(["Sesión", "Empezó", "Horas", "Canciones", "Estado", "Estudio",
                                              "Audio y frecuencias"])
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
            for c, v in enumerate([carpeta, empezo, num(s["horas"], 2), str(s["canciones"]), s["estado"],
                                   s.get("estudio") or "—", capa.estado_audio(s["carpeta"])]):
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
        self.b_estudio = QPushButton("Datos del estudio…")
        self.b_estudio.setToolTip("Servicio, lista, si vale para el estudio, tramos excluidos y notas (queda registro "
                                  "de cada cambio)")
        self.b_estudio.clicked.connect(self._editar_estudio)
        b_catalogo = QPushButton("Cargar catálogo…")
        b_catalogo.setToolTip("Carga en las sesiones de esta carpeta lo que dice un catálogo del estudio "
                              "(catalogo_sesiones.json). No toca las que ya tienen datos del estudio.")
        b_catalogo.clicked.connect(self._cargar_catalogo)
        self.b_archivo = QPushButton("Frecuencias y archivo…")
        self.b_archivo.setToolTip("Verificar la sesión contra su audio, generar su capa de frecuencias y archivarla "
                                  "(eliminar el audio con licencia)")
        self.b_archivo.clicked.connect(self._archivo)
        b_todas = QPushButton("Procesar todas…")
        b_todas.setToolTip("Verifica y genera la capa de frecuencias de todas las sesiones terminadas que la necesiten, "
                           "en segundo plano")
        b_todas.clicked.connect(self._procesar_todas)
        b_comparar = QPushButton("Comparar sesiones…")
        b_comparar.setToolTip("Compara las sesiones terminadas por sesión y por servicio, con intervalos, y guarda un Excel")
        b_comparar.clicked.connect(self._comparar)
        b_cerrar = QPushButton("Cerrar")
        b_cerrar.clicked.connect(self.reject)
        botones = QHBoxLayout()
        botones.addWidget(b_otra)
        botones.addWidget(self.b_estudio)
        botones.addWidget(b_catalogo)
        botones.addWidget(self.b_archivo)
        botones.addWidget(b_todas)
        botones.addWidget(b_comparar)
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
        self.b_estudio.setEnabled(bool(filas) and sesiones.editable(self.filas[filas[0].row()]["carpeta"]))
        self.b_archivo.setEnabled(bool(filas) and self.filas[filas[0].row()]["estado"] == "terminada")
        self.b_abrir.setToolTip("" if ok or not filas else "Se está grabando: la ventana se conecta sola a esa grabación.")

    def _abrir(self):
        filas = self.tabla.selectionModel().selectedRows()
        if filas and self.filas[filas[0].row()]["estado"] != "en curso":
            self.elegida = self.filas[filas[0].row()]["carpeta"]
            self.accept()

    def _editar_estudio(self):
        filas = self.tabla.selectionModel().selectedRows()
        if not filas:
            return
        s = self.filas[filas[0].row()]
        d = DialogoEstudio(self, s["carpeta"])
        if d.exec() == QDialog.DialogCode.Accepted:
            self._refrescar()

    def _archivo(self):
        filas = self.tabla.selectionModel().selectedRows()
        if filas:
            DialogoArchivo(self, self.filas[filas[0].row()]["carpeta"]).exec()
            self._refrescar()

    def _comparar(self):
        DialogoComparar(self, self.base).exec()
        self._refrescar()

    def _procesar_todas(self):
        lista = capa.pendientes(self.base)
        if not lista:
            QMessageBox.information(self, "Capa de frecuencias", "Todas las sesiones terminadas con audio ya tienen su "
                                                                 "capa de frecuencias verificada.")
            return
        horas = sum((sesiones._json(os.path.join(c, "sesion.json")) or {}).get("segundos_totales", 0) for c in lista) / 3600
        r = QMessageBox.question(self, "Procesar todas",
                                 f"Hay {len(lista)} sesiones sin capa de frecuencias verificada ({horas:.0f} h de audio).\n\n"
                                 f"Se procesan en segundo plano, de a una y con prioridad baja (≈ {horas * 2.3 / 60:.1f} h): "
                                 "puedes seguir usando la app y grabando. ¿Empezar?")
        if r == QMessageBox.StandardButton.Yes:
            sistema.lanzar_desacoplado(sistema.comando_propio("--capa", "--todas", self.base))

    def _cargar_catalogo(self):
        r, _ = QFileDialog.getOpenFileName(self, "Catálogo del estudio", self.base, "Catálogo (*.json)")
        if not r:
            return
        try:
            with open(r, encoding="utf-8") as f:
                cat = json.load(f)
            res = sesiones.importar_catalogo(self.base, cat)
        except Exception as e:
            QMessageBox.warning(self, "No pude cargar el catálogo", str(e))
            return
        QMessageBox.information(self, "Catálogo del estudio", "\n".join(f"{n}: {t}" for n, t in res) or "Sin sesiones.")
        self._refrescar()

    def _refrescar(self):
        actual = self.tabla.currentRow()
        self.filas = sesiones.listar(self.base)
        for i, s in enumerate(self.filas):
            if i < self.tabla.rowCount():
                self.tabla.setItem(i, 5, QTableWidgetItem(s.get("estudio") or "—"))
                self.tabla.setItem(i, 6, QTableWidgetItem(capa.estado_audio(s["carpeta"])))
        self.tabla.selectRow(max(0, actual))

    def _otra(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de la sesión", os.path.dirname(os.path.normpath(self.filas[0]["carpeta"])) if self.filas else "")
        if not d:
            return
        if not sesiones.es_sesion(d):
            QMessageBox.warning(self, "No es una sesión", f"«{d}» no tiene un sesion.json: no es una sesión de Loudness Lab.")
            return
        self.elegida = os.path.normpath(d)
        self.accept()


def _hms_a_s(t):
    """«h:mm:ss», «mm:ss» o segundos → segundos."""
    partes = [float(x.replace(",", ".")) for x in str(t).strip().split(":")]
    s = 0.0
    for x in partes:
        s = s * 60 + x
    return s


class Avance(QObject):
    """El avance de una tarea en otro hilo, hacia la ventana (la señal cruza de hilo sola)."""
    paso = Signal(float, str)


class DialogoComparar(QDialog):
    """El comparador (comparador.py): las sesiones terminadas, cuáles entran según sus datos del estudio, y al
    comparar, el Excel y el resumen por servicio."""

    COLS = ["Sesión", "Servicio", "Estudio", "Horas", "Capa", "Aviso"]

    def __init__(self, padre, base):
        super().__init__(padre)
        self.base = base
        self.ruta = None
        self.avance = Avance()
        self.avance.paso.connect(self._avance)
        self.setWindowTitle("Comparar sesiones")
        self.resize(980, 640)
        ayuda = QLabel("Marca las sesiones a comparar. Cuentan según sus datos del estudio: el servicio y si vale "
                       "(válida o parcial; solo las válidas entran al promedio de su servicio). Las que no los tienen "
                       "no entran: complétalos con «Datos del estudio…».")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet(f"color:{GRIS}")
        self.tabla = QTableWidget(0, len(self.COLS))
        self.tabla.setHorizontalHeaderLabels(self.COLS)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        hdr = self.tabla.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.b_estudio = QPushButton("Datos del estudio…")
        self.b_estudio.setToolTip("Completa o corrige los datos del estudio de la sesión elegida")
        self.b_estudio.clicked.connect(self._editar_estudio)
        self.barra = QProgressBar()
        self.barra.setRange(0, 1000)
        self.barra.hide()
        self.lbl_avance = QLabel("")
        self.lbl_avance.setWordWrap(True)
        self.lbl_avance.setStyleSheet(f"color:{GRIS}")
        self.resultado = QTableWidget(0, 8)
        self.resultado.setHorizontalHeaderLabels(["Servicio", "Sesiones", "Canciones", "Desv. LUFS [IC 95 %]",
                                                  "± 0,5 LU", "|ΔI| > 1 LU", "Cuerpo > 3 LU", "Percibido: desv. (LUFS)"])
        self.resultado.verticalHeader().setVisible(False)
        self.resultado.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.resultado.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.resultado.horizontalHeaderItem(7).setToolTip(
            "Desviación de la sonoridad percibida (Zwicker) entre canciones, en escala LUFS, contra la de sus LUFS en "
            "las mismas canciones (las que tienen capa de frecuencias)")
        self.resultado.setMaximumHeight(130)
        self.resultado.hide()
        self.b_comparar = QPushButton("Comparar y guardar Excel…")
        self.b_comparar.setDefault(True)
        self.b_comparar.clicked.connect(self._comparar)
        self.b_abrir = QPushButton("Abrir el Excel")
        self.b_abrir.clicked.connect(lambda: self.ruta and sistema.abrir(self.ruta))
        self.b_abrir.hide()
        cerrar = QPushButton("Cerrar")
        cerrar.clicked.connect(self.accept)
        pie = QHBoxLayout()
        pie.addWidget(self.b_estudio)
        pie.addStretch()
        pie.addWidget(self.b_abrir)
        pie.addWidget(self.b_comparar)
        pie.addWidget(cerrar)
        lay = QVBoxLayout(self)
        lay.addWidget(ayuda)
        lay.addWidget(self.tabla, 1)
        lay.addWidget(self.barra)
        lay.addWidget(self.lbl_avance)
        lay.addWidget(self.resultado)
        lay.addLayout(pie)
        self._llenar()

    def _llenar(self):
        marcadas = {self.cand[i]["carpeta"] for i in range(self.tabla.rowCount())
                    if self.tabla.item(i, 0).checkState() == Qt.CheckState.Checked} if self.tabla.rowCount() else None
        self.cand = comparador.candidatas(self.base)
        self.tabla.setRowCount(len(self.cand))
        nombres = {"valida": "válida", "parcial": "parcial", "no_valida": "no válida"}
        for i, c in enumerate(self.cand):
            it = QTableWidgetItem(c["nombre"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            marcada = (c["carpeta"] in marcadas) if marcadas is not None else not c["falta"]
            it.setCheckState(Qt.CheckState.Checked if marcada and not c["falta"] else Qt.CheckState.Unchecked)
            valores = [it, QTableWidgetItem(c["servicio"] or "—"), QTableWidgetItem(nombres.get(c["estado"], "—")),
                       QTableWidgetItem(num(c["horas"], 2)), QTableWidgetItem("sí" if c["capa"] else "no"),
                       QTableWidgetItem(c["falta"])]
            for j, v in enumerate(valores):
                if j == 3:
                    v.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if c["falta"]:
                    v.setForeground(QBrush(QColor(AMARILLO if j == 5 else "#9ca3af")))
                self.tabla.setItem(i, j, v)

    def elegidas(self):
        return [self.cand[i]["carpeta"] for i in range(self.tabla.rowCount())
                if self.tabla.item(i, 0).checkState() == Qt.CheckState.Checked]

    def _editar_estudio(self):
        filas = self.tabla.selectionModel().selectedRows()
        if not filas:
            QMessageBox.information(self, "Datos del estudio", "Elige una sesión de la lista.")
            return
        carpeta = self.cand[filas[0].row()]["carpeta"]
        if DialogoEstudio(self, carpeta).exec() == QDialog.DialogCode.Accepted:
            self._llenar()

    def _comparar(self):
        carpetas = self.elegidas()
        if not carpetas:
            QMessageBox.information(self, "Comparar sesiones", "Marca al menos una sesión con sus datos del estudio.")
            return
        ruta, _ = QFileDialog.getSaveFileName(self, "Guardar la comparación",
                                              os.path.join(self.base, "comparacion_servicios.xlsx"), "Excel (*.xlsx)")
        if not ruta:
            return
        self.ruta = ruta
        self.b_comparar.setEnabled(False)
        self.b_abrir.hide()
        self.resultado.hide()
        self.barra.setValue(0)
        self.barra.show()
        self.lbl_avance.setText("Empezando…")
        Tarea(comparador.comparar_y_escribir, carpetas, ruta, self.avance.paso.emit).iniciar(self._listo, self._fallo)

    def _avance(self, f, texto):
        self.barra.setValue(int(1000 * f))
        self.lbl_avance.setText(f"Comparando: {texto}")

    def _listo(self, r):
        self.barra.hide()
        self.b_comparar.setEnabled(True)
        self.b_abrir.show()
        servicios = [a for a in r["servicios"] if a.get("canciones")]
        self.resultado.setRowCount(len(servicios))
        for i, a in enumerate(servicios):
            ic = a.get("desv_ic") or (float("nan"), float("nan"))
            per = (f"{num(a.get('desv_percibido'), 2)} ({num(a.get('desv_lufs_con_sonoridad'), 2)})"
                   if not vacio(a.get("desv_percibido")) else "sin capa")
            for j, v in enumerate([a["servicio"], str(a.get("sesiones", "")), str(a["canciones"]),
                                   f"{num(a.get('desv'), 2)} [{num(ic[0], 2)}–{num(ic[1], 2)}]",
                                   num(a.get("pct_05"), 1, unidad="%"), num(a.get("pct_dI_1"), 1, unidad="%"),
                                   num(a.get("pct_cuerpo_3"), 1, unidad="%"), per]):
                it = QTableWidgetItem(v)
                if j:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.resultado.setItem(i, j, it)
        self.resultado.setVisible(bool(servicios))
        n = len(r["sesiones"])
        txt = f"Listo: {n} {'sesión comparada' if n == 1 else 'sesiones comparadas'}. Excel en {self.ruta}"
        if r["omitidas"]:
            txt += "\nNo entraron: " + "; ".join(f"{n} ({m})" for n, m in r["omitidas"])
        if not servicios:
            txt += "\nNinguna sesión válida: no hay promedios por servicio."
        self.lbl_avance.setText(txt)

    def _fallo(self, msg):
        self.barra.hide()
        self.b_comparar.setEnabled(True)
        self.lbl_avance.setText("")
        QMessageBox.warning(self, "No se pudo comparar", f"{msg}\n\nSi el Excel está abierto, ciérralo y vuelve a intentar.")


class DialogoArchivo(QDialog):
    """Verificar una sesión contra su audio, generar su capa de frecuencias y archivarla (eliminar el audio
    con licencia). El proceso pesado corre aparte (LoudnessLab.exe --capa) y aquí se ve su avance."""

    def __init__(self, padre, carpeta):
        super().__init__(padre)
        self.carpeta = carpeta
        self.proc = None
        self.setWindowTitle(f"Frecuencias y archivo · {os.path.basename(os.path.normpath(carpeta))}")
        self.resize(640, 380)
        self.lbl_req = QLabel("")
        self.lbl_req.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_req.setWordWrap(True)
        self.barra = QProgressBar()
        self.barra.setRange(0, 1000)
        self.barra.hide()
        self.lbl_avance = QLabel("")
        self.lbl_avance.setStyleSheet(f"color:{GRIS}")
        ayuda = QLabel("Archivar elimina los WAV de forma permanente (no van a la papelera). Quedan las mediciones, la "
                       "capa de frecuencias, los títulos y los recortes de picos/: la sesión se sigue abriendo, "
                       "exportando y comparando igual.")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet(f"color:{GRIS}")
        self.b_capa = QPushButton("Verificar y generar capa de frecuencias")
        self.b_capa.clicked.connect(self._procesar)
        self.b_archivar = QPushButton("Archivar: eliminar el audio…")
        self.b_archivar.clicked.connect(self._archivar)
        cerrar = QPushButton("Cerrar")
        cerrar.clicked.connect(self.accept)
        pie = QHBoxLayout()
        pie.addWidget(self.b_capa)
        pie.addWidget(self.b_archivar)
        pie.addStretch()
        pie.addWidget(cerrar)
        lay = QVBoxLayout(self)
        lay.addWidget(self.lbl_req)
        lay.addWidget(self.barra)
        lay.addWidget(self.lbl_avance)
        lay.addWidget(ayuda)
        lay.addStretch()
        lay.addLayout(pie)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refrescar)
        self.timer.start(700)
        self._refrescar()

    def _refrescar(self):
        req = capa.requisitos(self.carpeta)
        filas = [f"<span style='color:{VERDE if ok else ROJO}'>{'✓' if ok else '✗'}</span> {texto}"
                 + (f" <span style='color:{GRIS}'>· {_html(det)}</span>" if det else "") for _, texto, ok, det in req]
        self.lbl_req.setText("<br>".join(filas))
        prog = capa.leer_progreso(self.carpeta)
        corriendo = bool(prog) or (self.proc is not None and self.proc.poll() is None)
        if prog:
            self.barra.show()
            self.barra.setValue(int(1000 * prog.get("fraccion", 0)))
            self.lbl_avance.setText(prog.get("texto", ""))
        elif self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self.barra.hide()
            s = sesiones._json(os.path.join(self.carpeta, "sesion.json")) or {}
            v = s.get("verificacion") or {}
            self.lbl_avance.setText(f"Terminó: {'todo calza con el audio' if v.get('ok') else 'hay diferencias'} "
                                    f"(mediciones {v.get('mediciones_dif_max_db')} dB; espectro {v.get('espectro')})." if v
                                    else "El proceso terminó sin dejar resultado: revisa motor.log.")
        listo = dict((k, ok) for k, _, ok, _ in req)
        self.b_capa.setEnabled(not corriendo and listo.get("terminada") and listo.get("audio"))
        self.b_capa.setText("Verificar de nuevo" if listo.get("espectro") else "Verificar y generar capa de frecuencias")
        self.b_archivar.setEnabled(not corriendo and all(listo.values()))
        gb = sum(os.path.getsize(r) for r in capa.wavs(self.carpeta)) / 1e9
        self.b_archivar.setText(f"Archivar: eliminar el audio ({num(gb, 1)} GB)…" if gb else "Archivar: eliminar el audio…")

    def _procesar(self):
        self.proc = sistema.lanzar_desacoplado(sistema.comando_propio("--capa", self.carpeta))
        self.lbl_avance.setText("Empezando… (puedes cerrar esta ventana: sigue en segundo plano)")
        self.b_capa.setEnabled(False)

    def _archivar(self):
        rutas = capa.wavs(self.carpeta)
        gb = sum(os.path.getsize(r) for r in rutas) / 1e9
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Archivar la sesión")
        caja.setText(f"¿Eliminar el audio de «{os.path.basename(os.path.normpath(self.carpeta))}»?")
        caja.setInformativeText(f"Se eliminan {len(rutas)} archivos WAV ({gb:.1f} GB) de forma permanente: no van a la "
                                "papelera y no se pueden recuperar. Las mediciones, la capa de frecuencias, los títulos "
                                "y los recortes quedan.")
        si = caja.addButton("Eliminar el audio", QMessageBox.ButtonRole.DestructiveRole)
        no = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(no)
        caja.exec()
        if caja.clickedButton() is not si:
            return
        try:
            libres = capa.archivar(self.carpeta)
        except Exception as e:
            QMessageBox.warning(self, "No se archivó", str(e))
            return
        self.lbl_avance.setText(f"Archivada: se liberaron {libres / 1e9:.1f} GB.")
        self._refrescar()

    def done(self, r):
        self.timer.stop()
        super().done(r)


class DialogoEstudio(QDialog):
    """Lo que el estudio dice de una sesión terminada: servicio, lista, estado, tramos excluidos y notas.
    Cada cambio queda registrado en sesion.json («ediciones»)."""

    def __init__(self, padre, carpeta):
        super().__init__(padre)
        self.carpeta = carpeta
        s = sesiones._json(os.path.join(carpeta, "sesion.json")) or {}
        e = sesiones.estudio_de(s)
        cond = s.get("condiciones") or {}
        self.setWindowTitle(f"Datos del estudio · {os.path.basename(os.path.normpath(carpeta))}")
        self.resize(640, 460)
        self.servicio = QComboBox()
        self.servicio.addItems(["Soundtrack", "Spotify", "Mystify", "Otro"])
        self.servicio.setCurrentText(e.get("servicio") or cond.get("servicio") or servicio_de(cond.get("fuente")))
        self.playlist = QLineEdit(e.get("playlist", cond.get("playlist", "")))
        self.estado = QComboBox()
        for k, t in (("valida", "Válida"), ("parcial", "Parcial (solo métricas globales)"), ("no_valida", "No válida")):
            self.estado.addItem(t, k)
        self.estado.setCurrentIndex(max(0, self.estado.findData(e.get("estado") or "valida")))
        self.notas = QPlainTextEdit(e.get("notas", ""))
        self.notas.setFixedHeight(60)
        self.tramos = QTableWidget(0, 3)
        self.tramos.setHorizontalHeaderLabels(["Desde (h:mm:ss de audio)", "Hasta", "Motivo"])
        self.tramos.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tramos.verticalHeader().setVisible(False)
        for t in e.get("tramos_excluidos") or []:
            self._fila_tramo(hms(t["desde_s"]) if t["desde_s"] % 1 == 0 else f"{t['desde_s']:.1f}",
                             hms(t["hasta_s"]) if t["hasta_s"] % 1 == 0 else f"{t['hasta_s']:.1f}", t.get("motivo", ""))
        b_mas = QPushButton("Agregar tramo")
        b_mas.clicked.connect(lambda: self._fila_tramo("0:00:00", "0:00:00", ""))
        b_menos = QPushButton("Quitar tramo")
        b_menos.clicked.connect(lambda: self.tramos.removeRow(self.tramos.currentRow()) if self.tramos.currentRow() >= 0 else None)
        self.sin_musica = [t for t in (s.get("tramos_sin_musica") or []) if t.get("hasta_s") is not None]
        self.b_sin_musica = QPushButton(f"Agregar los tramos sin música ({len(self.sin_musica)})")
        self.b_sin_musica.setToolTip("Los tramos en que no llegó música que detectó la grabación (más de los minutos de "
                                     "«Avisar si hay silencio por»), con su causa")
        self.b_sin_musica.setEnabled(bool(self.sin_musica))
        self.b_sin_musica.clicked.connect(self._agregar_sin_musica)
        ediciones = s.get("ediciones") or []
        historial = QLabel(f"{len(ediciones)} cambios registrados; el último: {ediciones[-1]['hora']}" if ediciones
                           else "Sin cambios registrados.")
        historial.setStyleSheet(f"color:{GRIS}")
        form = QFormLayout()
        form.addRow("Servicio", self.servicio)
        form.addRow("Lista", self.playlist)
        form.addRow("Para el estudio", self.estado)
        form.addRow("Notas", self.notas)
        fila = QHBoxLayout()
        fila.addWidget(b_mas)
        fila.addWidget(b_menos)
        fila.addWidget(self.b_sin_musica)
        fila.addStretch()
        ayuda = QLabel("Tramos excluidos: partes de la grabación que no cuentan (por ejemplo, un error de operación). "
                       "En tiempo de audio de la sesión (el cronómetro de la grabación), no en hora del PC.")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet(f"color:{GRIS}")
        ok, cancelar = QPushButton("Guardar"), QPushButton("Cancelar")
        ok.setDefault(True)
        ok.clicked.connect(self._guardar)
        cancelar.clicked.connect(self.reject)
        pie = QHBoxLayout()
        pie.addWidget(historial)
        pie.addStretch()
        pie.addWidget(cancelar)
        pie.addWidget(ok)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("Tramos excluidos"))
        lay.addWidget(ayuda)
        lay.addWidget(self.tramos, 1)
        lay.addLayout(fila)
        lay.addLayout(pie)

    @staticmethod
    def _texto_tiempo(x):
        return hms(x) if float(x) % 1 == 0 else f"{float(x):.1f}"

    def _agregar_sin_musica(self):
        ya = {(self.tramos.item(i, 0).text(), self.tramos.item(i, 1).text()) for i in range(self.tramos.rowCount())}
        for t in self.sin_musica:
            d, h = self._texto_tiempo(t["desde_s"]), self._texto_tiempo(t["hasta_s"])
            if (d, h) not in ya:
                self._fila_tramo(d, h, f"sin música: {t.get('causa', '')}")

    def _fila_tramo(self, desde, hasta, motivo):
        i = self.tramos.rowCount()
        self.tramos.insertRow(i)
        for c, v in enumerate((desde, hasta, motivo)):
            self.tramos.setItem(i, c, QTableWidgetItem(str(v)))

    def _guardar(self):
        tramos = []
        try:
            for i in range(self.tramos.rowCount()):
                desde = _hms_a_s(self.tramos.item(i, 0).text())
                hasta = _hms_a_s(self.tramos.item(i, 1).text())
                if hasta <= desde:
                    raise ValueError(f"En la fila {i + 1}, «hasta» tiene que ser después de «desde».")
                tramos.append({"desde_s": desde, "hasta_s": hasta, "motivo": (self.tramos.item(i, 2) or QTableWidgetItem("")).text()})
        except ValueError as e:
            QMessageBox.warning(self, "Tramos excluidos", str(e) if "fila" in str(e) else "Escribe los tiempos como h:mm:ss.")
            return
        nuevo = {"servicio": self.servicio.currentText(), "playlist": self.playlist.text().strip(),
                 "estado": self.estado.currentData(), "notas": self.notas.toPlainText().strip(), "tramos_excluidos": tramos}
        try:
            sesiones.guardar_estudio(self.carpeta, nuevo, motivo="corregido desde la ventana")
        except Exception as e:
            QMessageBox.warning(self, "No pude guardar", str(e))
            return
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
        self.b_excel = QPushButton("Descargar resultados")
        self.b_excel.setToolTip("Guarda en un Excel: las canciones, los promedios de la sesión, los datos del "
                                "gráfico de LUFS y el registro.")
        self.b_excel.clicked.connect(self.descargar_resultados)
        self.b_excel.setEnabled(False)
        self.exportando = False
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
        barra.addSpacing(14)
        barra.addWidget(self.b_excel)

        self.lbl_alertas = QLabel("")
        self.lbl_alertas.setWordWrap(True)
        self.lbl_alertas.setStyleSheet(f"background:#fdecea; color:{ROJO}; border:1px solid #f5c2c0;"
                                       "border-radius:6px; padding:8px; font-weight:bold")
        self.lbl_alertas.hide()

        self.pestana_vivo = PestanaVivo()
        self.pestana_vivo.panel_frec.pedir_capa.connect(self._pedir_capa)
        self.pestana_vivo.panel_frec.recalcular.connect(self._recalcular_sesion)
        self.pestana_vivo.panel_frec.guardar.connect(self._guardar_niveles)
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
        # Una sesión cortada (abierta, sin motor) se vuelve a intentar retomar sola.
        self.retomando = None        # carpeta de la sesión que se está retomando
        self.ult_retomar = 0.0
        self.timer_retomar = QTimer(self)
        self.timer_retomar.timeout.connect(self._reintentar)
        self.timer_retomar.start(10_000)

        self.pestana_config.cargar_dispositivos()
        info = protocolo.motor_vivo()
        if info:
            self.esperando = time.time()
            self._poner_estado("conectando")
        else:
            self._sin_motor()
        # El guardián de Windows (tarea programada) retoma la sesión abierta tras un reinicio o una
        # caída, aunque la ventana no esté abierta.
        Tarea(guardian.al_dia).iniciar(lambda _r: None, self._guardian_fallo)

    # ------------------------------------------------------------------ config
    def guardar_config(self):
        if self.estado_ui in ("viendo", "terminada", "cortada"):
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
        if self.estado_ui == "cortada":
            # la sesión se cortó y no se pudo retomar: se puede dar por terminada sin su motor
            if self._confirmar_detener():
                carpeta = self.carpeta_sesion
                sesiones.cerrar_abierta(carpeta, "detenida desde la ventana (estaba cortada)")
                self.lbl_alertas.hide()
                self.abrir_sesion(carpeta)
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

    # ------------------------------------------------------------------ retomar
    def _pedir_capa(self):
        if self.carpeta_sesion and self.estado_ui in ("viendo", "terminada"):
            DialogoArchivo(self, self.carpeta_sesion).exec()
            self.abrir_sesion(self.carpeta_sesion)

    def _recalcular_sesion(self, nivel, phon):
        """La sesión que se está viendo, calculada a otro nivel de escucha (sin cambiarla)."""
        if not self.carpeta_sesion or self.estado_ui not in ("viendo", "terminada"):
            return
        self.statusBar().showMessage(f"Recalculando a {nivel:g} dB SPL y {phon:g} phon…")
        self.b_sesiones.setEnabled(False)
        carpeta = self.carpeta_sesion
        Tarea(sesiones.reconstruir, carpeta, nivel, phon).iniciar(
            self._sesion_abierta, lambda msg: self._abrir_fallo(carpeta, msg, False))

    def _guardar_niveles(self, nivel, phon):
        carpeta = self.carpeta_sesion
        if not carpeta or self.estado_ui not in ("viendo", "terminada"):
            return
        try:
            cambios = sesiones.guardar_sonoridad(carpeta, nivel, phon)
        except Exception as e:
            QMessageBox.warning(self, "No se guardó", str(e))
            return
        self.statusBar().showMessage(f"Guardado en la sesión: {nivel:g} dB SPL y {phon:g} phon" if cambios
                                     else "La sesión ya tenía esos niveles")
        self.abrir_sesion(carpeta)

    def _validar_condiciones(self, cfg):
        """Fuente, app y lista coherentes (S2). Devuelve (problemas que impiden grabar, avisos)."""
        problemas, avisos = [], []
        app = (cfg.app or "").lower()
        servicio = servicio_de(cfg.fuente)
        if servicio == "Soundtrack" and "spotify" in app:
            problemas.append("La fuente es Soundtrack, pero la app a seguir es Spotify.")
        if servicio == "Spotify" and app and "spotify" not in app:
            problemas.append(f"La fuente es Spotify, pero la app a seguir es «{cfg.app}».")
        if servicio == "Mystify" and not cfg.stream_url:
            problemas.append("La fuente es Mystify, pero falta la dirección del stream para los títulos.")
        if servicio != "Mystify" and cfg.stream_url:
            avisos.append(f"Los títulos se van a leer del stream de una radio ({cfg.stream_url}), pero la fuente es "
                          f"{cfg.fuente}.")
        if not (cfg.playlist or "").strip():
            avisos.append("No anotaste la lista (playlist o canal).")
        if servicio == "Mystify" and cfg.nivel_normalizacion != "No aplica":
            avisos.append("Una radio no normaliza por canción: la normalización debería ser «No aplica».")
        return problemas, avisos

    def _revisar_chequeo(self, cfg):
        """Prueba de cadena y lista de chequeo antes de grabar (S1). Devuelve lo que se guarda en la sesión,
        o None si no se graba todavía."""
        cad = self.pestana_chequeo.ultima_cadena
        prueba = None
        if cad and not vacio(cad.get("lufs")):
            dif = float(cad["lufs"]) - float(cad.get("esperado", -20.0))
            prueba = {"medido_lufs": float(cad["lufs"]), "esperado_lufs": float(cad.get("esperado", -20.0)),
                      "dif_lu": dif, "ok": abs(dif) <= 0.1, "pico_dbfs": cad.get("pico"),
                      "dispositivo": cad.get("dispositivo"), "hora": cad.get("hora")}
        reciente = False
        if prueba and prueba["dispositivo"] == cfg.dispositivo:
            try:
                reciente = time.time() - dt.datetime.fromisoformat(prueba["hora"]).timestamp() <= 30 * 60
            except (TypeError, ValueError):
                reciente = False
        cadena_ok = bool(prueba and prueba["ok"] and reciente)
        marcados = self.pestana_config.checklist
        lista_ok = all(marcados.get(t) for t in MANUAL)
        datos = {"prueba_cadena": prueba, "checklist_completa": lista_ok, "grabada_sin_chequeo": False}
        if cadena_ok and lista_ok:
            return datos
        faltan = []
        if not cadena_ok:
            faltan.append("la prueba de la cadena de medición (correcta, con este dispositivo, en los últimos 30 min)")
        if not lista_ok:
            faltan.append(f"la lista de chequeo ({sum(1 for t in MANUAL if marcados.get(t))} de {len(MANUAL)} marcadas)")
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Falta el chequeo previo")
        caja.setText("Antes de grabar falta " + " y ".join(faltan) + ".")
        caja.setInformativeText("Si grabas igual, la sesión queda marcada como grabada sin chequeo.")
        probar = caja.addButton("Probar ahora", QMessageBox.ButtonRole.AcceptRole)
        igual = caja.addButton("Grabar igual (queda marcado)", QMessageBox.ButtonRole.DestructiveRole)
        caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(probar)
        caja.exec()
        b = caja.clickedButton()
        if b is igual:
            datos["grabada_sin_chequeo"] = True
            return datos
        if b is probar:
            self.tabs.setCurrentWidget(self.pestana_chequeo)
            if not cadena_ok:
                self.pestana_chequeo.probar_cadena()
        return None

    def _retomar(self, carpeta):
        """La sesión abierta no tiene motor (se cortó): se lanza uno que la continúe. Si el guardián
        lanzó otro al mismo tiempo, el segundo no arranca y la ventana se conecta al primero."""
        self.carpeta_sesion = carpeta
        self.retomando = carpeta
        self.ult_retomar = time.time()
        self.recibio_fin = False
        self.proc = sistema.lanzar_desacoplado(sistema.comando_motor("--motor", "--continuar", carpeta))
        self.esperando = time.time()
        self._poner_estado("retomando")
        self.statusBar().showMessage(f"Retomando la sesión · {carpeta}")

    def _retomar_fallo(self, msg):
        carpeta, self.retomando = self.retomando, None
        self.esperando = None
        self.proc = None
        self.lbl_alertas.setText(f"⚠  La grabación se cortó y no se pudo retomar: {msg}\n"
                                 "Se vuelve a intentar sola cada minuto (y el guardián de Windows, cada 2). "
                                 "Si no se puede seguir, «Detener» la termina.")
        self.lbl_alertas.show()
        self.abrir_sesion(carpeta)       # se muestra mientras tanto (queda como «cortada»)

    def _reintentar(self):
        """Cada 10 s, con una sesión cortada a la vista: si el guardián ya lanzó su motor, se conecta;
        si no, cada minuto vuelve a intentar retomarla."""
        if self.estado_ui != "cortada" or self.grabando():
            return
        if protocolo.motor_vivo():
            self.esperando = time.time()
            self._poner_estado("conectando")
            return
        ab = protocolo.sesion_abierta()
        if ab and time.time() - self.ult_retomar >= 60:
            self._retomar(ab["carpeta"])

    def _guardian_fallo(self, msg):
        QMessageBox.warning(self, "Guardián de Windows",
                            "No pude registrar la tarea de Windows que retoma una grabación después de un "
                            f"reinicio o una caída:\n\n{msg}\n\nLa grabación igual se retoma al abrir Loudness Lab.")

    # ------------------------------------------------------------------ sesiones
    def _sin_motor(self):
        """No hay ninguna grabación en curso: si quedó una sesión abierta (se cortó), se retoma; si
        no, se abre donde se quedó, en la última sesión."""
        ab = protocolo.sesion_abierta()
        if ab:
            if time.time() - self.ult_retomar >= 20:
                self._retomar(ab["carpeta"])
            return
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
        self._poner_estado("cortada" if snap["cerrada"].get("abierta") else "viendo")
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
        ab = protocolo.sesion_abierta()
        if ab:
            nombre = os.path.basename(os.path.normpath(ab["carpeta"]))
            r = QMessageBox.question(self, "Hay una sesión abierta",
                                     f"La sesión «{nombre}» quedó abierta: se cortó y se retoma sola. Para empezar "
                                     "otra, primero hay que detenerla (es final).\n\n¿Detenerla y empezar la nueva?")
            if r != QMessageBox.StandardButton.Yes:
                return
            sesiones.cerrar_abierta(ab["carpeta"], "detenida al empezar otra sesión (estaba cortada)")
        self.guardar_config()
        cfg = self.cfg
        if not cfg.dispositivo:
            QMessageBox.warning(self, "Falta el dispositivo", "Elige desde dónde grabar en «Configuración».")
            return
        problemas, avisos = self._validar_condiciones(cfg)
        if problemas:
            self.tabs.setCurrentWidget(self.pestana_config)
            QMessageBox.warning(self, "La configuración no es coherente", "\n\n".join(problemas))
            return
        if avisos:
            r = QMessageBox.question(self, "Revisa la configuración", "\n\n".join(avisos) + "\n\n¿Grabar igual?")
            if r != QMessageBox.StandardButton.Yes:
                self.tabs.setCurrentWidget(self.pestana_config)
                return
        chequeo = self._revisar_chequeo(cfg)
        if chequeo is None:
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
        datos.update(chequeo)
        ruta = sistema.carpeta_app() / "inicio.json"
        ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        self.proc = sistema.lanzar_desacoplado(sistema.comando_motor("--motor", "--config", str(ruta)))
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
            if self.retomando:
                # el motor que retoma lo lanzó otro (el guardián) o aún no se anuncia: se espera
                if time.time() - self.esperando > 25:
                    self._retomar_fallo("el motor no respondió a tiempo.")
                return
            # El aviso de la grabación a la que había que conectarse ya no está: terminó mientras
            # tanto (por ejemplo, se cerró justo antes de abrir la app). No queda a qué conectarse.
            self.esperando = None
            if self.en_curso and not self.recibio_fin:
                self._terminar("el motor se cerró inesperadamente; revisa registro.log en la carpeta de la sesión")
            else:
                self._sin_motor()
            return
        if self.proc is not None and self.proc.poll() is not None:
            codigo, self.proc = self.proc.returncode, None
            err = ""
            try:
                err = protocolo.ruta_error().read_text(encoding="utf-8")
            except OSError:
                pass
            if self.retomando:
                if codigo != 0:      # (0: ya había otro motor arrancando, el del guardián: se espera a ese)
                    self._retomar_fallo(err.split("\n\n")[0] if err else "el motor terminó sin dar detalles.")
                return
            self.esperando = None
            self._poner_estado("detenido")
            QMessageBox.critical(self, "No se pudo iniciar la grabación",
                                 (err.split("\n\n")[0] if err else "El motor terminó sin dar detalles.")
                                 + f"\n\nDetalle en {sistema.carpeta_app() / 'motor.log'}")
        elif time.time() - self.esperando > 25:
            if self.retomando:
                self._retomar_fallo("el motor no respondió a tiempo.")
                return
            self.esperando = None
            self._poner_estado("detenido")
            QMessageBox.critical(self, "No se pudo iniciar la grabación", "El motor no respondió a tiempo.")

    def _manejar(self, m):
        tipo = m.get("tipo")
        if tipo == "snapshot":
            self.en_curso = True
            self.retomando = None
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
            ab = protocolo.sesion_abierta()
            if ab and self.carpeta_sesion and sesiones._misma_carpeta(ab["carpeta"], self.carpeta_sesion):
                # El motor murió sin cerrar la sesión: sigue abierta y se retoma. Si se acaba de
                # retomar y volvió a caer, se espera un minuto antes de insistir.
                self.en_curso = False
                self.pestana_vivo.agregar_log("— El motor se detuvo sin cerrar la sesión: se retoma.", "WARNING")
                if time.time() - self.ult_retomar >= 30:
                    self._retomar(ab["carpeta"])
                else:
                    self.retomando = ab["carpeta"]
                    self._retomar_fallo("el motor se volvió a detener al poco de retomar.")
                return
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
                  "retomando": ("◌ Retomando la grabación…", AMARILLO),
                  "grabando": ("● Grabando", ROJO), "pausada": ("⏸ En pausa", AMARILLO),
                  "cortada": ("⚠ Grabación cortada · se retoma sola", ROJO),
                  "terminada": ("■ Terminada", GRIS), "viendo": ("▣ Sesión guardada", GRIS)}
        txt, color = textos[estado]
        self.lbl_estado.setText(txt + (f" ({motivo})" if motivo and estado == "terminada" else ""))
        self.lbl_estado.setStyleSheet(f"color:{color}")
        activo = estado in ("iniciando", "conectando", "retomando", "grabando", "pausada")
        solo_lectura = estado in ("viendo", "terminada")
        # una sesión cortada sigue abierta: se puede detener (es final), no empezar otra encima
        self.b_grabar.setText("■  Detener" if activo or estado == "cortada"
                              else ("+  Nueva sesión" if solo_lectura else "▶  Iniciar grabación"))
        self.b_grabar.setEnabled(estado in ("detenido", "terminada", "viendo", "grabando", "pausada", "cortada"))
        self.b_pausa.setVisible(activo)
        self.b_pausa.setEnabled(estado in ("grabando", "pausada"))
        self.b_pausa.setText("▶  Reanudar" if estado == "pausada" else "⏸  Pausar")
        self.b_sesiones.setEnabled(not activo)
        self._habilitar_excel()
        self.pestana_config.bloquear(
            activo or solo_lectura or estado == "cortada",
            "Grabación en curso: la configuración queda fija hasta que termine. Solo se puede cambiar «Escuchar»."
            if activo or estado == "cortada" else "Sesión guardada: su configuración es de solo lectura. Con "
                                                 "«Nueva sesión» puedes grabar otra con estos mismos ajustes.")
        self.pestana_chequeo.bloquear(activo)

    # ------------------------------------------------------------------ resultados
    def _habilitar_excel(self):
        hay = self.estado_ui in ("grabando", "pausada", "viendo", "terminada", "cortada") \
            and self.pestana_vivo.inicio_epoch is not None
        self.b_excel.setEnabled(hay and not self.exportando)

    def descargar_resultados(self):
        """Guarda lo que muestra la pestaña En vivo en un Excel de cuatro hojas (ver excel.py)."""
        pv = self.pestana_vivo
        if pv.inicio_epoch is None or self.exportando:
            return
        nombre = self.lbl_etiqueta.text() or "sesion"
        carpeta = self.carpeta_sesion if self.carpeta_sesion and os.path.isdir(self.carpeta_sesion) \
            else self.pestana_config.carpeta.text().strip()
        sugerido = os.path.join(carpeta, f"{sistema.limpiar_nombre(nombre)} - resultados.xlsx")
        ruta, _ = QFileDialog.getSaveFileName(self, "Descargar resultados", sugerido, "Libro de Excel (*.xlsx)")
        if not ruta:
            return
        if not ruta.lower().endswith(".xlsx"):
            ruta += ".xlsx"
        d = pv.datos_exportacion(self.carpeta_sesion)
        self.exportando = True
        self._habilitar_excel()
        self.statusBar().showMessage("Generando el Excel…")
        Tarea(excel.exportar, ruta, nombre, d["inicio_epoch"], d["horas"], d["pistas"], d["stats"], d["curva"],
              d["registro"]).iniciar(lambda r: self._excel_listo(ruta, r), lambda msg: self._excel_fallo(ruta, msg))

    def _excel_listo(self, ruta, r):
        self.exportando = False
        self._habilitar_excel()
        self.statusBar().showMessage(f"Resultados guardados en {ruta}")
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Information)
        caja.setWindowTitle("Resultados guardados")
        caja.setText(f"Guardé el Excel en:\n{ruta}")
        extra = []
        if r["celdas_registro"] > 1:
            extra.append(f"El registro es más largo de lo que cabe en una celda de Excel (32 767 caracteres): "
                         f"sigue en las celdas A2 a A{r['celdas_registro']} de la hoja «Registro».")
        if r["submuestreo"] > 1:
            extra.append(f"La sesión es tan larga que la hoja «Data LUFS» tiene un punto cada {r['submuestreo']} s.")
        if extra:
            caja.setInformativeText("\n".join(extra))
        abrir = caja.addButton("Abrir el Excel", QMessageBox.ButtonRole.AcceptRole)
        caja.addButton("Cerrar", QMessageBox.ButtonRole.RejectRole)
        caja.exec()
        if caja.clickedButton() is abrir:
            try:
                sistema.abrir(ruta)
            except OSError as e:
                QMessageBox.warning(self, "No pude abrir el Excel", str(e))

    def _excel_fallo(self, ruta, msg):
        self.exportando = False
        self._habilitar_excel()
        self.statusBar().showMessage("No se pudo guardar el Excel")
        detalle = "Si el archivo está abierto en Excel, ciérralo y vuelve a intentarlo." if "ermission" in msg \
            or "denied" in msg else ""
        QMessageBox.warning(self, "No pude guardar el Excel", f"{ruta}\n\n{msg}" + (f"\n\n{detalle}" if detalle else ""))

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
                         "volver a abrir Loudness Lab te reconectas a ella. Si el PC se reinicia o el programa "
                         "se cierra, la grabación se retoma sola al volver a iniciar sesión.")
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
    # de la ventana y la cierra (sin detener el motor). Con LOUDNESS_EXPORTAR="ruta.xlsx" guarda
    # además el Excel de «Descargar resultados».
    captura = os.environ.get("LOUDNESS_CAPTURA")
    if captura:
        partes = captura.split("|")
        ruta, seg = partes[0], float(partes[1]) if len(partes) > 1 else 5

        def capturar():
            if os.environ.get("LOUDNESS_EXPORTAR"):
                d = v.pestana_vivo.datos_exportacion(v.carpeta_sesion)
                excel.exportar(os.environ["LOUDNESS_EXPORTAR"], v.lbl_etiqueta.text(), d["inicio_epoch"], d["horas"],
                               d["pistas"], d["stats"], d["curva"], d["registro"])
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
