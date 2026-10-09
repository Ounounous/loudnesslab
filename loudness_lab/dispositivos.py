"""Dispositivos de audio de Windows (WASAPI): cuál se graba, cuál es el cable, pruebas rápidas.

Se graba por loopback: lo que Windows envía a una salida. Para grabar sin que suene, la app de
música se envía al cable virtual (CABLE Input) en vez de a los parlantes, y se graba el
loopback del cable.
"""

import contextlib
import queue
import threading
import time

import numpy as np

from .bs1770 import MedidorBloques, db, integrado

SUFIJO_LOOPBACK = " [Loopback]"

# Al empezar a grabar, el cable entrega primero lo que quedó en su buffer interno: los últimos
# ~80 ms de lo que sonó antes, aunque haya sido hace minutos (medido: 70-80 ms tras 0,5 s y
# tras 5 s). Ese inicio no corresponde a lo que suena ahora y se descarta.
ARRANQUE_SUCIO_S = 0.2


def _pa():
    import pyaudiowpatch as pa
    return pa


# PortAudio no tolera inicializarse ni cerrarse desde dos hilos a la vez: la ventana, al abrir,
# listaba dispositivos y hacía el chequeo previo en paralelo, y el proceso se caía con un acceso
# a memoria inválido dentro de PyAudio() (2 de cada 3 aperturas del .exe). Todo uso de PortAudio
# fuera del motor pasa por aquí, de a uno. Reentrante porque unas funciones llaman a otras.
_CANDADO = threading.RLock()


@contextlib.contextmanager
def _portaudio():
    pa = _pa()
    with _CANDADO:
        p = pa.PyAudio()
        try:
            yield p, pa
        finally:
            p.terminate()


def es_cable(nombre):
    n = (nombre or "").lower()
    return "vb-audio" in n or n.startswith("cable ")


def _salidas_wasapi(p, pa):
    w = p.get_host_api_info_by_type(pa.paWASAPI)
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] == w["index"] and d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice"):
            yield d


def _predeterminada(p, pa):
    w = p.get_host_api_info_by_type(pa.paWASAPI)
    return p.get_device_info_by_index(w["defaultOutputDevice"])


def listar():
    """{"predeterminada": nombre, "salidas": [{nombre, sr, canales, cable, grabable}]}.
    Una salida es grabable si tiene su loopback."""
    with _portaudio() as (p, pa):
        loop = {d["name"][:-len(SUFIJO_LOOPBACK)] for d in p.get_loopback_device_info_generator()
                if d["name"].endswith(SUFIJO_LOOPBACK)}
        salidas = [{"nombre": d["name"], "sr": int(d["defaultSampleRate"]), "canales": d["maxOutputChannels"],
                    "cable": es_cable(d["name"]), "grabable": d["name"] in loop}
                   for d in _salidas_wasapi(p, pa)]
        return {"predeterminada": _predeterminada(p, pa)["name"], "salidas": salidas}


def nombre_por_defecto():
    """La salida a grabar si no se eligió ninguna: el cable si existe, si no la predeterminada."""
    info = listar()
    for s in info["salidas"]:
        if s["nombre"].startswith("CABLE Input") and s["grabable"]:
            return s["nombre"]
    for s in info["salidas"]:
        if s["cable"] and s["grabable"]:
            return s["nombre"]
    return info["predeterminada"]


def buscar(p, pa, nombre):
    """(loopback, salida) del dispositivo con ese nombre exacto; si no hay nombre, la
    predeterminada. Error si no existe: grabar otro dispositivo en silencio sería peor."""
    objetivo = nombre or _predeterminada(p, pa)["name"]
    salida = next((d for d in _salidas_wasapi(p, pa) if d["name"] == objetivo), None)
    lb = next((d for d in p.get_loopback_device_info_generator()
               if d["name"] == objetivo + SUFIJO_LOOPBACK), None)
    if lb is None:
        raise RuntimeError(f"No encuentro el dispositivo '{objetivo}' (o no permite grabar su salida).")
    return lb, salida


def buscar_salida(p, pa, nombre):
    """Salida para escuchar: por nombre exacto, o la predeterminada si no hay nombre."""
    if not nombre:
        return _predeterminada(p, pa)
    return next((d for d in _salidas_wasapi(p, pa) if d["name"] == nombre), None)


