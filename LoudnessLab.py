"""Punto de entrada de Loudness Lab.

Sin argumentos abre la ventana. Con --motor corre el motor de grabación (la ventana lo lanza
así, en un proceso aparte).
"""

import sys


def main():
    if "--motor" in sys.argv:
        from loudness_lab.motor import main as motor
        return motor(sys.argv[1:])
    from loudness_lab.gui.ventana import main as ventana
    return ventana()


if __name__ == "__main__":
    sys.exit(main())
