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

### Estudio: qué canciones cuentan, alertas y datos de cada sesión (v0.2)
Las definiciones vienen del informe del 20-oct (el plan v0.2 y sus datos quedan en el equipo del estudio, en `docs/plan_v0.2/`, fuera del repositorio) y están en un solo lugar, `loudness_lab/estudio.py`: el panel en vivo, las sesiones reabiertas y el Excel cuentan igual. `tests/test_estudio.py` comprueba que la app reproduce los números del informe (por sesión, por servicio con intervalos, y canciones pareadas) y que la alerta salta donde debe.
- **Canciones válidas:** solo cuentan las que no son cortas, no son **parciales** (la primera y la última de la sesión, y las pegadas a una pausa o un corte, que llegan o se van empezadas) y no caen en un **tramo excluido**. Las repetidas se cuentan aparte («distintas»). En la tabla, las que no cuentan van en gris con su motivo.
- **Promedios de la sesión:** mediana y dispersión; **% a ±0,5 y ±1 LU de la mediana**; **|ΔI| > 1 LU** (LUFS integrado de la canción nueva − el de la anterior, en los cambios entre canciones válidas). Entre corchetes, el **intervalo de confianza al 95 %** (bootstrap) y n junto a cada número. El salto en el borde (10 s / 10 s) sigue, pero ya no es el titular.
- **Alerta de cambio de estado** (en rojo, en el registro y como fila `alerta` en `pistas.csv`): si el nivel de las últimas 3 canciones se aleja más de 2 LU de las 10 anteriores, o si los cambios de canción pasan de encimados a silencios (o al revés). Es lo que habría avisado el error de operación de Dinner and drinks (18-oct, 12:50 y 12:53). La de nivel no aplica sin normalización ni a radios.
- **Corte incierto (radios web):** si el ajuste fino no encontró un hueco, el cambio se marca «corte incierto»: no se calculan con él el salto en el borde ni el silencio, y no deja recorte.
- **Condiciones del servicio como campos** (Configuración): normalización (activada y nivel, o no aplica), crossfade en segundos, Automix, volumen de la app o zona, orden de la lista y una captura de pantalla de los ajustes (se copia a la sesión). Se llenan con lo habitual de cada fuente. Quedan en `sesion.json` → `condiciones`.
- **Al iniciar:** la app no deja grabar si fuente y app no calzan (por ejemplo, fuente Soundtrack con la app de Spotify) y avisa si falta la lista. Si no hay una **prueba de cadena** correcta (±0,1 LU) con ese dispositivo en los últimos 30 min, o la **lista de chequeo** está incompleta, ofrece «Probar ahora» o «Grabar igual», que deja la sesión marcada (`grabada_sin_chequeo`). La prueba queda en `sesion.json` (`prueba_cadena`: medido, esperado, diferencia, ok, dispositivo, hora). La lista tiene dos ítems nuevos: volumen anotado y crossfade/Automix anotados.
- **Datos del estudio** (`sesion.json` → `estudio`: servicio, lista, si vale —válida, parcial, no válida—, tramos excluidos y notas): «Sesiones…» › **Datos del estudio…** los corrige en una sesión terminada (los tramos, en tiempo de audio h:mm:ss) y cada cambio queda en `ediciones`. **Cargar catálogo…** los carga desde un `catalogo_sesiones.json` sin pisar los que ya existen. El 20-oct se cargó el catálogo del informe en las sesiones de `Q:\LoudnessLab\grabaciones`, salvo «Spotify Alternative groove».

