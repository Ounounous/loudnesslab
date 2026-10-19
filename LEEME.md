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

La grabación corre en un proceso aparte: **cerrar la ventana no la detiene**, y al volver a abrir la app se reconecta. Si el PC se reinicia o se apaga, o el programa se cae, **la grabación se retoma sola** (ver «Si la grabación se corta»). Mientras graba, el PC no se suspende solo. Si la app de música deja de sonar, deja de llegar audio al cable o se acaba el disco, aparece un aviso en rojo.

Los números en vivo usan exactamente la misma medición que `analizar.py`; el análisis final sigue siendo el de la sección 5.

### Sesiones, pausar y detener
- **El nombre de la sesión es el de su carpeta** (`<carpeta de salida>\<nombre>`, sin fecha). Si ya existe, la app ofrece crear `<nombre>_2` o cambiar el nombre.
- **Pausar / Reanudar** (botón junto a «Detener») es para una pausa momentánea. Mientras dura, el audio **no se graba ni se mide**: lo que suene en ese rato no entra al experimento, y el reloj de la grabación no avanza. La sesión sigue en la misma carpeta. En el gráfico queda un hueco, y la canción que suena al reanudar se marca «empezó tras una pausa»: llega empezada, no se compara con la de antes de la pausa (no genera salto) y no se junta con ella. Si cierras la ventana en pausa, al volver a abrir la app se reconecta y sigue en pausa.
- **Detener es final.** Antes de detener, la app avisa. Una sesión detenida se guarda completa y se puede abrir, pero **no se puede continuar**: para otra grabación se crea una sesión nueva.
- **Sesiones…** (arriba) lista las sesiones de la carpeta de salida, con «Otra carpeta…» para abrir una que esté en otro lugar. Abrir una sesión muestra su gráfico, canciones, promedios, registro y la configuración con que se grabó (solo lectura), reconstruidos desde `mediciones.f32` y `pistas.csv` con la misma medición de la grabación, sin necesitar el audio. **«Nueva sesión»** parte con esos mismos ajustes y un nombre libre.
- **Al abrir la app** vuelve a la última sesión que tuviste abierta (o se conecta a la grabación en curso, si la hay, o retoma la que quedó cortada).

### Si la grabación se corta (reinicio, apagado, corte de luz, caída)
Una sesión queda **abierta hasta que tocas Detener** (o hasta que se cumple su duración). Si el motor de grabación muere antes, la sesión no termina: queda **cortada** y se **retoma sola**, en el mismo estado en que estaba (grabando o en pausa).
- **Quién la retoma**: la ventana, si está abierta (al instante) o cuando la abras; y si no, el **guardián**, una tarea del Programador de tareas de Windows (`\LoudnessLab\Guardian`) que la app registra sola la primera vez que se abre. Corre al iniciar sesión en Windows y cada 2 minutos; si no hay una sesión abierta, no hace nada. No es un servicio: el nombre de la canción y los reproductores solo existen con tu usuario adentro, así que **después de reiniciar, la grabación sigue cuando inicias sesión** (sin inicio de sesión automático). La app de música hay que volver a ponerla a sonar a mano.
- **Qué queda registrado**: el tramo sin grabar es un **corte**, con su hora de inicio y fin y el motivo, que se saca del registro de eventos de Windows («reinicio de Windows desde el menú Inicio (usuario …)», «apagado inesperado (corte de luz, batería agotada o el PC se colgó)», «el programa se cerró…», «el programa falló (…)»). Va en el registro, en `pistas.csv` (filas `corte` y `retoma`) y en `sesion.json` (`cortes`).
- **Se trata igual que una pausa**: en el gráfico queda un hueco, la canción que sonaba se cierra en el corte, la que suena al retomar se marca «empezó tras una pausa o un corte» y no hay salto a través del corte. `analizar.py` lo entiende igual (en su resumen: `cortes` y `segundos_en_cortes`).
- **No se pierde lo grabado**: el audio llega a disco al instante y las mediciones cada segundo. Al retomar se repara lo que quedó a medias: la cabecera del WAV que se estaba escribiendo, las mediciones que faltaron (se vuelven a medir desde el audio, partiendo un poco antes para que el filtro llegue igual) y la última línea a medias de los CSV. Lo único que se descarta es el resto de menos de 100 ms que no completa un bloque. Probado matando el motor de golpe, en pausa y por error: la medición en vivo, con los cortes, coincide con `analizar.py` bloque por bloque (diferencia 0,0000 dB) y las canciones y saltos son los mismos.
- **Si no se puede retomar** (por ejemplo, el cable desapareció o cambió de formato) la ventana lo dice en rojo, muestra la sesión como «Grabación cortada · se retoma sola» y lo vuelve a intentar cada minuto (el guardián, cada 2). **Detener** la da por terminada (repara sus archivos igual). Para empezar otra sesión, primero hay que detener la que quedó abierta: la app lo pregunta.
- **Sin internet** la grabación no se detiene: graba lo que llegue (silencio si el reproductor se quedó sin música) y sigue igual cuando vuelve. La medición no cambia.
- Solo se retoma la sesión que quedó anotada como abierta por esta versión (`%APPDATA%\LoudnessLab\sesion_abierta.json`); las sesiones viejas interrumpidas no se tocan.

