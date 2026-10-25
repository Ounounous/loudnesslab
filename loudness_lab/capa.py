"""La capa de frecuencias de una sesión ya grabada, la verificación de sus datos contra el audio, y el
archivo de la sesión (borrar el audio cuando ya no hace falta). Plan v0.2, S6 y S12.

Una sola pasada por los WAV con el mismo medidor de la grabación (bandas=True):
  - compara la energía de cada bloque (la de LUFS) con mediciones.f32 (tolerancia 0,01 dB);
  - si espectro.f32 ya existe (lo midió la grabación en vivo), lo compara banda a banda;
  - si no existe (o se pide rehacerlo), lo escribe.
El resultado queda en sesion.json: «verificacion» y, con espectro, «espectro» (sus metadatos).

Archivar borra los WAV de forma permanente (no van a la papelera) solo si la sesión está terminada,
verificada contra su audio, con su capa de frecuencias, con títulos y con los datos del estudio. Los
recortes de picos/ no se tocan. Después, todo sigue funcionando con mediciones.f32 y espectro.f32.
"""

import datetime as dt
import glob
import json
import os
import time

import numpy as np

from . import sesiones
from .bandas import METADATOS, N_BANDAS
from .bs1770 import MedidorBloques

TOLERANCIA_DB = 0.01
PROGRESO = ".capa_progreso.json"   # en la carpeta de la sesión, mientras se procesa (lo lee la ventana)


def _ahora():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def wavs(carpeta, s=None):
    """Las rutas de los WAV de la sesión, en orden."""
    s = s if s is not None else (sesiones._json(os.path.join(carpeta, "sesion.json")) or {})
    nombres = [a["archivo"] for a in (s.get("archivos") or [])]
    if not nombres or not all(os.path.exists(os.path.join(carpeta, n)) for n in nombres):
        nombres = sorted(os.path.basename(p) for p in glob.glob(os.path.join(carpeta, "audio_*.wav")))
    return [os.path.join(carpeta, n) for n in nombres]


def filas_espectro(carpeta):
    ruta = os.path.join(carpeta, "espectro.f32")
    return os.path.getsize(ruta) // (N_BANDAS * 4) if os.path.exists(ruta) else 0