def _capturar(p, pa, lb, segundos, reproducir=None, salida=None):
    """Graba `segundos` del loopback; opcionalmente reproduce `reproducir(n)` en `salida`."""
    cola = queue.Queue()
    ch = int(lb["maxInputChannels"])

    def cb_in(data, n, _t, _s):
        cola.put(data)
        return (None, pa.paContinue)

    so = None
    if reproducir is not None:
        def cb_out(_i, n, _t, _s):
            return (reproducir(n), pa.paContinue)
        so = p.open(format=pa.paFloat32, channels=int(salida["maxOutputChannels"]),
                    rate=int(salida["defaultSampleRate"]), output=True,
                    output_device_index=salida["index"], stream_callback=cb_out)
    si = p.open(format=pa.paFloat32, channels=ch, rate=int(lb["defaultSampleRate"]), input=True,
                input_device_index=lb["index"], frames_per_buffer=1024, stream_callback=cb_in)
    time.sleep(segundos)
    si.stop_stream()
    si.close()
    if so is not None:
        so.stop_stream()
        so.close()
    datos = b"".join(cola.queue)
    x = np.frombuffer(datos, dtype=np.float32).reshape(-1, ch) if datos else np.zeros((0, ch))
    return x[int(ARRANQUE_SUCIO_S * int(lb["defaultSampleRate"])):]


def _medir(x, sr):
    """(LUFS integrado, pico de muestra dBFS) de un trozo de audio."""
    if len(x) < sr // 2:
        return float("nan"), float("nan")
    m = MedidorBloques(sr, x.shape[1], true_peak=False)
    zc, _, _ = m.procesar(x)
    z = zc.sum(axis=1)
    return integrado(z, 0, len(z)), float(db(np.abs(x).max()))


def medir_niveles(segundos=1.5):
    """Cuánto suena cada salida ahora mismo: sirve para ver a dónde está mandando el audio la
    app de música. Una salida sin nada sonando no entrega datos (queda como None)."""
    out = []
    with _portaudio() as (p, pa):
        for s in listar()["salidas"]:
            if not s["grabable"]:
                continue
            lb, _ = buscar(p, pa, s["nombre"])
            x = _capturar(p, pa, lb, segundos)
            l, _ = _medir(x, int(lb["defaultSampleRate"]))
            out.append({"nombre": s["nombre"], "lufs": None if np.isnan(l) else l})
    return out


def probar_cadena(nombre, nivel_dbfs=-20.0, segundos=4.0):
    """Reproduce un tono de 1 kHz a `nivel_dbfs` en la salida y lo graba por su loopback.
    En estéreo ese tono mide exactamente `nivel_dbfs` LUFS: si la cadena (Windows, el cable,
    el grabador) altera algo, aparece aquí. Primero comprueba que no haya otra cosa sonando."""
    with _portaudio() as (p, pa):
        lb, salida = buscar(p, pa, nombre)
        if salida is None:
            return {"ok": False, "mensaje": "No encuentro la salida para reproducir el tono."}
        sr = int(lb["defaultSampleRate"])
        previo = _capturar(p, pa, lb, 1.0)
        l_prev, _ = _medir(previo, sr)
        if len(previo) and not np.isnan(l_prev) and l_prev > -50:
            return {"ok": False, "mensaje": f"Hay audio sonando en {nombre} ({l_prev:.1f} LUFS). "
                                            "Pausa la música y vuelve a probar."}
        amp = 10 ** (nivel_dbfs / 20)
        ch_out = int(salida["maxOutputChannels"])
        fase = [0]

        def tono(n):
            t = (np.arange(n) + fase[0]) / int(salida["defaultSampleRate"])
            fase[0] += n
            s = (amp * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            return np.repeat(s[:, None], ch_out, axis=1).tobytes()

        x = _capturar(p, pa, lb, segundos, reproducir=tono, salida=salida)
    if len(x) < sr * 2:
        return {"ok": False, "mensaje": "No llegó audio al grabador durante la prueba."}
    x = x[sr: len(x) - sr // 2]  # sin el arranque ni el final
    l, pico = _medir(x, sr)
    ok = abs(l - nivel_dbfs) <= 0.2 and abs(pico - nivel_dbfs) <= 0.2
    return {"ok": ok, "lufs": l, "pico": pico, "esperado": nivel_dbfs,
            "mensaje": (f"Tono de {nivel_dbfs:.1f} LUFS medido en {l:.2f} LUFS (pico {pico:.2f} dBFS)."
                        + ("" if ok else " La cadena altera el nivel: revisa el volumen del dispositivo "
                                         "y que no tenga mejoras de audio activas."))}
