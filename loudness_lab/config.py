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
    "Otra": "",
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