### Frecuencias y archivo (v0.2)
LUFS suma la energía con el filtro K, que a volumen de música de fondo cuenta casi completos los graves y los agudos extremos que el oído casi no oye. La **capa de frecuencias** mide, en la misma rejilla de 100 ms que LUFS, la energía de cada tercio de octava (29 bandas, 25 Hz–16 kHz) y la deja en `espectro.f32` (el bloque n de `espectro.f32` es el bloque n de `mediciones.f32`).
- **En vivo**, el panel **Frecuencias** de «En vivo» muestra las barras de los últimos 3 s, la línea roja de lo que suma LUFS (filtro K) y la verde punteada de lo que oye el oído a volumen de fondo (ISO 226, 60 phon por defecto: ver «Niveles de escucha» más abajo). Al lado, el reparto de la canción en curso (graves, medios-bajos, medios, agudos) y su **desajuste LUFS–oído**: + suena más de lo que dice su LUFS, − suena menos (ruido rosa = 0). Sirve para ordenar canciones, no como valor absoluto. Medido con música real: lo que mide el motor en vivo es igual a lo que sale del WAV (0,0 dB).
- **En una sesión guardada**, el panel muestra el promedio de sus canciones válidas y cuánto varía el desajuste entre ellas. La tabla trae dos columnas nuevas, **Graves** (% de la energía en 25–200 Hz) y **Desaj.** (dB), y el Excel una hoja **Espectro por canción**.
- **Sesiones de antes de la v0.2** (o una retomada de una de ellas) no tienen la capa: el panel lo dice y ofrece **Generar capa de frecuencias…**, que la calcula desde el audio. Corre aparte, con prioridad baja (unos 2,3 min por hora de audio), así que se puede seguir grabando; el avance se ve en el diálogo.
- **«Sesiones…» › Frecuencias y archivo…** muestra lo que falta para archivar una sesión (✓/✗): que haya terminado, que tenga su audio, que las mediciones calcen con el audio (≤ 0,01 dB), que la capa esté completa y verificada, los títulos y los datos del estudio. **Verificar y generar capa de frecuencias** vuelve a medir todo el audio y lo compara con lo que quedó guardado (o genera la capa si falta); el resultado queda en `sesion.json` → `verificacion` y `espectro`. **Procesar todas…** hace lo mismo con todas las sesiones terminadas que lo necesiten, de a una. La columna «Audio y frecuencias» de la lista dice en qué está cada sesión.
- **Archivar** elimina los WAV de la sesión **de forma permanente (no van a la papelera)**, por los derechos del audio. Solo se puede con todos los requisitos en ✓, y pide confirmación. Quedan las mediciones, la capa, los títulos y los recortes de `picos/` (esos los borras tú si quieres): la sesión se sigue abriendo, exportando y analizando igual (`analizar.py` usa `mediciones.f32` cuando no hay WAV). Queda constancia en `sesion.json` → `audio_archivado`.
- Por consola: `LoudnessLab.exe --capa <carpeta de la sesión>` o `LoudnessLab.exe --capa --todas <carpeta de grabaciones>` (`--forzar` rehace `espectro.f32`).

### Cambios de canción y sonoridad percibida (v0.2, fase 3)
**Cómo cambia el volumen entre canciones.** El salto en el borde (últimos 10 s contra primeros 10 s) mide sobre todo intros y finales. Por eso se agregan, con las definiciones del informe del 20-oct (`loudness_lab/estudio.py`):
- **ΔI**: LUFS integrado de la canción nueva − el de la anterior. En los promedios, % de cambios con |ΔI| > 1 LU y > 3 LU.
- **Salto de cuerpo**: mediana del short-term de la canción nueva (de los 10 a los 70 s) − la del final de la anterior (sin sus últimos 10 s). Es lo que se nota cuando la canción ya está andando. % > 3 LU, en amarillo en la tabla (> 5 LU en rojo).
- **Subida de arranque**: máximo short-term de los 3 a los 15 s de la nueva − esa misma referencia. % > 5 LU y p95.
- **Short-term máximo** de cada canción sobre la mediana de la sesión: % de canciones > +5 LU (como el criterio de la EBU para música).
- **Silencio entre canciones**: mediana y % > 2 s.
- En los cortes inciertos de una radio no se calculan el arranque, el silencio ni el salto en el borde (dependen de dónde cae el corte); ΔI y cuerpo sí.

En la tabla van las columnas **ΔI** y **Cuerpo**; en el panel de promedios, las filas «Cambios de canción», «Cuerpo y arranque» y «Silencio entre canciones» (el detalle, al pasar el mouse); en el Excel, todo. `picos.csv` agrega `delta_i_lu` y `salto_cuerpo_lu` de cada recorte cuando termina la canción nueva (a los `picos.csv` de antes se les agregan las columnas solas). Comprobado: estas métricas, juntando las sesiones de cada servicio, dan exactamente los números del informe.