def leer_espectro(carpeta, a, b):
    """Las filas [a, b) de espectro.f32 (29 columnas), sin dejar el archivo abierto."""
    ruta = os.path.join(carpeta, "espectro.f32")
    a = max(0, int(a))
    n = max(0, int(b) - a)
    if not n or not os.path.exists(ruta):
        return np.zeros((0, N_BANDAS), dtype=np.float32)
    x = np.fromfile(ruta, dtype=np.float32, count=n * N_BANDAS, offset=a * N_BANDAS * 4)
    return x[:len(x) // N_BANDAS * N_BANDAS].reshape(-1, N_BANDAS)


def _db(x):
    with np.errstate(divide="ignore"):
        return 10 * np.log10(np.maximum(x, 1e-30))


def _progreso(carpeta, fraccion, texto):
    try:
        with open(os.path.join(carpeta, PROGRESO), "w", encoding="utf-8") as f:
            json.dump({"fraccion": fraccion, "texto": texto, "hora": time.time(), "pid": os.getpid()}, f)
    except OSError:
        pass


def procesar(carpeta, forzar=False, al_avanzar=None):
    """Verifica la sesión contra su audio y genera la capa de frecuencias si falta. Devuelve el informe
    (también queda en sesion.json → «verificacion»)."""
    ruta_s = os.path.join(carpeta, "sesion.json")
    s = sesiones._json(ruta_s)
    if s is None:
        raise ValueError(f"«{carpeta}» no es una sesión: falta sesion.json.")
    if not s.get("fin_pc") or not sesiones.editable(carpeta):
        raise RuntimeError("La sesión no está terminada (graba o quedó cortada): se procesa cuando termine.")
    if s.get("audio_archivado"):
        raise RuntimeError("La sesión ya fue archivada: su audio se eliminó y no se puede volver a medir.")
    sr, ch = int(s.get("sample_rate") or 48000), int(s.get("canales") or 2)
    blk = sr // 10
    rutas = wavs(carpeta, s)
    if not rutas:
        raise RuntimeError("La sesión no tiene audio (WAV): no se puede verificar ni generar su capa de frecuencias.")
    import soundfile as sf
    frames = sum(sf.info(r).frames for r in rutas)
    med = np.fromfile(os.path.join(carpeta, "mediciones.f32"), dtype=np.float32)
    n_med = len(med) // (ch + 2)
    z_med = med[:n_med * (ch + 2)].reshape(n_med, ch + 2)[:, :ch].astype(np.float64).sum(axis=1)
    if abs(frames // blk - n_med) > 1:
        raise RuntimeError(f"El audio no cubre lo medido: {frames / sr / 3600:.2f} h de WAV para "
                           f"{n_med / 36000:.2f} h de mediciones.")
    previo = filas_espectro(carpeta)
    comparar = previo > 0 and not forzar
    escribir = not comparar
    tmp = os.path.join(carpeta, "espectro.f32.tmp")
    salida = open(tmp, "wb") if escribir else None
    medidor = MedidorBloques(sr, ch, true_peak=False, bandas=True)
    pos = 0
    dif_z = dif_b = 0.0
    bloques_b = 0
    hechos = 0
    t0 = time.time()

    def comparar_filas(z, b):
        nonlocal pos, dif_z, dif_b, bloques_b
        k = len(z)
        if not k:
            return
        ref = z_med[pos:pos + k]
        n = min(k, len(ref))
        ok = (ref[:n] > 1e-10) & (z[:n] > 1e-10)
        if ok.any():
            dif_z = max(dif_z, float(np.abs(_db(z[:n][ok]) - _db(ref[:n][ok])).max()))
        if salida is not None:
            salida.write(b[:max(0, min(k, n_med - pos))].astype(np.float32).tobytes())
        if comparar and pos < previo:
            viejo = leer_espectro(carpeta, pos, min(pos + k, previo)).astype(np.float64)
            m = len(viejo)
            sel = (viejo > 1e-12) & (b[:m] > 1e-12)
            if sel.any():
                dif_b = max(dif_b, float(np.abs(_db(b[:m][sel]) - _db(viejo[sel])).max()))
            bloques_b += m
        pos += k

    try:
        for ruta in rutas:
            for x in sf.blocks(ruta, blocksize=sr * 10, dtype="float32", always_2d=True):
                zc, _, _, b = medidor.procesar(x)
                comparar_filas(zc.sum(axis=1), b)
                hechos += len(x)
                if al_avanzar:
                    al_avanzar(hechos / frames, f"{os.path.basename(ruta)} · {hechos / sr / 3600:.2f} de {frames / sr / 3600:.2f} h")
        zc, _, _, b = medidor.terminar()
        comparar_filas(zc.sum(axis=1), b)
    finally:
        if salida is not None:
            salida.close()
    ok_med = dif_z <= TOLERANCIA_DB and abs(pos - n_med) <= 1
    ok_esp = (dif_b <= TOLERANCIA_DB and abs(previo - n_med) <= 1) if comparar else ok_med
    informe = {"hora": _ahora(), "bloques_audio": int(pos), "bloques_mediciones": int(n_med), "frames": int(frames),
               "mediciones_dif_max_db": round(dif_z, 6), "mediciones_ok": bool(ok_med),
               "espectro": "comparado con el que ya tenía la sesión" if comparar else "generado desde el audio",
               "espectro_dif_max_db": round(dif_b, 6) if comparar else None, "espectro_ok": bool(ok_esp),
               "segundos": round(time.time() - t0, 1)}
    informe["ok"] = bool(ok_med and ok_esp)
    if escribir:
        if ok_med:
            os.replace(tmp, os.path.join(carpeta, "espectro.f32"))
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
            informe["espectro"] = "no se escribió: las mediciones no calzan con el audio"
    s = sesiones._json(ruta_s) or s
    s["verificacion"] = informe
    if ok_med and (escribir or comparar):
        s["espectro"] = {**METADATOS, **(s.get("espectro") or {}),
                         "origen": (s.get("espectro") or {}).get("origen", "en vivo") if comparar else "desde los WAV",
                         "verificado": bool(ok_esp), "filas": int(filas_espectro(carpeta))}
    sesiones._escribir_sesion(carpeta, s)
    return informe


def procesar_con_avance(carpeta, forzar=False):
    """Para el comando --capa: deja el avance en la carpeta de la sesión (lo muestra la ventana)."""
    _progreso(carpeta, 0.0, "Empezando…")
    try:
        inf = procesar(carpeta, forzar, lambda f, t: _progreso(carpeta, f, t))
        _progreso(carpeta, 1.0, "Listo" if inf["ok"] else "Terminó con diferencias")
        return inf
    except Exception as e:
        _progreso(carpeta, 1.0, f"Error: {e}")
        raise
    finally:
        try:
            time.sleep(1.5)     # que la ventana alcance a leer el final
            os.remove(os.path.join(carpeta, PROGRESO))
        except OSError:
            pass


def leer_progreso(carpeta):
    try:
        with open(os.path.join(carpeta, PROGRESO), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------ archivo
def _con_titulos(carpeta):
    import csv
    try:
        with open(os.path.join(carpeta, "pistas.csv"), encoding="utf-8", newline="") as f:
            return sum(1 for r in csv.DictReader(f) if r.get("evento") == "cambio_pista" and r.get("titulo"))
    except OSError:
        return 0


def requisitos(carpeta):
    """Lo que tiene que estar listo para archivar la sesión: [(clave, texto, cumplido, detalle)]."""
    s = sesiones._json(os.path.join(carpeta, "sesion.json")) or {}
    rutas = wavs(carpeta, s)
    archivada = bool(s.get("audio_archivado"))
    est = sesiones.estudio_de(s)
    ver = s.get("verificacion") or {}
    filas = filas_espectro(carpeta)
    n_med = os.path.getsize(os.path.join(carpeta, "mediciones.f32")) // ((int(s.get("canales") or 2) + 2) * 4) \
        if os.path.exists(os.path.join(carpeta, "mediciones.f32")) else 0
    titulos = _con_titulos(carpeta)
    terminada = bool(s.get("fin_pc")) and sesiones.editable(carpeta)
    vigente = bool(ver.get("ok")) and ver.get("bloques_mediciones") == n_med and (ver.get("hora") or "") >= (s.get("fin_pc") or "")
    gb = sum(os.path.getsize(r) for r in rutas) / 1e9
    return [
        ("terminada", "La sesión terminó (no está grabando ni cortada)", terminada, ""),
        ("audio", "Tiene su audio", bool(rutas) and not archivada,
         "ya archivada" if archivada else f"{len(rutas)} WAV, {gb:.1f} GB" if rutas else "sin WAV"),
        ("verificada", "Mediciones verificadas contra el audio (≤ 0,01 dB)", vigente,
         f"diferencia máx {ver.get('mediciones_dif_max_db')} dB · {ver.get('hora', '')[:16].replace('T', ' ')}"
         if ver else "sin verificar"),
        ("espectro", "Capa de frecuencias (espectro.f32) completa y verificada",
         filas > 0 and abs(filas - n_med) <= 1 and bool((s.get("espectro") or {}).get("verificado")) and vigente,
         f"{filas} de {n_med} bloques" if filas else "no existe"),
        ("titulos", "Títulos de las canciones", titulos > 0 or est.get("estado") in ("parcial", "no_valida"),
         f"{titulos} cambios de canción" if titulos else "sin títulos" + (" (la sesión no es válida para el estudio)" if est.get("estado") in ("parcial", "no_valida") else "")),
        ("estudio", "Datos del estudio (servicio y si vale)", bool(est.get("servicio") or est.get("estado") == "no_valida") and bool(est.get("estado")),
         " · ".join(x for x in (est.get("servicio"), est.get("estado")) if x) or "faltan"),
    ]


def estado_audio(carpeta):
    """Una línea para la lista de sesiones: si tiene audio, capa de frecuencias o ya está archivada."""
    s = sesiones._json(os.path.join(carpeta, "sesion.json")) or {}
    if s.get("audio_archivado"):
        return "archivada"
    prog = leer_progreso(carpeta)
    if prog:
        return f"procesando {100 * prog.get('fraccion', 0):.0f} %"
    rutas = wavs(carpeta, s)
    gb = sum(os.path.getsize(r) for r in rutas) / 1e9
    audio = f"WAV {gb:.1f} GB".replace(".", ",") if rutas else "sin WAV"
    esp = s.get("espectro") or {}
    if esp.get("verificado") and (s.get("verificacion") or {}).get("ok"):
        capa = "capa ✓"
    elif filas_espectro(carpeta):
        capa = "capa sin verificar"
    else:
        capa = "sin capa"
    return f"{audio} · {capa}"


def archivable(carpeta):
    return all(r[2] for r in requisitos(carpeta))


def archivar(carpeta):
    """Borra los WAV de la sesión de forma permanente (no a la papelera), si cumple todos los requisitos.
    Devuelve los bytes liberados. Deja constancia en sesion.json → «audio_archivado»."""
    faltan = [r[1] for r in requisitos(carpeta) if not r[2]]
    if faltan:
        raise RuntimeError("Todavía no se puede archivar: " + "; ".join(faltan) + ".")
    s = sesiones._json(os.path.join(carpeta, "sesion.json"))
    rutas = wavs(carpeta, s)
    total = 0
    borrados = []
    for r in rutas:
        total += os.path.getsize(r)
        os.remove(r)            # permanente: os.remove no pasa por la papelera
        borrados.append(os.path.basename(r))
    s["audio_archivado"] = {"hora": _ahora(), "archivos": borrados, "bytes": total,
                            "verificacion": s.get("verificacion"),
                            "nota": "Audio con licencia eliminado tras verificar las mediciones y la capa de frecuencias. "
                                    "Los recortes de picos/ se conservan."}
    sesiones._escribir_sesion(carpeta, s)
    return total


# ------------------------------------------------------------------------------ comando
def _prioridad_baja():
    """Para no quitarle procesador a una grabación en curso (el motor tiene que leer el audio a tiempo)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        # con los tipos declarados: el «proceso actual» es el identificador −1, que como int de 32 bits
        # llegaba mal y la llamada fallaba sin avisar (el proceso quedaba en prioridad normal)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def pendientes(base):
    """Las sesiones de `base` que se pueden procesar y todavía no están verificadas con su capa."""
    out = []
    for c in sorted(glob.glob(os.path.join(base, "*"))):
        s = sesiones._json(os.path.join(c, "sesion.json"))
        if not s or not s.get("fin_pc") or s.get("audio_archivado") or not wavs(c, s) or not sesiones.editable(c):
            continue
        if dict((k, ok) for k, _, ok, _ in requisitos(c)).get("espectro"):
            continue
        out.append(c)
    return out


def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="LoudnessLab --capa",
                                 description="Verifica sesiones contra su audio y genera su capa de frecuencias.")
    ap.add_argument("--capa", action="store_true")
    ap.add_argument("ruta", help="carpeta de una sesión, o con --todas, la carpeta de grabaciones")
    ap.add_argument("--todas", action="store_true", help="todas las sesiones de la carpeta que falten")
    ap.add_argument("--forzar", action="store_true", help="rehacer espectro.f32 aunque exista")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    _prioridad_baja()
    carpetas = pendientes(args.ruta) if args.todas else [args.ruta]
    codigo = 0
    for c in carpetas:
        print(f"== {os.path.basename(os.path.normpath(c))}", flush=True)
        try:
            inf = procesar_con_avance(c, args.forzar)
            print(f"   {'OK' if inf['ok'] else 'DIFERENCIAS'}: mediciones {inf['mediciones_dif_max_db']} dB · "
                  f"espectro {inf['espectro']} · {inf['segundos']} s", flush=True)
            codigo |= 0 if inf["ok"] else 2
        except Exception as e:
            print(f"   no se procesó: {e}", flush=True)
            codigo |= 1
    return codigo
