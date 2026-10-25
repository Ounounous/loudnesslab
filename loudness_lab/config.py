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

# Servicio que se mide, según la fuente (S2 del plan v0.2)
SERVICIO_POR_FUENTE = {"Soundtrack": "Soundtrack", "Spotify": "Spotify", "Mystify (radio web)": "Mystify"}

# Normalización del servicio: lo que se elige → (activa, nivel). «Sin niveles»: el servicio la tiene pero
# no ofrece elegir nivel (Soundtrack). «No aplica»: una radio, que no normaliza por canción.
NORMALIZACIONES = {
    "Activada · Normal": (True, "Normal"),
    "Activada · Silencioso": (True, "Silencioso"),
    "Activada · Fuerte": (True, "Fuerte"),
    "Activada (sin niveles)": (True, "Sin niveles"),
    "Desactivada": (False, "No aplica"),
    "No aplica": (None, "No aplica"),
}
# Lo habitual de cada fuente (se puede cambiar): normalización, automix, crossfade
CONDICIONES_POR_FUENTE = {
    "Spotify": ("Activada · Normal", "Sí", 0.0),
    "Soundtrack": ("Activada (sin niveles)", "No aplica", 0.0),
    "Mystify (radio web)": ("No aplica", "No aplica", 0.0),
}


def servicio_de(fuente):
    return SERVICIO_POR_FUENTE.get(fuente or "", "Otro")


def normalizacion_de(activa, nivel):
    """El texto de NORMALIZACIONES que corresponde a (activa, nivel)."""
    for texto, par in NORMALIZACIONES.items():
        if par == (activa, nivel):
            return texto
    return "Activada · Normal" if activa else "Desactivada"


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
    normalizacion: str = ""        # (texto, sesiones anteriores a v0.2; ahora se deriva de los campos de abajo)
    crossfade: str = ""            # (texto, ídem)
    calidad: str = ""
    # condiciones del servicio como campos (S2 del plan v0.2)
    normalizacion_activa: object = True     # True | False | None (no aplica)
    nivel_normalizacion: str = "Normal"     # Silencioso | Normal | Fuerte | Sin niveles | No aplica
    crossfade_s: float = 0.0
    automix: str = "Sí"                     # Sí | No | No aplica
    volumen_app_pct: float = 100.0          # volumen de la app o de la zona
    orden: str = "Fijo"                     # Fijo | Aleatorio
    captura_ajustes: str = ""               # imagen con los ajustes del servicio (se copia a la sesión)
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
    nivel_escucha_db_spl: float = 65.0   # sonoridad percibida: una señal a −14 LUFS suena a esto en la mesa
    phon_indice: float = 60.0            # índice rápido LUFS–oído (ISO 226): nivel de escucha en phon
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
