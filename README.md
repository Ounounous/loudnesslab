# Loudness Lab

Graba durante horas lo que suena en el PC, registra cada cambio de canción y
mide el volumen con la norma ITU-R BS.1770-4 / EBU R128 (LUFS), la energía por
tercio de octava y la sonoridad percibida (Zwicker, ISO 532-1). Se hizo para
medir cuánto varía el volumen entre canciones en la música de fondo de
restoranes (Soundtrack, Spotify y radios web).

- App de Windows con el motor de grabación en un proceso aparte: sigue grabando
  si se cierra la ventana y se retoma sola tras un reinicio o un corte.
- Medición verificada contra el set de pruebas de la EBU.
- Resultados en Excel, recortes de los saltos notorios y archivado del audio
  con licencia una vez medido.

Instrucciones completas en [LEEME.md](LEEME.md).

## Requisitos
Windows 10/11, Python 3.12 y un cable de audio virtual (VB-Audio Virtual Cable).
`construir.bat` crea el entorno y el .exe.

## Créditos
Creado por Harmonic Solutions, 2026.

El cálculo de sonoridad ISO 532-1 (`loudness_lab/zwicker.py`) proviene de
MoSQITo 1.2.1, bajo licencia Apache 2.0 (ver `loudness_lab/LICENCIA_MOSQITO.txt`).