### Radios web que no publican qué suena (Mystify)
Algunas radios web suenan en Chrome sin avisarle a Windows qué canción está sonando: el panel multimedia solo ve «Chrome». Pero su stream trae el título adentro (metadatos ICY: `StreamTitle='Artista - Título'`, cada ~0,7 s de audio). En **Configuración › Fuente**, elige **Mystify (radio web)**: se llena solo **Títulos de la radio (stream)** con `https://radio.mystify.cl:8230/radio.mp3` (para otra radio, pega la dirección de su stream; en Chrome está en el `<audio src=…>` de la página). Con ese campo lleno, los títulos ya no se leen de Windows sino del stream:
- El motor abre su propia conexión al stream solo para leer los títulos. Esa copia no va sincronizada con la que suena en Chrome (cada conexión tiene su retraso, unos segundos), así que **cada minuto compara un trozo de su copia con el audio que se graba** y mide el desfase exacto. Con eso, cada cambio de título se ubica en la muestra de la grabación donde ocurre y se anota en `pistas.csv` como cualquier otro cambio de canción. Probado con Mystify: correlación 0,99–1,00 entre ambas y el desfase no se movió (3,045 s durante 8 min).
- El álbum se completa desde la API pública de la radio (AzuraCast), cuando la tiene.
- `radio.csv` en la sesión registra cada título tal como llegó, cada medición del desfase (con su correlación) y dónde quedó cada cambio. `sesion.json` anota de dónde salieron los títulos (`titulos`).
- Si el stream no responde (sin internet), lo reintenta solo y avisa en rojo; la grabación del audio no se detiene.
- El chequeo previo comprueba que el stream responda y mande títulos.
- **El título llega antes que el borde real de la canción**, y no siempre lo mismo. Medido en Mystify (16 cambios, 55 min de stream): el hueco entre canciones cae a **+2,2 s** del título (mediana; la mitad de los casos entre +1,5 y +3,7 s; extremos −1,6 y +6,6 s). Por eso la fuente trae un **ajuste** («ajuste» junto a la dirección del stream, +2,0 s para Mystify, se llena solo): el cambio se anota 2 s después del título y ahí actúa el mismo ajuste fino que con cualquier fuente (busca la caída de nivel en ±3 s). Con eso el corte cae justo en el hueco en 12 de 15 cambios; en los otros queda a lo más ~4,6 s del hueco (sin el ajuste: 9 de 15, hasta 6,6 s). Es lo mismo que pasa con Spotify y Soundtrack, cuyo aviso llega ~1 s tarde y se corrige con ese mismo ajuste fino. El ajuste usado queda en `sesion.json` (`titulos`).
- La API pública de Mystify solo entrega las **últimas 5 canciones**; el historial completo pide usuario. Para tener el listado de un período, la propia grabación lo deja: el Excel de «Descargar resultados» trae Artista, Título y Álbum en columnas separadas, listas para armar la misma lista en Spotify (por ejemplo con un importador de listas desde CSV o Excel) y de ahí en Soundtrack.

### Gráfico de LUFS: zoom
- **Rueda del mouse**: acerca o aleja el tiempo, alrededor del puntero. **Ctrl + rueda**: acerca o aleja la escala de LUFS (útil para ver las diferencias de un par de LU entre canciones, que en la escala completa de −45 a 0 se aprietan).
- **Arrastrar** mueve el gráfico. Los botones **Tiempo −/+** y **LUFS −/+** hacen lo mismo que la rueda. **Doble clic** o **Restablecer vista** vuelven a la vista inicial.
- Con la grabación en curso, mientras el gráfico esté acercado o movido a mano **no se desplaza solo**: se queda en lo que estás mirando. «Restablecer vista» (o elegir 1 h / 6 h / Toda) vuelve a seguir lo último que se mide. No se puede acercar a menos de 20 s ni alejar más allá de la sesión.

### Descargar resultados (Excel)
El botón **Descargar resultados** (arriba a la derecha) guarda un `.xlsx` de la sesión que se está viendo o grabando, con cuatro hojas:
1. **Últimas canciones**: todas las canciones de la sesión (no solo las últimas que caben en la tabla), de la primera a la última, con las mismas columnas de la tabla más una «Nota» (corta / empezó tras una pausa o un corte). Los números son números, con filtros, y los saltos y true peak sobre el umbral van marcados como en la ventana. Un true peak en blanco es silencio digital.
2. **Promedios de la sesión**: el panel de la ventana, un valor por celda con su unidad, más el nombre, el inicio y las horas grabadas.
3. **Data LUFS**: los datos del gráfico, un punto por segundo de audio: hora, segundo de audio, short-term (3 s; −60 = silencio) y LUFS integrado de la canción en curso. Una pausa se nota como un salto en la hora.
4. **Registro**: el `registro.log` completo de la sesión. Excel no admite más de 32 767 caracteres en una celda, así que un registro largo (una sesión de 13 h ocupa unos 100 000) empieza en **A1 y sigue en A2, A3…**, cortado entre líneas; la app avisa cuántas celdas ocupó.

