"""Las sesiones guardadas: listarlas y reabrir una sin necesitar su audio.

Una sesión es una carpeta con sesion.json (su nombre es el de la carpeta). Para mostrar una sesión
terminada se repasa mediciones.f32 y pistas.csv por el mismo Seguidor que la midió en vivo: los
números son los de la grabación, sin una segunda implementación.
"""

import csv
import datetime as dt
import json
import os

import numpy as np

from . import VERSION, protocolo
from .seguimiento import Seguidor
from .sistema import limpiar_nombre

NAN = float("nan")


def _json(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def es_sesion(carpeta):
    return bool(carpeta) and os.path.isfile(os.path.join(carpeta, "sesion.json"))


def _epoch(iso):
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _misma_carpeta(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def nombre_libre(base, nombre):
    """El nombre pedido o, si ya hay una sesión así, el mismo con _2, _3…"""
    nombre = limpiar_nombre(nombre)
    candidato, n = nombre, 1
    while os.path.exists(os.path.join(base, candidato)):
        n += 1
        candidato = f"{nombre}_{n}"
    return candidato


def listar(base):
    """Las sesiones de una carpeta, la más reciente primero."""
    vivo = protocolo.motor_vivo()
    en_curso = vivo.get("carpeta") if vivo else None
    out = []
    try:
        nombres = os.listdir(base)
    except OSError:
        return out
    for n in nombres:
        carpeta = os.path.join(base, n)
        s = _json(os.path.join(carpeta, "sesion.json"))
        if s is None:
            continue
        if en_curso and _misma_carpeta(carpeta, en_curso):
            estado = "en curso"
        else:
            estado = "terminada" if s.get("fin_pc") else "interrumpida"
        out.append({"nombre": s.get("etiqueta") or n, "carpeta": carpeta, "inicio": s.get("inicio_pc") or "",
                    "inicio_epoch": _epoch(s.get("inicio_pc")) or os.path.getmtime(carpeta),
                    "horas": float(s.get("segundos_totales") or 0) / 3600,
                    "canciones": int((s.get("resumen_en_vivo") or {}).get("canciones") or 0),
                    "estado": estado})
    out.sort(key=lambda x: x["inicio_epoch"], reverse=True)
    return out


def pausas_de(eventos, sr):
    """(segundo de audio, duración en s) de cada pausa de la grabación, según los eventos
    pausa/reanuda de pistas.csv. El audio de una pausa no se graba: el reloj del PC sigue y el audio no."""
    out, abierta = [], None
    for e in eventos:
        if e["evento"] == "pausa":
            abierta = e
        elif e["evento"] == "reanuda" and abierta is not None:
            try:
                dur = (dt.datetime.fromisoformat(e["hora_pc"])
                       - dt.datetime.fromisoformat(abierta["hora_pc"])).total_seconds()
            except (ValueError, TypeError, KeyError):
                dur = 0.0
            out.append((int(abierta["frame_global"]) / sr, dur))
            abierta = None
    if abierta is not None:   # la sesión terminó en pausa
        out.append((int(abierta["frame_global"]) / sr, 0.0))
    return out


def _config_de(s, carpeta):
    """La configuración con que se grabó. Las sesiones anteriores a esta versión no la guardaban
    completa: se arma con lo que sí quedó en sesion.json."""
    if isinstance(s.get("config"), dict):
        return dict(s["config"])
    cond = s.get("condiciones") or {}
    um = s.get("umbrales") or {}
    return {"carpeta_salida": os.path.dirname(os.path.normpath(carpeta)), "etiqueta": s.get("etiqueta", ""),
            "notas": s.get("notas", ""), "app": s.get("filtro_app", ""), "dispositivo": s.get("dispositivo", ""),
            "fuente": cond.get("fuente", ""), "playlist": cond.get("playlist", ""),
            "normalizacion": cond.get("normalizacion", ""), "crossfade": cond.get("crossfade", ""),
            "calidad": cond.get("calidad", ""), "checklist": s.get("checklist") or {},
            **{k: v for k, v in um.items() if v is not None}}


def _eventos(carpeta):
    ruta = os.path.join(carpeta, "pistas.csv")
    out = []
    try:
        with open(ruta, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    r["frame_global"] = int(r["frame_global"])
                except (ValueError, TypeError, KeyError):
                    continue
                out.append(r)
    except OSError:
        pass
    return out


def _log(carpeta, maximo=500):
    """Las últimas líneas de registro.log, con el formato corto de la ventana (sin la fecha)."""
    try:
        with open(os.path.join(carpeta, "registro.log"), encoding="utf-8", errors="replace") as f:
            lineas = f.read().splitlines()[-maximo:]
    except OSError:
        return []
    return [{"tipo": "log", "linea": l[11:] if len(l) > 11 and l[4] == "-" and l[10] == " " else l, "nivel": "INFO"}
            for l in lineas]


def reconstruir(carpeta):
    """Lo que la ventana recibe de un motor al conectarse (un «snapshot»), armado desde los archivos
    de una sesión que ya no graba."""
    s = _json(os.path.join(carpeta, "sesion.json"))
    if s is None:
        raise ValueError(f"«{carpeta}» no es una sesión: falta sesion.json.")
    cfg = _config_de(s, carpeta)
    sr, ch = int(s.get("sample_rate") or 48000), int(s.get("canales") or 2)
    blk = sr // 10
    seg = Seguidor(min_dur=float(cfg.get("min_dur") or 30), refinar_s=3.0,
                   umbral_salto=float(cfg.get("umbral_salto") or 3.0))

    try:
        datos = np.fromfile(os.path.join(carpeta, "mediciones.f32"), dtype=np.float32)
    except OSError:
        datos = np.zeros(0, dtype=np.float32)
    n = len(datos) // (ch + 2)   # una fila cortada por una caída se descarta
    datos = datos[:n * (ch + 2)].reshape(n, ch + 2)
    z = datos[:, :ch].astype(np.float64).sum(axis=1)
    sp, tp = datos[:, ch].astype(np.float64), datos[:, ch + 1].astype(np.float64)

    eventos = _eventos(carpeta)
    pos = 0
    for e in eventos:   # cada aviso llegó cuando ya se había medido hasta su posición
        b = min(e["frame_global"] // blk, n)
        if b > pos:
            seg.agregar(z[pos:b], sp[pos:b], tp[pos:b])
            pos = b
        seg.evento(e, b)
    if pos < n:
        seg.agregar(z[pos:], sp[pos:], tp[pos:])
    seg.terminar()

    idx = np.arange(9, n, 10)   # un punto por segundo, como en vivo
    pausas = [[t, d] for t, d in pausas_de(eventos, sr)]
    archivos = s.get("archivos") or []
    inicio = (_epoch(archivos[0].get("hora_pc_inicio")) if archivos else None) or _epoch(s.get("inicio_pc")) \
        or os.path.getmtime(carpeta)
    nombre = s.get("etiqueta") or os.path.basename(os.path.normpath(carpeta))
    estadisticas = seg.estadisticas()
    terminada = bool(s.get("fin_pc"))
    return {
        "tipo": "snapshot", "version": VERSION, "config": cfg, "carpeta": carpeta, "inicio_epoch": inicio,
        "log": _log(carpeta), "pistas": list(seg.cerradas), "pausas": pausas,
        "curva_t": (idx + 1) / 10.0, "curva_st": seg.st.v[idx],
        "estado": {"tipo": "estado", "grabando": False, "segundos": n / 10, "inicio_epoch": inicio,
                   "etiqueta": nombre, "carpeta": carpeta, "pausado": False, "pausa_s": 0.0,
                   "pausado_total_s": float(sum(d for _, d in pausas)), "momentary": NAN, "shortterm": NAN,
                   "pista": None, "estado_app": "", "sesion": estadisticas, "alertas": []},
        "cerrada": {"nombre": nombre, "terminada": terminada, "motivo": s.get("motivo_fin") or "",
                    "inicio_pc": s.get("inicio_pc") or "", "fin_pc": s.get("fin_pc") or "",
                    "horas": n / 36000, "canciones": estadisticas["canciones"],
                    "segundos_en_pausa": float(sum(d for _, d in pausas)), "pausas": len(pausas),
                    "dispositivo": s.get("dispositivo", ""), "app": cfg.get("app", ""),
                    "perdidas": int(s.get("overflows") or 0)},
    }
