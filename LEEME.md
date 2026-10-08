# Experimento de loudness: Soundtrack vs. Spotify

Graba durante horas o días lo que suena en el PC, registra cada cambio de canción y mide el volumen percibido con la norma ITU-R BS.1770-4 / EBU R128 (LUFS).

## 1. Instalación (una vez)
1. Tener Python 3.10 o superior (python.org, marcando "Add Python to PATH").
2. Doble clic en `1_instalar.bat`. Al final muestra tus salidas de audio.

## 2. Preparar Windows (antes de cada sesión, no tocar durante la grabación)
- Volumen de Windows al **100%** y volumen dentro de Soundtrack al **100%**.
- Configuración > Sistema > Sonido > tu salida > **Mejoras de audio: desactivadas**. También desactivar "Ecualización de sonoridad" y el sonido espacial si aparecen.
- Desactivar notificaciones (modo "No molestar") y no usar ese PC para otra cosa mientras graba. Cualquier sonido que suene en el PC queda en la grabación.
- Configurar el PC para que **no entre en suspensión**.
- Anotar en "Notas" la playlist, si la normalización está activa y la configuración de crossfade.

## 3. Probar que se lee la canción
Doble clic en `2_probar_metadata.bat` con Soundtrack sonando y cambia de canción un par de veces.
- Si aparece el artista y el título, todo bien. Fíjate en el nombre `app` que muestra.
- Si la app de Soundtrack **no aparece**, Windows no está recibiendo su metadata. La grabación funciona igual, pero sin cortes por canción (avísame y armamos detección automática de cambios).

## 4. Grabar
Doble clic en `3_grabar.bat` y escribe una etiqueta (ej. `soundtrack_lounge`), notas, la app (`soundtrack`) y horas (0 = hasta Ctrl+C).
- Ocupa ~1,4 GB por hora (~33 GB por día). Al iniciar muestra cuántas horas caben.
- Crea `grabaciones/<etiqueta>_<fecha>/` con:
  - `audio_0001_....wav`, `audio_0002_...` (uno por hora, 32-bit float)
  - `pistas.csv`: cada cambio de canción con la hora del PC y la posición exacta en muestras
  - `reloj.csv`: cada minuto compara el reloj del PC con el audio grabado (desfase)
  - `sesion.json`: dispositivo, formato, notas, inicio y fin
- Cada minuto imprime horas grabadas, desfase y "overflows". Si los overflows suben, se perdió audio: cierra otros programas.
- Para Spotify: misma configuración; en "App a seguir" escribe `spotify` y usa una etiqueta `spotify_...`.

## 5. Analizar
Doble clic en `4_analizar.bat` (analiza todas las sesiones y las compara). O por consola:
```
python analizar.py grabaciones\soundtrack_lounge_20261007_120000
python analizar.py "grabaciones\*" --sin-true-peak     (más rápido)
```
Por sesión, en `analisis/`:
| Archivo | Contenido |
|---|---|
| `resumen.txt` | Lo importante: dispersión entre canciones, saltos en transiciones, peores 10 cambios |
| `pistas_metricas.csv` | Por canción: LUFS integrado, LRA, true peak, LUFS de los primeros/últimos 10 s |
| `transiciones.csv` | Salto en LU de cada cambio de canción |
| `curva_100ms.csv` | Momentary, short-term y true peak cada 100 ms con hora real |
| `curva_loudness.png`, `histograma_canciones.png`, `saltos_transiciones.png` | Gráficos |
| `audio_XXXX_etiquetas.txt` | Marcadores para Audacity (Archivo > Importar > Etiquetas) con nombre, LUFS y "!! SALTO" en los cambios fuertes |

Con más de una sesión, `comparacion/` trae la tabla y el gráfico lado a lado.

## Cómo leer los números
- **LUFS integrado por canción**: el volumen percibido promedio. La métrica clave es la *desviación* y el *rango* entre canciones, no el valor absoluto.
- **Salto en transición**: diferencia entre los últimos 10 s de una canción y los primeros 10 s de la siguiente. Referencia aproximada: < 1 LU casi imperceptible, 2–3 LU se nota, > 4–5 LU alguien va a ir a tocar el volumen.
- **True peak** sobre −1 dBTP: riesgo de distorsión en el parlante o en la conversión.
- **LRA**: dinámica interna de la canción (canciones con LRA alto tienen partes muy bajas y muy altas aunque su promedio sea "normal").

## Notas técnicas
- Medición validada: los coeficientes del filtro K son los exactos de BS.1770 y un tono de 1 kHz a 0 dBFS en un canal mide −3,00 LUFS (referencia −3,01).
- El cambio de canción que informa Windows llega con ~1 s de retraso. El análisis busca el silencio real dentro de ±3 s y ajusta el corte (columna `corte_ajustado`). Con crossfade no hay silencio y se usa el momento informado.
- El grabador reproduce silencio digital en la misma salida para que Windows no deje de entregar audio en las pausas. Así el tiempo de las muestras no se desfasa del reloj.
- Canciones de menos de 30 s y transiciones con una pausa al medio se excluyen de las estadísticas.
- Esto mide la señal digital que entrega el servicio, no el sonido en la sala (parlante y acústica aparte).