Por defecto se guarda en la carpeta de la sesión como `<sesión> - resultados.xlsx`, pero el diálogo deja elegir otro lugar. Si el archivo está abierto en Excel, avisa y no lo pisa.

### Qué deja cada sesión
Lo mismo que el script (WAV por hora, `pistas.csv`, `reloj.csv`, `sesion.json`), más:
- `mediciones.f32`: por cada bloque de 100 ms, energía K-ponderada por canal, pico de muestra y true peak. Con esto se recalcula cualquier métrica sin el audio.
- `registro.log`: lo mismo que el registro de la ventana.
- `latido.json`: cada 5 s, hasta dónde llegó la grabación y a qué hora. Si la grabación se corta, de ahí sale cuándo empezó el corte.
- En `pistas.csv`, las filas `pausa` y `reanuda` (mismo frame) marcan cada pausa. `sesion.json` trae la configuración completa (`config`), las pausas (`pausas`, con su duración) y `segundos_en_pausa`. `analizar.py` las entiende: corta la canción en la pausa, no calcula saltos a través de ella, y en `curva_100ms.csv` la hora real descuenta el tiempo pausado (el audio no lo incluye, el reloj sí).
- `picos/` y `picos.csv`: un recorte de 20 s por cada cambio de canción con un salto notorio (Configuración › Umbrales › «Marcar saltos desde», 3 LU por defecto, sube o baja): los últimos 10 s de la canción que terminó y los primeros 10 s de la que empezó, que son los tramos con que se mide el salto. El corte cae en el segundo 10 del recorte. El nombre lleva el número, la hora aproximada, el salto y los títulos (`0001_155413_+7.0LU_Beck - Loser_a_Olly Murs….wav`); si la ruta es muy larga, queda solo con número, hora y salto. `picos.csv` trae el detalle (títulos, LUFS de cada tramo, posición exacta en la grabación). El recorte aparece unos 30 s después del cambio, cuando la canción nueva cumple la duración mínima: una canción más corta no cuenta como cambio y no deja recorte. Son copias, la grabación completa queda intacta. Ocupan ~7 MB cada uno.

### Si algo falla
`registro.log` de la sesión, y en `%APPDATA%\LoudnessLab\`: `motor.log` (arranque del motor, y cada vez que el guardián retoma una sesión) y `caidas.log` (traza si el proceso se cae).

Para quitar el guardián de Windows (la app lo vuelve a registrar al abrirse): `schtasks /Delete /TN "\LoudnessLab\Guardian" /F`.

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

## Verificar que la medición es correcta
`verificar_medicion.py` pasa el **EBU loudness test set** (se baja de https://tech.ebu.ch/loudness y se descomprime en cualquier carpeta) por el mismo camino de medición que la grabación en vivo y compara con las tablas de EBU Tech 3341 y 3342. Además comprueba que medir en trozos o de una vez da lo mismo, que el medidor en vivo coincide con el análisis, y compara contra `ffmpeg` (filtro `ebur128`) con los programas del set y con los WAV que le pases en `--comparar`:
```
.venv\Scripts\python.exe verificar_medicion.py C:\ruta\al\set --comparar sesion.wav --salida informe.txt
```
Termina con código 1 si algo falla sin explicación. Los casos 13 y 14 (momentary de tonos de 400 ms) fallan a propósito de cómo está construida la medición: todo se calcula en bloques de 100 ms, y un tono corto que no empieza en esa rejilla se lee hasta 0,46 dB más bajo. El integrado y el LRA, que es lo que usa el experimento, pasan. Esto valida el algoritmo; para validar la cadena completa (app → cable → captura) hay que reproducir un archivo local por el cable y compararlo con `ffmpeg`.

## Notas técnicas
- Medición validada: los coeficientes del filtro K son los exactos de BS.1770 y un tono de 1 kHz a 0 dBFS en un canal mide −3,00 LUFS (referencia −3,01).
- El cambio de canción que informa Windows llega con ~1 s de retraso. El análisis busca el silencio real dentro de ±3 s y ajusta el corte (columna `corte_ajustado`). Con crossfade no hay silencio y se usa el momento informado.
- El grabador reproduce silencio digital en la misma salida para que Windows no deje de entregar audio en las pausas. Así el tiempo de las muestras no se desfasa del reloj.
- Canciones de menos de 30 s y transiciones con una pausa al medio se excluyen de las estadísticas.
- Esto mide la señal digital que entrega el servicio, no el sonido en la sala (parlante y acústica aparte).