**Sonoridad percibida (Zwicker, ISO 532-1).** LUFS es una suma de energía ponderada; el oído no suma así (los graves a volumen de fondo cuentan menos, y un sonido de banda ancha suena más fuerte que uno angosto con la misma energía). La norma ISO 532-1 (método de Zwicker) estima cuánto suena de verdad, en sone. La app la calcula **una vez por segundo** desde la capa de frecuencias (la energía media de cada tercio de octava en ese segundo, método estacionario), así que funciona en vivo, en una sesión reabierta y en una archivada, con los mismos números:
- Por canción, en sus segundos con música: **N5** (la sonoridad superada el 5 % del tiempo, la que la norma sugiere para sonidos que cambian), la mediana de N y **LN**, el nivel de sonoridad del N5 en phon.
- **LUFS percibido** (columna **Percib.**): el LUFS al que un ruido rosa sonaría igual de fuerte que la canción. Así la sonoridad queda en la escala de LUFS. Como N5 toma los momentos fuertes de la canción y el ruido rosa es parejo, la música suele quedar sobre su LUFS (en «mystify_radio-en-vivo 24-10-2026», mediana +2,0 LU); lo que importa es la diferencia entre canciones: ahí, las dos con ~70 % de graves quedaron solo +0,2 y +0,7 LU sobre su LUFS, es decir, suenan menos de lo que su LUFS dice.
- En el gráfico, la línea verde **percibido** (se oculta con su casilla) va junto al integrado de cada canción.
- En los promedios, **Sonoridad percibida**: la desviación de la sonoridad percibida entre las canciones válidas, con su intervalo, junto a la desviación de sus LUFS. Esa es la métrica de la hipótesis del restorán: si es mayor que la de LUFS, a igual LUFS las canciones suenan distinto.
- En el panel Frecuencias en vivo: LN y LUFS percibido de la canción en curso, y el LN del último segundo.
- **Niveles de escucha**: la percepción depende de a qué volumen suena la música, y hay dos supuestos:
  - **Nivel de escucha (−14 LUFS =)**, para la sonoridad de Zwicker: a cuántos dB SPL (sin ponderar) suena una canción a −14 LUFS en el lugar de escucha. 65 dB SPL por defecto, de 40 a 95.
  - **Índice rápido LUFS–oído a**, para la columna Desaj. y la línea verde: el nivel de escucha en phon de las curvas ISO 226. 60 phon por defecto, de 20 a 90.
  - Para las grabaciones nuevas se eligen en Configuración › Umbrales y quedan en `sesion.json` → `sonoridad`; una sesión sin ese dato usa 65 dB SPL y 60 phon.
  - **Una sesión guardada se puede recalcular a otros niveles** desde el panel Frecuencias: «Calcular a: −14 LUFS = [ ] dB SPL · índice rápido a [ ] phon» y **Recalcular**. Cambian la percepción (Percib., Desaj., LN, la línea verde, los promedios y el Excel), no los LUFS. La vista recalculada se avisa en amarillo y la sesión no cambia, salvo con **Guardar en la sesión**, que deja registro en `ediciones`. El Excel dice con qué niveles se calculó (hoja 2).
  - Lo correcto es medirlos: una señal conocida a −14 LUFS por el sistema de sonido, al volumen de siempre, y un sonómetro en una mesa (ponderación Z o C; en dB(A) la música da unos 3 a 10 dB menos). Las recomendaciones de 35–45 dB(A) para oficinas suelen ser de ruido de fondo, no de la música. A menor nivel, más se nota que los graves suenan menos de lo que dice su LUFS.
- El algoritmo es el de MoSQITo 1.2.1 (Apache 2.0, copiado en `loudness_lab/zwicker.py` con su licencia), validado por sus autores con los casos de prueba de la norma; las pruebas de la app comprueban que la copia da lo mismo que el original, que un tono de 1 kHz a 40 dB SPL da 1 sone y que la calibración da 79,89 dB.
- **Validación con música** (`tests/validar_zwicker.py`, necesita el audio): en 30 s de 21 canciones de tres sesiones (Soundtrack, Spotify y Mystify), comparada con ISO 532-1 variable en el tiempo (MoSQITo, que sigue la sonoridad cada 2 ms con su ponderación temporal): el LN de la app queda en promedio 0,5 phon más alto (desv. 0,5; de −0,4 a +2,2), con correlación 0,99 y casi el mismo orden de las canciones (Spearman 0,97). La dispersión entre canciones sale algo menor que con el método variable (2,3 contra 2,7 phon): promediar cada segundo suaviza, así que una diferencia de sonoridad entre canciones que la app detecta es, si algo, un poco mayor en realidad.

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

