# Experimento de loudness: Soundtrack vs. Spotify

Graba durante horas o días lo que suena en el PC, registra cada cambio de canción y mide el volumen percibido con la norma ITU-R BS.1770-4 / EBU R128 (LUFS).

## La app: Loudness Lab

Acceso directo **Loudness Lab** en el escritorio (es `dist\LoudnessLab\LoudnessLab.exe`). Reemplaza a `3_grabar.bat`: graba, mide en vivo y muestra todo en pantalla.

### Antes de la primera sesión: grabar sin que suene
1. En Windows: Configuración › Sistema › Sonido › **Mezclador de volumen** › la app de música (Spotify, Soundtrack, el navegador para YouTube…) › Dispositivo de salida: **CABLE Input (VB-Audio Virtual Cable)**.
2. Los parlantes siguen siendo la salida predeterminada: las notificaciones y cualquier otro sonido del PC salen por ahí y **no entran en la medición**. Se puede usar el PC mientras graba.
3. Para oír la música igual, marca **Escuchar** arriba en la app: manda una copia a los parlantes, con su propio volumen, sin tocar lo que se mide.

### Cada sesión
1. **Chequeo previo**: «Revisar ahora» verifica el cable, la salida predeterminada, que la app publique qué suena y el espacio en disco. «Probar la cadena de medición» reproduce un tono de −20 LUFS por el cable (no suena) y comprueba que se mida −20,0. Marca la lista manual: queda registrada en la sesión.
2. **Configuración**: carpeta de salida (por defecto `Q:\LoudnessLab\grabaciones`), nombre de la sesión y condiciones del experimento (fuente, playlist, normalización, crossfade, calidad). Todo queda en `sesion.json`.
3. **Iniciar grabación**. La pestaña **En vivo** muestra la canción actual, momentary y short-term, el gráfico con el nivel de cada canción y los cortes, la tabla de canciones con los saltos sobre el umbral marcados, los promedios de la sesión y el registro.

La grabación corre en un proceso aparte: **cerrar la ventana no la detiene**, y al volver a abrir la app se reconecta. Mientras graba, el PC no se suspende. Si la app de música deja de sonar, deja de llegar audio al cable o se acaba el disco, aparece un aviso en rojo.

Los números en vivo usan exactamente la misma medición que `analizar.py`; el análisis final sigue siendo el de la sección 5.

### Qué deja cada sesión
Lo mismo que el script (WAV por hora, `pistas.csv`, `reloj.csv`, `sesion.json`), más:
- `mediciones.f32`: por cada bloque de 100 ms, energía K-ponderada por canal, pico de muestra y true peak. Con esto se recalcula cualquier métrica sin el audio.
- `registro.log`: lo mismo que el registro de la ventana.

### Si algo falla
`registro.log` de la sesión, y en `%APPDATA%\LoudnessLab\`: `motor.log` (arranque del motor) y `caidas.log` (traza si el proceso se cae).

Para reconstruir el .exe después de cambiar el código: `construir.bat` (también rehace el acceso directo).

---

# Scripts originales (siguen funcionando)

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
