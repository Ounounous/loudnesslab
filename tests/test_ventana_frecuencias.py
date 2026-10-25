r"""Pruebas de las fases 2 y 3 en la ventana, sin pantalla: el panel «Frecuencias» (en vivo, en una sesión
guardada y cuando falta la capa), las columnas Graves/Desaj. y las de la fase 3 (ΔI, cuerpo, percibido),
el panel de promedios, la hoja «Espectro por canción» del Excel, el diálogo «Frecuencias y archivo», la
lista de sesiones, picos.csv con ΔI y salto de cuerpo, y analizar.py sin audio.
Uso: .venv\Scripts\python.exe tests\test_ventana_frecuencias.py"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["LOUDNESS_SIN_GUARDIAN"] = "1"

import numpy as np  # noqa: E402
from scipy.signal import butter, sosfilt  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import soundfile as sf  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import analizar  # noqa: E402
from loudness_lab import bandas, capa, excel, picos, sesiones  # noqa: E402
from loudness_lab.bs1770 import MedidorBloques  # noqa: E402
from loudness_lab.gui import ventana  # noqa: E402

fallas = []
SR = 48000
DUR = 36           # s por canción
N_CANCIONES = 5


def chequeo(nombre, ok, detalle=""):
    if not ok:
        fallas.append(nombre)
    print(f"  {'OK ' if ok else 'FALLA'} {nombre} {detalle}")


def sesion_sintetica(base):
    """Cinco «canciones» de ruido rosa con distinto color y nivel, medidas como en vivo (sin espectro)."""
    rng = np.random.default_rng(11)
    partes = []
    for k in range(N_CANCIONES):
        x = np.stack([bandas.ruido_rosa(SR * DUR, SR, 2 * k + 1), bandas.ruido_rosa(SR * DUR, SR, 2 * k + 2)], 1)
        if k % 3 == 1:     # graves de más
            x = x + 2.5 * sosfilt(butter(2, 150, "lowpass", fs=SR, output="sos"), x, axis=0)
        elif k % 3 == 2:   # medios de más (donde el oído es más sensible)
            x = x + 4.0 * sosfilt(butter(2, [1000, 3500], "bandpass", fs=SR, output="sos"), x, axis=0)
        else:              # agudos de más
            x = x + 1.5 * sosfilt(butter(2, 5000, "highpass", fs=SR, output="sos"), x, axis=0)
        partes.append(x * rng.uniform(0.2, 0.6))
    x = np.concatenate(partes).astype(np.float32)
    c = os.path.join(base, "sesion_fase2")
    os.makedirs(c)
    wav = "audio_0001_20261024_120000.wav"
    sf.write(os.path.join(c, wav), x, SR, subtype="FLOAT")
    m = MedidorBloques(SR, 2)
    trozos = [m.procesar(x[i:i + 4800 * 7]) for i in range(0, len(x), 4800 * 7)] + [m.terminar()]
    zc, sp, tp = (np.concatenate([t[j] for t in trozos]) for j in range(3))
    np.column_stack([zc, sp, tp]).astype(np.float32).tofile(os.path.join(c, "mediciones.f32"))
    ini = "2026-10-24T12:00:00-03:00"
    s = {"etiqueta": "sesion_fase2", "sample_rate": SR, "canales": 2, "inicio_pc": ini,
         "fin_pc": "2026-10-24T12:03:00-03:00", "segundos_totales": len(x) / SR,
         "archivos": [{"archivo": wav, "frame_global_inicio": 0, "hora_pc_inicio": ini}],
         "frames_totales": len(x), "estudio": {"servicio": "Otro", "estado": "valida"}}
    json.dump(s, open(os.path.join(c, "sesion.json"), "w", encoding="utf-8"))
    with open(os.path.join(c, "pistas.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("hora_pc,frame_global,archivo,frame_en_archivo,evento,app,artista,titulo,album,estado,posicion_s,duracion_s\n")
        for k in range(N_CANCIONES):
            fr = k * DUR * SR
            f.write(f"2026-10-24T12:00:{k * 10:02d}-03:00,{fr},{wav},{fr},cambio_pista,app,Artista {k},Tema {k},,reproduciendo,,\n")
    return c


class CajaFalsa:
    """QMessageBox que no bloquea: responde con el primer botón que se le agrega (el de confirmar)."""
    Icon, ButtonRole, StandardButton = QMessageBox.Icon, QMessageBox.ButtonRole, QMessageBox.StandardButton
    vistos = []

    def __init__(self, *a):
        self.botones = []

    def addButton(self, texto, rol):
        b = types.SimpleNamespace(texto=texto)
        self.botones.append(b)
        return b

    def clickedButton(self):
        return self.botones[0]

    def exec(self):
        CajaFalsa.vistos.append(self)

    def __getattr__(self, nombre):      # setIcon, setText, setInformativeText, setDefaultButton…
        return lambda *a, **k: None

    @staticmethod
    def warning(*a):
        CajaFalsa.vistos.append(("warning",) + a[1:])

    information = warning


def args_analisis(salida):
    return types.SimpleNamespace(refinar=3.0, min_dur=30.0, umbral_etiqueta=3.0, sin_true_peak=False,
                                 sin_graficos=True, comparacion=salida, desde_mediciones=False)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    d = tempfile.mkdtemp(prefix="fase2_")
    try:
        c = sesion_sintetica(d)
        vivo = ventana.PestanaVivo()
        panel = vivo.panel_frec

        print("1. Sesión sin capa de frecuencias: el panel lo dice y ofrece generarla")
        snap = sesiones.reconstruir(c)
        vivo.mostrar_sesion(snap)
        chequeo("canciones sin espectro", snap["pistas"] and not any(p.get("espectro") for p in snap["pistas"]),
                f"({len(snap['pistas'])} canciones)")
        chequeo("aviso y botón «Generar capa…»", not panel.lbl_falta.isHidden() and not panel.b_capa.isHidden()
                and "no tiene capa" in panel.lbl_falta.text(), repr(panel.lbl_falta.text()[:70]))
        chequeo("columnas Graves, Desaj. y Percib. vacías", vivo.tabla.item(0, 10).text() in ("—", "") and
                vivo.tabla.item(0, 9).text() in ("—", "") and vivo.tabla.horizontalHeaderItem(11).text() == "Desaj.")
        chequeo("sin capa, la sonoridad lo dice", vivo.stats["sonoridad"].text() == "sin capa de frecuencias")

        print("2. analizar.py con el audio (para comparar después sin él)")
        con_audio, li_audio, sal_audio = analizar.analizar_sesion(c, args_analisis(os.path.join(d, "cmp")))

        print("3. Generar la capa: la sesión guardada muestra el promedio de sus canciones válidas")
        inf = capa.procesar(c)
        chequeo("capa generada y verificada", inf["ok"] and inf["espectro"] == "generado desde el audio")
        snap = sesiones.reconstruir(c)
        vivo.limpiar()
        vivo.mostrar_sesion(snap)
        con = [p for p in snap["pistas"] if p.get("espectro")]
        validas = [p for p in con if p.get("valida")]
        chequeo("cada canción trae su espectro, reparto y desajuste", len(con) == len(snap["pistas"])
                and all(len(p["espectro"]) == bandas.N_BANDAS and "graves" in p["reparto"] for p in con))
        graves = [con[k] for k in range(len(con)) if k % 3 == 1]
        medios = [con[k] for k in range(len(con)) if k % 3 == 2]
        otras = [p for p in con if p not in graves]
        chequeo("las de graves de más tienen más graves", min(p["reparto"]["graves"] for p in graves)
                > max(p["reparto"]["graves"] for p in otras))
        chequeo("las de medios de más suenan más de lo que dice su LUFS",
                min(p["desajuste_db"] for p in medios) > 0 > max(p["desajuste_db"] for p in graves),
                f"(medios {[round(p['desajuste_db'], 1) for p in medios]}, graves {[round(p['desajuste_db'], 1) for p in graves]})")
        chequeo("panel con bloques y texto del promedio", panel.lbl_falta.isHidden() and panel.b_capa.isHidden()
                and f"Promedio de las {len(validas)} canciones válidas" in panel.lbl.text()
                and panel.nivel.max() > 0 and np.array_equal(panel.pico, panel.nivel), repr(panel.lbl.text()[:80]))
        fila = vivo.tabla.item(0, 10).text(), vivo.tabla.item(0, 11).text()
        chequeo("columnas Graves y Desaj. con valores", fila[0].endswith("%") and fila[1] not in ("—", ""), str(fila))

        print("3b. Fase 3: cambios de canción y sonoridad percibida")
        ultima = snap["pistas"][-1]
        fila = [vivo.tabla.item(0, k).text() for k in (7, 8, 9)]
        chequeo("columnas ΔI, Cuerpo y Percib. de la última canción",
                fila == [ventana.num(ultima["delta_i"], signo=True), ventana.num(ultima["salto_cuerpo"], signo=True),
                         ventana.num(ultima["lufs_percibido"])], str(fila))
        chequeo("cada canción con LN, N5 y LUFS percibido", all(p.get("ln") and p.get("n5") and p.get("lufs_percibido")
                                                              for p in snap["pistas"]))
        s3 = snap["estado"]["sesion"]
        chequeo("promedios: sonoridad percibida y su intervalo", s3["con_sonoridad"] == len(validas)
                and "desv." in vivo.stats["sonoridad"].text() and " phon" in vivo.stats["sonoridad"].text()
                and len(s3["ic"]["percibido_desv"]) == 2, vivo.stats["sonoridad"].text())
        chequeo("promedios: cuerpo, arranque y silencio", "cuerpo > 3 LU" in vivo.stats["cuerpo"].text()
                and "> 2 s" in vivo.stats["silencio"].text() and "> 3 LU" in vivo.stats["cambios"].text(),
                vivo.stats["cuerpo"].text())
        vivo.sucio = True
        vivo.redibujar()
        xs, ys = vivo.percibidos.getData()
        chequeo("gráfico: una línea de percibido por canción", xs is not None and len(xs) == 2 * len(snap["pistas"])
                and set(np.round(ys, 6)) == {round(p["lufs_percibido"], 6) for p in snap["pistas"]})
        vivo.chk_percibido.setChecked(False)
        chequeo("y se puede ocultar", not vivo.percibidos.isVisible())
        vivo.chk_percibido.setChecked(True)

        print("3c. Recalcular la sesión a otro nivel de escucha, y guardarlo")
        chequeo("se abre con los niveles por defecto", s3["nivel_escucha_db_spl"] == 65 and s3["phon_indice"] == 60
                and not vivo.panel_frec.fila_niveles.isHidden() and "guardados en la sesión" in panel.lbl_niveles.text())
        snap40 = sesiones.reconstruir(c, 40, 45)
        vivo.limpiar()
        vivo.mostrar_sesion(snap40)
        a, b = snap["pistas"], snap40["pistas"]
        chequeo("a 40 dB SPL / 45 phon: mismos LUFS, otra percepción",
                [p["lufs"] for p in a] == [p["lufs"] for p in b]
                and all(abs(p["lufs_percibido"] - q["lufs_percibido"]) > 0.05 for p, q in zip(a, b))
                and any(abs(p["desajuste_db"] - q["desajuste_db"]) > 0.1 for p, q in zip(a, b)))
        graves_40 = [q["lufs_percibido"] - q["lufs"] for q in b if q["reparto"]["graves"] > 50]
        graves_65 = [p["lufs_percibido"] - p["lufs"] for p in a if p["reparto"]["graves"] > 50]
        chequeo("a menor nivel, los graves pesan menos en lo percibido", max(graves_40) < min(graves_65),
                f"(percibido − LUFS: {[round(x, 1) for x in graves_65]} a 65 → {[round(x, 1) for x in graves_40]} a 40)")
        chequeo("la vista avisa que está recalculada", "Vista recalculada a 40 dB SPL y 45 phon" in panel.lbl_niveles.text()
                and panel.sp_nivel.value() == 40 and "(45 phon)" in panel.leyenda.text(), panel.lbl_niveles.text())
        cambios = sesiones.guardar_sonoridad(c, 40, 45)
        sj = sesiones._json(os.path.join(c, "sesion.json"))
        snap_g = sesiones.reconstruir(c)
        chequeo("guardar deja los niveles en la sesión, con registro", cambios == ["nivel", "phon"]
                and sj["sonoridad"]["nivel_escucha_db_spl"] == 40 and sj["sonoridad"]["phon_indice"] == 45
                and [e["campo"] for e in sj["ediciones"][-2:]] == ["sonoridad.nivel_escucha_db_spl", "sonoridad.phon_indice"]
                and snap_g["estado"]["sesion"]["nivel_escucha_db_spl"] == 40
                and [p["lufs_percibido"] for p in snap_g["pistas"]] == [q["lufs_percibido"] for q in b])
        vivo.limpiar()
        vivo.mostrar_sesion(snap_g)
        chequeo("y al reabrirla son los suyos", "guardados en la sesión" in panel.lbl_niveles.text())
        sesiones.guardar_sonoridad(c, 65, 60)      # (lo que sigue espera los de siempre)
        vivo.limpiar()
        vivo.mostrar_sesion(snap)

        print("3d. Gráfico y tabla: el detalle de cada canción, clic para ir a la tabla, clic en la tabla para el zoom")
        vivo.resize(1400, 900)
        vivo.sucio = True
        vivo.redibujar()
        n = len(snap["pistas"])
        medio = [(vivo.x_ini[k] + vivo.x_fin[k]) / 2 for k in range(n)]
        chequeo("el gráfico ya no muestra la ayuda del zoom como tooltip fijo", vivo.plot.toolTip() == "")
        chequeo("cada instante del gráfico, su canción", [vivo._pista_en(x) for x in medio] == list(range(n))
                and vivo._pista_en(vivo.x_ini[0] - 5) is None)
        det = vivo._detalle(3)
        fila_t = n - 1 - 3
        chequeo("el detalle trae los mismos datos de la tabla", "Artista 3 – Tema 3" in det and all(
            ventana._html(vivo.tabla.item(fila_t, k).text()) in det for k in (0, 2, 3, 6, 7, 8, 9, 10, 11))
            and ventana.AYUDA_GRAFICO in det, det[:90])

        class Clic:
            def __init__(self, x, doble=False):
                self.p, self.d = vivo.vista.mapViewToScene(ventana.pg.Point(x, -20)), doble

            def button(self):
                return ventana.Qt.MouseButton.LeftButton

            def double(self):
                return self.d

            def scenePos(self):
                return self.p

        vivo._sobre_grafico(Clic(medio[2]).scenePos())
        chequeo("pasar el mouse muestra esa canción", vivo._bajo_mouse == 2)
        vivo._clic_grafico(Clic(medio[1]))
        sel = vivo.tabla.selectionModel().selectedRows()
        chequeo("clic en el gráfico: la canción queda elegida en la tabla y marcada",
                [r.row() for r in sel] == [n - 1 - 1] and vivo.tabla.item(n - 2, 1).text().startswith("Artista 1 – Tema 1")
                and vivo.resaltado.isVisible() and vivo.resaltado.getRegion() == (vivo.x_ini[1], vivo.x_fin[1]))
        vivo._fila_elegida(n - 1 - 2)
        (a, b), _ = vivo.vista.viewRange()
        chequeo("clic en la tabla: zoom a la canción con la anterior y la siguiente",
                a <= vivo.x_ini[1] < vivo.x_ini[2] and b >= vivo.x_fin[3] and a > vivo.x_ini[0] and b < vivo.x_fin[4]
                and vivo.manual and vivo.resaltado.getRegion() == (vivo.x_ini[2], vivo.x_fin[2]))
        vivo._fila_elegida(n - 1)            # la primera: no tiene anterior
        (a, b), _ = vivo.vista.viewRange()
        chequeo("la primera canción: con la siguiente", a <= vivo.x_ini[0] and vivo.x_fin[1] <= b < vivo.x_fin[2])
        vivo._clic_grafico(Clic(medio[0], doble=True))
        chequeo("doble clic: vuelve a la vista de siempre", not vivo.manual)

        print("3e. Comparar sesiones (diálogo)")
        dc = ventana.DialogoComparar(None, d)
        chequeo("lista la sesión terminada, marcada", dc.tabla.rowCount() == 1 and dc.elegidas() == [c])
        ruta_cmp = os.path.join(d, "comparacion.xlsx")

        class DialogoArchivoFalso:
            @staticmethod
            def getSaveFileName(*a, **k):
                return ruta_cmp, ""

        class TareaYa:      # la tarea en el mismo hilo (en la app corre aparte)
            def __init__(self, fn, *a):
                self.fn, self.a = fn, a

            def iniciar(self, listo, fallo=None):
                listo(self.fn(*self.a))

        guardados = ventana.QFileDialog, ventana.Tarea
        ventana.QFileDialog, ventana.Tarea = DialogoArchivoFalso, TareaYa
        try:
            dc._comparar()
        finally:
            ventana.QFileDialog, ventana.Tarea = guardados
        chequeo("compara, guarda el Excel y muestra el resumen del servicio", os.path.exists(ruta_cmp)
                and dc.resultado.rowCount() == 1 and dc.resultado.item(0, 0).text() == "Otro"
                and "Listo: 1 sesión comparada" in dc.lbl_avance.text(), dc.lbl_avance.text()[:60])
        chequeo("con la sonoridad percibida del servicio", "(" in dc.resultado.item(0, 7).text(),
                dc.resultado.item(0, 7).text())

        print("3f. Techo de la cadena (S10)")
        from loudness_lab import estudio
        rng = np.random.default_rng(2)
        sp = rng.uniform(0.01, 0.9, 20000)
        tope = 10 ** (-0.11 / 20)
        con_techo = np.r_[sp, np.full(80, tope)]
        chequeo("picos amontonados bajo 0 dBFS: hay techo", (estudio.techo_cadena(con_techo) or {}).get("dbfs") == -0.11)
        chequeo("pocos bloques en el máximo: no", estudio.techo_cadena(np.r_[sp, np.full(20, tope)]) is None)
        chequeo("si los picos llegan a 0 dBFS: no", estudio.techo_cadena(np.r_[con_techo, [1.0]]) is None)
        p_max = max(snap["pistas"], key=lambda q: q["sp"])
        st_t = {**snap["estado"]["sesion"], "techo_cadena": {"dbfs": round(p_max["sp"], 2), "bloques": 99, "canciones": 1}}
        vivo._poner_stats(st_t)
        fila_t = len(vivo.pistas) - 1 - vivo.pistas.index(next(q for q in vivo.pistas if q["sp"] == p_max["sp"]))
        chequeo("la ventana avisa y muestra «≥ 0» en la canción que llega al techo",
                "la cadena no deja pasar" in vivo.stats["tp"].text() and vivo.tabla.item(fila_t, 5).text() == "≥ 0",
                vivo.stats["tp"].text())
        vivo._poner_stats(snap["estado"]["sesion"])
        chequeo("sin techo, vuelve a su valor", vivo.tabla.item(fila_t, 5).text() == ventana.num(p_max["tp"]))

        print("4. En vivo: canción en curso y pausa")
        e = np.mean([p["espectro"] for p in con], axis=0)
        vivo.actualizar({"grabando": True, "frecuencias": {"bandas_3s": list(e), "cancion": bandas.espectro_cancion(
            np.tile(e, (1, 1)), [0.0])}})
        chequeo("canción en curso", panel.lbl.text().startswith("Canción en curso: graves"), repr(panel.lbl.text()[:60]))
        son = {"n5": 9.0, "n_mediana": 7.0, "ln": 71.7, "lufs_percibido": -16.2}
        vivo.actualizar({"grabando": True, "pista": {"artista": "A", "titulo": "T", "app": "x", "t_s": 40, "lufs": -14.0,
                                                      "tp": -3.0, "salto": float("nan")},
                         "frecuencias": {"bandas_3s": list(e), "cancion_son": son, "ln_ahora": 70.1, "n_ahora": 8.2}})
        chequeo("en vivo: sonoridad de la canción y la de ahora", "LN 71,7 phon = −16,2 LUFS percibidos (su LUFS: −14,0)"
                in panel.lbl.text() and "Ahora: LN 70,1 phon" in panel.lbl.text(), repr(panel.lbl.text()[-90:]))
        vivo.actualizar({"grabando": True, "pausado": True, "frecuencias": {"bandas_3s": list(e)}})
        chequeo("en pausa no muestra bloques", "En pausa" in panel.lbl.text() and not panel.nivel.any()
                and not panel.pico.any())

        print("4b. Visor de segmentos: el rastro del pico cae lento")
        reloj = [100.0]
        panel._ahora = lambda: reloj[0]
        fuerte = np.full(bandas.N_BANDAS, 10 ** (-12 / 10))     # −12 dB: 23,2 de 24 segmentos
        suave = np.full(bandas.N_BANDAS, 10 ** (-52 / 10))      # −52 dB: 7,2
        panel.mostrar(fuerte, "x")
        chequeo("−12 dB: la barra llega a 23,2 de 24 segmentos", abs(panel.nivel[0] - 23.2) < 1e-9
                and panel.pico[0] == panel.nivel[0])
        panel.mostrar(suave, "x")
        chequeo("al bajar, la barra baja al tiro y el rastro del pico se queda arriba", abs(panel.nivel[0] - 7.2) < 1e-9
                and abs(panel.pico[0] - 23.2) < 1e-9 and panel.timer_pico.isActive())
        reloj[0] += 0.3
        panel._animar_picos()
        quieto = abs(panel.pico[0] - 23.2) < 1e-9
        reloj[0] += 0.5
        panel._animar_picos()
        medio = panel.pico[0]
        reloj[0] += 2.0
        panel._animar_picos()
        chequeo("el rastro espera un momento, cae lento y termina en el nivel nuevo", quieto and 7.2 < medio < 23.2
                and abs(panel.pico[0] - 7.2) < 1e-9 and not panel.timer_pico.isActive(), f"(a los 0,8 s va en {medio:.1f})")
        panel.mostrar(fuerte, "x")
        chequeo("al subir, el pico sube con la barra", abs(panel.pico[0] - 23.2) < 1e-9)
        panel._ahora = time.monotonic
        panel.limpiar()
        vivo.actualizar({"grabando": True})
        chequeo("grabación retomada sin capa en vivo", "no mide la capa" in panel.lbl_falta.text())

        print("5. Excel: hoja «Espectro por canción»")
        vivo.limpiar()
        vivo.mostrar_sesion(snap)
        datos = vivo.datos_exportacion(c)
        ruta = os.path.join(d, "resultados.xlsx")
        r = excel.exportar(ruta, "sesion_fase2", datos["inicio_epoch"], datos["horas"], datos["pistas"],
                           datos["stats"], datos["curva"], datos["registro"])
        with zipfile.ZipFile(ruta) as z:
            libro = z.read("xl/workbook.xml").decode("utf-8")
            hoja5 = z.read("xl/worksheets/sheet5.xml").decode("utf-8")
            textos = "".join(z.read(n).decode("utf-8") for n in z.namelist()
                             if n.startswith("xl/worksheets/") or n == "xl/sharedStrings.xml")
        chequeo("cinco hojas, la quinta es el espectro", libro.count("<sheet ") == 5 and "Espectro por canci" in libro)
        chequeo("una fila por canción con espectro", r["con_espectro"] == len(con) and
                hoja5.count("<row ") >= len(con) + 1, f"({r['con_espectro']} canciones)")
        chequeo("encabezados de bandas y regiones", "Graves (%)" in hoja5 + textos and "16 kHz" in hoja5 + textos)
        chequeo("fase 3 en el Excel: columnas y promedios", all(t in textos for t in (
            "Salto de cuerpo", "Subida de arranque", "LUFS percibido", "LN (phon)", "N5 (sone)",
            "Sonoridad percibida: desviación entre canciones (escala LUFS)", "|ΔI| &gt; 3 LU",
            "Calculado con nivel de escucha (−14 LUFS =)", "Índice rápido LUFS–oído (ISO 226) a")))

        print("6. Diálogo «Frecuencias y archivo»: requisitos y archivar")
        ventana.QMessageBox = CajaFalsa
        dlg = ventana.DialogoArchivo(None, c)
        chequeo("todos los requisitos cumplidos", "✗" not in dlg.lbl_req.text() and dlg.b_archivar.isEnabled(),
                dlg.b_archivar.text())
        chequeo("muestra cuánto audio libera", "GB" in dlg.b_archivar.text())
        dlg._archivar()
        chequeo("archiva: sin WAV y con constancia", not capa.wavs(c) and "Archivada" in dlg.lbl_avance.text()
                and (sesiones._json(os.path.join(c, "sesion.json")) or {}).get("audio_archivado"))
        chequeo("ya no se puede volver a generar ni archivar", not dlg.b_capa.isEnabled() and not dlg.b_archivar.isEnabled())
        dlg.done(0)

        print("7. Lista de sesiones y sesión archivada")
        lista = ventana.DialogoSesiones(None, d)
        chequeo("columna «Audio y frecuencias»", lista.tabla.item(0, 6).text() == "archivada", lista.tabla.item(0, 6).text())
        snap2 = sesiones.reconstruir(c)
        vivo.limpiar()
        vivo.mostrar_sesion(snap2)
        chequeo("la sesión archivada se sigue abriendo con su capa",
                [p["lufs"] for p in snap2["pistas"]] == [p["lufs"] for p in snap["pistas"]]
                and f"Promedio de las {len(validas)}" in panel.lbl.text())
        os.remove(os.path.join(c, "espectro.f32"))
        panel.mostrar_sesion([], c)
        chequeo("sin capa y archivada: lo dice, sin botón", "ya fue archivado" in panel.lbl_falta.text()
                and panel.b_capa.isHidden())

        print("8. analizar.py sin audio (desde mediciones.f32) == con audio")
        sin_audio, li_sin, sal_sin = analizar.analizar_sesion(c, args_analisis(os.path.join(d, "cmp")))
        dif = max(np.abs(li_sin - li_audio).max(), np.abs(sal_sin - sal_audio).max()) if len(li_sin) == len(li_audio) \
            and len(sal_sin) == len(sal_audio) else float("inf")
        chequeo("mismas canciones, LUFS y saltos (≤ 0,01)", dif <= 0.01 and len(li_sin) >= 3, f"(dif máx {dif:.2e})")
        chequeo("mismo integrado de la sesión", abs(sin_audio["lufs_integrado_sesion"] - con_audio["lufs_integrado_sesion"]) <= 0.01)

        print("9. picos.csv: ΔI y salto de cuerpo, también en un picos.csv de antes")
        pc = os.path.join(d, "picos_prueba")
        os.makedirs(pc)
        viejos = ["n", "archivo", "hora_pc", "corte_s", "salto_lu", "lufs_ultimos_10s_anterior",
                  "lufs_primeros_10s_siguiente", "de", "a", "corte_en_recorte_s", "corte_ajustado"]
        with open(os.path.join(pc, "picos.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(",".join(viejos) + "\n1,x.wav,2026-10-24T12:00:00,120.0,4.5,-20,-15.5,A - T,B - U,10.0,True\n")
        g = picos.GuardaPicos(pc, SR, 30)
        g.alimentar(0, np.zeros((SR * 200, 2), dtype=np.float32))
        fila = g.guardar({"corte": 1500, "salto": -3.5, "ultimos_10s": -14, "primeros_10s": -17.5, "de": "B - U",
                          "a": "C - V", "corte_ajustado": False}, lambda fr: 1.7e9 + fr / SR)
        hechos = g.completar([{"t_s": 120.0, "delta_i": 1.234, "salto_cuerpo": -2.5},
                              {"t_s": 150.0, "delta_i": float("nan"), "salto_cuerpo": 0.42},
                              {"t_s": 99.0, "delta_i": 1.0, "salto_cuerpo": 1.0}])
        import csv
        filas = list(csv.DictReader(open(os.path.join(pc, "picos.csv"), encoding="utf-8", newline="")))
        chequeo("agrega las columnas a un picos.csv viejo sin perder filas", fila and len(filas) == 2
                and list(filas[0].keys()) == picos.CAMPOS)
        chequeo("completa solo los cambios con recorte", hechos == 2 and filas[0]["delta_i_lu"] == "1.23"
                and filas[0]["salto_cuerpo_lu"] == "-2.5" and filas[1]["delta_i_lu"] == ""
                and filas[1]["salto_cuerpo_lu"] == "0.42", str([(r["delta_i_lu"], r["salto_cuerpo_lu"]) for r in filas]))
        del app
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("\nTODO BIEN" if not fallas else f"\nHAY FALLAS: {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
