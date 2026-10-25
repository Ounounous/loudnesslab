r"""Pruebas de la resiliencia de la fase 4 (plan v0.2, S14), sin dispositivo de audio: la vigía de Windows
(apagado, suspensión) con mensajes simulados, la suspensión anotada como corte, los tramos sin música con su
causa, y su paso a tramos excluidos en «Datos del estudio…».
Uso: .venv\Scripts\python.exe tests\test_resiliencia.py"""

import csv
import json
import os
import shutil
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["LOUDNESS_SIN_GUARDIAN"] = "1"

import numpy as np  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from loudness_lab import motor, sesiones, sistema  # noqa: E402
from loudness_lab.seguimiento import Seguidor  # noqa: E402

fallas = []


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


class EscritorFalso:
    def __init__(self):
        self.frame_global = 48000 * 600

    def posicion(self):
        return self.frame_global, "audio_0001.wav", self.frame_global


class PublicadorFalso:
    def __init__(self):
        self.enviados = []

    def enviar(self, m):
        self.enviados.append(m)


def motor_falso(carpeta):
    """Un motor a medias: lo que usan la suspensión y los tramos sin música."""
    m = motor.Motor.__new__(motor.Motor)
    m.carpeta, m.sr, m.blk, m.etiqueta, m.cfg = carpeta, 48000, 4800, "prueba", {"app": "spotify"}
    m.escritor, m.publicador = EscritorFalso(), PublicadorFalso()
    m.seguidor = Seguidor(min_dur=30, sr=48000)
    m.seguidor.agregar(np.full(6000, 1e-3), np.full(6000, 0.1), np.full(6000, 0.1))
    m.t0, m.pausas, m.cortes, m.pausa_total, m.pausado, m.terminado = time.time() - 700, [], [], 0.0, False, False
    m.reparaciones, m.corte_pendiente, m.desfase = [], None, 0.0
    m._t_suspension = m._t_despertar = None
    m.sin_musica, m._sin_musica_actual = [], None
    m.radio, m.estado_app, m.sin_meta_desde, m.ult_dato = None, "pausado", None, 0
    return m


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    d = tempfile.mkdtemp(prefix="resiliencia_")
    try:
        print("1. La vigía de Windows (mensajes simulados)")
        vistos = []
        bloquear = [True]
        v = sistema.VigiaWindows(lambda: "Loudness Lab está grabando «prueba»", al_apagar=lambda: vistos.append("apagar"),
                                 al_suspender=lambda: vistos.append("suspender"),
                                 al_despertar=lambda: vistos.append("despertar"), bloquear=lambda: bloquear[0])
        chequeo("crea su ventana oculta", v.iniciar())
        u = sistema._user32()
        chequeo("grabando: pide esperar el apagado", u.SendMessageW(v.hwnd, sistema.WM_QUERYENDSESSION, 0, 0) == 0)
        u.SendMessageW(v.hwnd, sistema.WM_ENDSESSION, 0, 0)       # el usuario canceló
        u.SendMessageW(v.hwnd, sistema.WM_POWERBROADCAST, sistema.PBT_APMSUSPEND, 0)
        u.SendMessageW(v.hwnd, sistema.WM_POWERBROADCAST, sistema.PBT_APMRESUMEAUTOMATIC, 0)
        u.SendMessageW(v.hwnd, sistema.WM_ENDSESSION, 1, 0)       # «Apagar de todos modos»
        chequeo("suspensión, vuelta y apagado llegan al motor", vistos == ["suspender", "despertar", "apagar"], str(vistos))
        bloquear[0] = False
        chequeo("sin grabar: no bloquea el apagado", u.SendMessageW(v.hwnd, sistema.WM_QUERYENDSESSION, 0, 0) == 1)
        v.detener()
        chequeo("se detiene", not v.hilo.is_alive())

        print("2. Apagado: el bucle cierra y avisa")
        m = motor_falso(d)
        m.apagando, m.cerrado_ok = motor.threading.Event(), motor.threading.Event()
        hilo = motor.threading.Thread(target=m._al_apagar)
        t0 = time.time()
        hilo.start()
        time.sleep(0.2)
        espera = m.apagando.is_set() and hilo.is_alive()
        m.cerrado_ok.set()                  # (lo que hace ejecutar() tras el cierre limpio)
        hilo.join(2)
        chequeo("la vigía espera al cierre limpio antes de dejar seguir a Windows", espera and not hilo.is_alive()
                and time.time() - t0 < 2)

        print("3. Suspensión: queda como un corte")
        m = motor_falso(d)
        m._latido = lambda *a, **k: None
        m._al_suspender()
        m._t_suspension -= 1800             # durmió media hora
        m._al_despertar()
        m._revisar_suspension()
        filas = list(csv.DictReader(open(os.path.join(d, "pistas.csv"), encoding="utf-8", newline="")))
        c = m.cortes[0] if m.cortes else {}
        chequeo("corte y retoma en pistas.csv, con su motivo", [f["evento"] for f in filas] == ["corte", "retoma"]
                and filas[0]["estado"] == "suspensión de Windows")
        chequeo("con su duración, como un hueco en el gráfico", abs(c.get("duracion_s", 0) - 1800) < 2
                and m.pausa_total == c.get("duracion_s") and m.publicador.enviados[-1]["tipo"] == "pausas")
        cortes = sesiones.cortes_de([{**f, "frame_global": int(f["frame_global"])} for f in filas], 48000)
        chequeo("y la sesión lo lee como los demás cortes", len(cortes) == 1 and abs(cortes[0]["duracion_s"] - 1800) < 2,
                str(cortes))
        m.pausado = True
        m._al_suspender()
        m._al_despertar()
        m._revisar_suspension()
        chequeo("en pausa no agrega un corte (la pausa ya cubre ese tiempo)", len(m.cortes) == 1)

        print("4. Tramos sin música, con su causa y una notificación de Windows")
        avisos = []
        notificar = sistema.notificar
        sistema.notificar = lambda titulo, texto: avisos.append(texto)
        try:
            m = motor_falso(d)
            m.seguidor.silencio_bloques = 1500          # 2,5 min sin música
            m._seguir_sin_musica(True)
            m._seguir_sin_musica(True)                  # sigue: no se repite
            m.seguidor.agregar(np.full(300, 1e-3), np.full(300, 0.1), np.full(300, 0.1))
            m._seguir_sin_musica(False)
        finally:
            sistema.notificar = notificar
        t = m.sin_musica[0] if m.sin_musica else {}
        chequeo("un tramo, desde que empezó el silencio hasta que volvió la música", len(m.sin_musica) == 1
                and t["desde_s"] == 450.0 and t["hasta_s"] == 630.0, str(t))
        chequeo("con su causa", t.get("causa") == "spotify está en pausa", t.get("causa"))
        chequeo("y una sola notificación", len(avisos) == 1 and "sin música" in avisos[0], str(avisos))
        m2 = motor_falso(d)
        m2.estado_app, m2.sin_meta_desde = "", time.time()
        chequeo("la app cerrada", "no está abierta" in m2._causa_sin_musica())
        m2.estado_app, m2.sin_meta_desde = "reproduciendo", None
        chequeo("la app reproduce pero no llega audio", "no llega audio" in m2._causa_sin_musica())

        print("5. «Datos del estudio…»: los tramos sin música pasan a tramos excluidos")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        c = os.path.join(d, "sesion_s14")
        os.makedirs(c)
        json.dump({"etiqueta": "sesion_s14", "fin_pc": "2026-10-25T01:00:00-03:00",
                   "estudio": {"servicio": "Spotify", "estado": "valida"}, "tramos_sin_musica": m.sin_musica},
                  open(os.path.join(c, "sesion.json"), "w", encoding="utf-8"))
        from loudness_lab.gui import ventana
        dlg = ventana.DialogoEstudio(None, c)
        chequeo("el botón dice cuántos hay", dlg.b_sin_musica.isEnabled() and "(1)" in dlg.b_sin_musica.text())
        dlg._agregar_sin_musica()
        dlg._agregar_sin_musica()                       # (no se duplican)
        fila = [dlg.tramos.item(0, k).text() for k in range(3)] if dlg.tramos.rowCount() else []
        chequeo("se agregan una vez, con la causa como motivo", dlg.tramos.rowCount() == 1
                and fila == ["0:07:30", "0:10:30", "sin música: spotify está en pausa"], str(fila))
        del app
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
