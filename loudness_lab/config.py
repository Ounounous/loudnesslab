"""Configuración de la app, guardada entre usos en %APPDATA%\\LoudnessLab\\config.json."""

import json
from dataclasses import asdict, dataclass, field, fields

from .sistema import carpeta_app, carpeta_salida_por_defecto

# Fuente -> app que la publica en el panel multimedia de Windows (filtro por nombre)
APP_POR_FUENTE = {
    "Soundtrack": "soundtrack",
    "Spotify": "spotify",
    "YouTube (navegador)": "chrome",
    "Archivos locales": "zunemusic",   # Reproductor multimedia de Windows
    "Mystify (radio web)": "chrome",
    "Otra": "",
}

# Radios web que no publican el título en Windows: se lee de su stream (ver radio.py). `ajuste_s`: cuánto
# después del título suele estar el borde real entre canciones en esa radio; el ajuste fino de ±3 s busca
# el hueco alrededor de ese punto, como con cualquier otra fuente.
# Mystify (19-10-2026, 16 cambios): el hueco entre canciones cae a +2,2 s del título (mediana; cuartiles
# +1,5 … +3,7 s). Con +2,0 s el corte queda en el hueco en 12 de 15 cambios con hueco claro.
STREAM_POR_FUENTE = {
    "Mystify (radio web)": {"url": "https://radio.mystify.cl:8230/radio.mp3", "ajuste_s": 2.0},
}


@dataclass
class Config:
    carpeta_salida: str = field(default_factory=carpeta_salida_por_defecto)
    # condiciones del experimento (quedan en sesion.json)
    fuente: str = "Spotify"
    playlist: str = ""
    normalizacion: str = ""
    crossfade: str = ""
    calidad: str = ""
    notas: str = ""
    etiqueta: str = ""
    # captura
    app: str = "spotify"
    stream_url: str = ""           # radio web: los títulos se leen de su stream y no del panel de Windows
    ajuste_titulo_s: float = 0.0   # radio web: el borde entre canciones suele estar estos segundos después del título
    dispositivo: str = ""          # nombre de la salida que se graba (por defecto, el cable)
    horas: float = 0.0             # 0 = hasta detener
    # escucha
    monitor: bool = False
    monitor_dispositivo: str = ""  # "" = salida predeterminada de Windows
    monitor_vol: float = 0.5
    # umbrales
    umbral_salto: float = 3.0
    alerta_silencio_min: float = 2.0
    min_dur: float = 30.0
    checklist: dict = field(default_factory=dict)
    # la sesión con la que se abre la app (carpeta); no es parte de lo que se graba
    ultima_sesion: str = ""


def _ruta():
    return carpeta_app() / "config.json"


def cargar():
    try:
        datos = json.loads(_ruta().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Config()
    validos = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in datos.items() if k in validos})


def guardar(cfg):
    _ruta().write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