### Gráfico de LUFS: canciones y zoom
- **Pasar el mouse** sobre el gráfico muestra la canción que está ahí: título, artista (y álbum si lo hay) y los mismos datos de su fila en «Últimas canciones» (inicio, duración, LUFS, LRA, true peak, salto, ΔI, cuerpo, percibido, graves, desajuste). Sobre la canción que suena, sus valores provisorios.
- **Clic** en el gráfico: esa canción queda elegida en la tabla (la tabla se desplaza hasta ella) y marcada en el gráfico con una franja amarilla. La tabla guarda las últimas 1000 canciones; una más antigua solo se marca en el gráfico.
- **Clic en una canción de la tabla**: se marca en el gráfico y el gráfico se acerca a ella, con la anterior y la siguiente. Como con cualquier zoom a mano, el gráfico deja de seguir lo último hasta «Restablecer vista».
- **Rueda del mouse**: acerca o aleja el tiempo, alrededor del puntero. **Ctrl + rueda**: acerca o aleja la escala de LUFS (útil para ver las diferencias de un par de LU entre canciones, que en la escala completa de −45 a 0 se aprietan).
- **Arrastrar** mueve el gráfico. Los botones **Tiempo −/+** y **LUFS −/+** hacen lo mismo que la rueda. **Doble clic** o **Restablecer vista** vuelven a la vista inicial.
- Con la grabación en curso, mientras el gráfico esté acercado o movido a mano **no se desplaza solo**: se queda en lo que estás mirando. «Restablecer vista» (o elegir 1 h / 6 h / Toda) vuelve a seguir lo último que se mide. No se puede acercar a menos de 20 s ni alejar más allá de la sesión.

### Descargar resultados (Excel)
El botón **Descargar resultados** (arriba a la derecha) guarda un `.xlsx` de la sesión que se está viendo o grabando, con cinco hojas:
1. **Últimas canciones**: todas las canciones de la sesión (no solo las últimas que caben en la tabla), de la primera a la última, con LUFS, LRA, true peak, salto en el borde, ΔI, salto de cuerpo, subida de arranque, silencio antes, LUFS percibido y LN, más una «Nota» (corta / empezó tras una pausa o un corte). Los números son números, con filtros, y los saltos y true peak sobre el umbral van marcados como en la ventana. Un true peak en blanco es silencio digital.
2. **Promedios de la sesión**: el panel de la ventana, un valor por celda con su unidad y su intervalo de confianza cuando lo tiene, más el nombre, el inicio y las horas grabadas. Al final, la sonoridad percibida (desviación entre canciones, la de sus LUFS, LN).
3. **Data LUFS**: los datos del gráfico, un punto por segundo de audio: hora, segundo de audio, short-term (3 s; −60 = silencio) y LUFS integrado de la canción en curso. Una pausa se nota como un salto en la hora.
4. **Registro**: el `registro.log` completo de la sesión. Excel no admite más de 32 767 caracteres en una celda, así que un registro largo (una sesión de 13 h ocupa unos 100 000) empieza en **A1 y sigue en A2, A3…**, cortado entre líneas; la app avisa cuántas celdas ocupó.
5. **Espectro por canción**: si la sesión tiene capa de frecuencias, una fila por canción con su reparto (graves, medios-bajos, medios, agudos, en %), su desajuste LUFS–oído, LUFS, LUFS percibido, LN, N5, mediana de N y la energía de cada tercio de octava en dB. Si no la tiene, lo dice.

Por defecto se guarda en la carpeta de la sesión como `<sesión> - resultados.xlsx`, pero el diálogo deja elegir otro lugar. Si el archivo está abierto en Excel, avisa y no lo pisa.

### Qué deja cada sesión
Lo mismo que el script (WAV por hora, `pistas.csv`, `reloj.csv`, `sesion.json`), más:
- `mediciones.f32`: por cada bloque de 100 ms, energía K-ponderada por canal, pico de muestra y true peak. Con esto se recalcula cualquier métrica sin el audio.
- `espectro.f32`: por cada bloque de 100 ms, la energía de los 29 tercios de octava (ver «Frecuencias y archivo»).
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

`transiciones.csv` trae también el salto de cuerpo, la subida de arranque y el silencio de cada cambio, y el resumen sus porcentajes (con las definiciones del estudio, pero sin sus reglas de canciones parciales: para eso está la app).

Una sesión archivada (sin WAV) se analiza desde `mediciones.f32`, que se verificó contra el audio antes de borrarlo: da los mismos números. `--desde-mediciones` lo hace aunque haya audio (mucho más rápido). Sin audio no hay etiquetas de Audacity.

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
