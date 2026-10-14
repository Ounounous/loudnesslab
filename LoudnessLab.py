"""Punto de entrada de Loudness Lab.

Sin argumentos abre la ventana. Con --motor corre el motor de grabación (la ventana lo lanza
así, en un proceso aparte). Con --asegurar corre el guardián, que retoma una sesión abierta cuyo
motor no está corriendo (lo usa la tarea programada de Windows).
"""

import faulthandler
import os
import sys


def _registrar_caidas():
    """Si el proceso se cae en código nativo (audio, Qt, Windows), la traza queda en
    %APPDATA%\\LoudnessLab\\caidas.log: sin esto, una caída a mitad de una grabación larga
    no deja ninguna pista."""
    try:
        carpeta = os.path.join(os.environ.get("APPDATA", "."), "LoudnessLab")
        os.makedirs(carpeta, exist_ok=True)
        f = open(os.path.join(carpeta, "caidas.log"), "a", encoding="utf-8")
        faulthandler.enable(file=f, all_threads=True)
        return f
    except OSError:
        return None


def main():
    _archivo_caidas = _registrar_caidas()  # noqa: F841  (debe quedar abierto mientras corre)
    if "--asegurar" in sys.argv:   # el guardián (tarea programada de Windows)
        from loudness_lab.guardian import main as guardian
        return guardian()
    if "--motor" in sys.argv:
        from loudness_lab.motor import main as motor
        return motor(sys.argv[1:])
    from loudness_lab.gui.ventana import main as ventana
    return ventana()


if __name__ == "__main__":
    sys.exit(main())
