"""
analizar.py - Mide loudness (ITU-R BS.1770-4 / EBU R128) de una o varias sesiones grabadas con grabar.py.

Calcula, en bloques de 100 ms sobre toda la grabación (procesando por partes, sirve para días de audio):
  - Loudness momentáneo (ventana 400 ms) y short-term (ventana 3 s), en LUFS
  - True peak (sobremuestreo 4x) y sample peak, en dBTP / dBFS
Y por canción (usando pistas.csv):
  - LUFS integrado, LRA, true peak máx., loudness de los primeros y últimos 10 s
  - Salto de volumen en cada transición entre canciones

Uso:
    python analizar.py grabaciones/soundtrack_lounge_20261007_120000
    python analizar.py grabaciones/soundtrack_* grabaciones/spotify_*      (analiza y compara)

Salidas en <sesion>/analisis/ y, si hay más de una sesión, en ./comparacion/
"""

import argparse
import csv
import datetime as dt
import glob
import json
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly, sosfilt

ABS_GATE = -70.0
SILENCIO = -60.0  # bajo esto, una ventana short-term se considera silencio/pausa


# ----------------------------------------------------------------------------
# BS.1770: filtro K (coeficientes exactos para cualquier sample rate)
# ----------------------------------------------------------------------------
def filtro_k(sr):
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / sr)
    Vh = 10 ** (G / 20)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    s1 = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0,
          1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / sr)
    a0 = 1 + K / Q + K * K
    s2 = [1.0, -2.0, 1.0, 1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    return np.array([s1, s2])


def lufs(z):
    z = np.asarray(z, dtype=float)
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(np.maximum(z, 1e-20))


def media_movil(x, n):
    """Promedio de las últimas n posiciones (incluida la actual); NaN donde no hay n valores."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


# ----------------------------------------------------------------------------
# Paso 1: recorrer el audio y obtener energía K-ponderada y peaks cada 100 ms
# ----------------------------------------------------------------------------
def medir_bloques(carpeta, sesion, true_peak=True):
    archivos = [a["archivo"] for a in sesion.get("archivos", [])]
    if not archivos:
        archivos = sorted(os.path.basename(p) for p in glob.glob(os.path.join(carpeta, "audio_*.wav")))
    if not archivos:
        raise SystemExit(f"No hay archivos audio_*.wav en {carpeta}")

    sr = sf.info(os.path.join(carpeta, archivos[0])).samplerate
    blk = sr // 10
    sos = filtro_k(sr)
    zi = None
    resto = None
    ctx_tp = None
    z_list, sp_list, tp_list = [], [], []
    inicios = []
    total = 0

    for n, nombre in enumerate(archivos, 1):
        ruta = os.path.join(carpeta, nombre)
        info = sf.info(ruta)
        inicios.append({"archivo": nombre, "frame_global_inicio": total, "frames": info.frames})
        total += info.frames
        print(f"  [{n}/{len(archivos)}] {nombre}  ({info.frames / sr / 60:.1f} min)", flush=True)
        for x in sf.blocks(ruta, blocksize=blk * 100, dtype="float32", always_2d=True):
            if resto is not None and len(resto):
                x = np.vstack([resto, x])
            usable = (len(x) // blk) * blk
            resto = x[usable:]
            x = x[:usable].astype(np.float64)
            if not usable:
                continue
            if zi is None:
                zi = np.zeros((sos.shape[0], 2, x.shape[1]))
            y, zi = sosfilt(sos, x, axis=0, zi=zi)
            nb = usable // blk
            ms = (y.reshape(nb, blk, -1) ** 2).mean(axis=1)  # media cuadrática por canal
            z_list.append(ms.sum(axis=1))  # canales L/R con peso 1
            sp_list.append(np.abs(x).reshape(nb, blk, -1).max(axis=(1, 2)))
            if true_peak:
                pre = ctx_tp if ctx_tp is not None else np.zeros((0, x.shape[1]))
                xx = np.vstack([pre, x])
                up = np.abs(resample_poly(xx, 4, 1, axis=0))[len(pre) * 4:]
                tp_list.append(up.reshape(nb, blk * 4, -1).max(axis=(1, 2)))
                ctx_tp = x[-32:]
    z = np.concatenate(z_list)
    sp = np.concatenate(sp_list)
    tp = np.concatenate(tp_list) if true_peak else sp.copy()
    return sr, z, sp, tp, inicios


# ----------------------------------------------------------------------------
# Métricas BS.1770 / EBU 3342 sobre un rango de bloques de 100 ms [a, b)
# ----------------------------------------------------------------------------
def integrado(z, a, b):
    """LUFS integrado con gating absoluto (-70) y relativo (-10 LU), bloques de 400 ms con 75% solape."""
    if b - a < 4:
        return np.nan
    m = media_movil(z[a:b], 4)[3:]
    lm = lufs(m)
    m = m[lm > ABS_GATE]
    if not len(m):
        return np.nan
    rel = lufs(m.mean()) - 10
    m = m[lufs(m) > rel]
    return float(lufs(m.mean())) if len(m) else np.nan


def lra(z, a, b):
    """Loudness Range (EBU Tech 3342): percentiles 10-95 de short-term, gating -70 abs y -20 LU rel."""
    if b - a < 30:
        return np.nan
    s = media_movil(z[a:b], 30)[29:]
    ls = lufs(s)
    sel = ls > ABS_GATE
    if not sel.any():
        return np.nan
    rel = lufs(s[sel].mean()) - 20
    v = ls[sel & (ls > rel)]
    if len(v) < 2:
        return np.nan
    return float(np.percentile(v, 95) - np.percentile(v, 10))


def db(x):
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(x, 1e-12))


# ----------------------------------------------------------------------------
# Paso 2: pistas
# ----------------------------------------------------------------------------
def leer_pistas(carpeta, blk_frames):
    ruta = os.path.join(carpeta, "pistas.csv")
    if not os.path.exists(ruta):
        return []
    eventos = []
    with open(ruta, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["bloque"] = int(int(r["frame_global"]) / blk_frames)
            except (ValueError, TypeError):
                continue
            eventos.append(r)
    return eventos


def refinar_corte(mom, b, ventana):
    """Busca el punto más silencioso cerca del cambio reportado (el aviso de Windows llega con retraso).
    Solo mueve el corte si hay una caída clara (>= 6 LU bajo la mediana de la zona)."""
    a0, b0 = max(0, b - ventana), min(len(mom), b + ventana)
    zona = mom[a0:b0]
    if not len(zona) or np.all(np.isnan(zona)):
        return b, False
    i = int(np.nanargmin(zona))
    if np.nanmedian(zona) - zona[i] >= 6:
        return a0 + i, True
    return b, False


def segmentar(eventos, mom, n_bloques, refinar_s):
    """Convierte los eventos en segmentos [inicio, fin) por canción."""
    ventana = int(refinar_s * 10)
    cortes = []
    for e in eventos:
        if e["evento"] == "cambio_pista" and e.get("titulo"):
            b, mov = refinar_corte(mom, e["bloque"], ventana) if ventana else (e["bloque"], False)
            cortes.append((b, e, mov))
        elif e["evento"] in ("cambio_estado", "sin_sesion") and e.get("estado") != "reproduciendo":
            cortes.append((e["bloque"], dict(e, titulo=None), False))
    cortes.sort(key=lambda c: c[0])
    segs = []
    for i, (b, e, mov) in enumerate(cortes):
        fin = cortes[i + 1][0] if i + 1 < len(cortes) else n_bloques
        if e.get("titulo") and fin > b:
            segs.append({"ini": b, "fin": fin, "artista": e.get("artista", ""), "titulo": e["titulo"],
                         "album": e.get("album", ""), "app": e.get("app", ""), "corte_ajustado": mov})
    # unir segmentos consecutivos de la misma canción (pausa/reanudación)
    unidos = []
    for s in segs:
        if unidos and unidos[-1]["titulo"] == s["titulo"] and unidos[-1]["artista"] == s["artista"] \
                and s["ini"] - unidos[-1]["fin"] <= 1:
            unidos[-1]["fin"] = s["fin"]
        else:
            unidos.append(s)
    return unidos


# ----------------------------------------------------------------------------
# Análisis de una sesión
# ----------------------------------------------------------------------------
def fmt_t(seg):
    seg = int(round(seg))
    return f"{seg // 3600:d}:{seg % 3600 // 60:02d}:{seg % 60:02d}"


def analizar_sesion(carpeta, args):
    sesion = {}
    if os.path.exists(os.path.join(carpeta, "sesion.json")):
        with open(os.path.join(carpeta, "sesion.json"), encoding="utf-8") as f:
            sesion = json.load(f)
    etiqueta = sesion.get("etiqueta") or os.path.basename(os.path.normpath(carpeta))
    print(f"\n=== {etiqueta} ===")
    sr, z, sp, tp, archivos = medir_bloques(carpeta, sesion, true_peak=not args.sin_true_peak)
    n = len(z)
    mom = lufs(media_movil(z, 4))
    st = lufs(media_movil(z, 30))
    t = (np.arange(n) + 1) / 10.0  # fin de cada bloque, en s
    salida = os.path.join(carpeta, "analisis")
    os.makedirs(salida, exist_ok=True)

    inicio_pc = None
    if sesion.get("archivos"):
        try:
            inicio_pc = dt.datetime.fromisoformat(sesion["archivos"][0]["hora_pc_inicio"])
        except Exception:
            pass

    # Curva completa
    with open(os.path.join(salida, "curva_100ms.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "hora_pc", "momentary_lufs", "shortterm_lufs", "truepeak_dbtp", "samplepeak_dbfs"])
        tpd, spd = db(tp), db(sp)
        for i in range(n):
            hora = (inicio_pc + dt.timedelta(seconds=float(t[i]))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-5] \
                if inicio_pc else ""
            w.writerow([f"{t[i]:.1f}", hora,
                        "" if np.isnan(mom[i]) else f"{mom[i]:.2f}",
                        "" if np.isnan(st[i]) else f"{st[i]:.2f}",
                        f"{tpd[i]:.2f}", f"{spd[i]:.2f}"])

    # Pistas
    segs = segmentar(leer_pistas(carpeta, sr // 10), mom, n, args.refinar)
    pistas = []
    for k, s in enumerate(segs, 1):
        a, b = s["ini"], s["fin"]
        dur = (b - a) / 10
        p = {
            "n": k, "inicio": fmt_t(a / 10), "inicio_s": round(a / 10, 1), "duracion_s": round(dur, 1),
            "artista": s["artista"], "titulo": s["titulo"],
            "lufs_integrado": integrado(z, a, b), "lra_lu": lra(z, a, b),
            "truepeak_max_dbtp": float(db(tp[a:b].max())), "samplepeak_max_dbfs": float(db(sp[a:b].max())),
            "momentary_max_lufs": float(np.nanmax(mom[a + 4:b])) if b - a > 4 else np.nan,
            "shortterm_max_lufs": float(np.nanmax(st[a + 30:b])) if b - a > 30 else np.nan,
            "lufs_primeros_10s": integrado(z, a, min(b, a + 100)),
            "lufs_ultimos_10s": integrado(z, max(a, b - 100), b),
            "corte_ajustado": s["corte_ajustado"], "pista_corta": dur < args.min_dur,
        }
        pistas.append(p)

    transiciones = []
    for p1, p2 in zip(pistas, pistas[1:]):
        if p1["pista_corta"] or p2["pista_corta"]:
            continue
        if p2["inicio_s"] - (p1["inicio_s"] + p1["duracion_s"]) > 1:
            continue  # hubo una pausa entre medio
        transiciones.append({
            "t": p2["inicio"], "t_s": p2["inicio_s"],
            "de": f"{p1['artista']} - {p1['titulo']}", "a": f"{p2['artista']} - {p2['titulo']}",
            "salto_10s_lu": p2["lufs_primeros_10s"] - p1["lufs_ultimos_10s"],
            "salto_integrado_lu": p2["lufs_integrado"] - p1["lufs_integrado"],
        })

    escribir_csv(os.path.join(salida, "pistas_metricas.csv"), pistas)
    escribir_csv(os.path.join(salida, "transiciones.csv"), transiciones)

    # Resumen
    musica = st > SILENCIO
    st_m = st[musica & ~np.isnan(st)]
    med = float(np.median(st_m)) if len(st_m) else np.nan
    li = np.array([p["lufs_integrado"] for p in pistas if not p["pista_corta"]], dtype=float)
    li = li[~np.isnan(li)]
    sal = np.array([x["salto_10s_lu"] for x in transiciones], dtype=float)
    sal = sal[~np.isnan(sal)]
    tpd = db(tp)

    def stats(v):
        if not len(v):
            return {}
        return {"n": int(len(v)), "media": float(v.mean()), "desv_std": float(v.std(ddof=1)) if len(v) > 1 else 0.0,
                "min": float(v.min()), "p5": float(np.percentile(v, 5)), "mediana": float(np.median(v)),
                "p95": float(np.percentile(v, 95)), "max": float(v.max()), "rango": float(v.max() - v.min())}

    resumen = {
        "etiqueta": etiqueta, "carpeta": os.path.basename(os.path.normpath(carpeta)), "notas": sesion.get("notas", ""), "inicio_pc": sesion.get("inicio_pc"),
        "horas_grabadas": round(n / 36000, 2), "horas_con_musica": round(float(musica.sum()) / 36000, 2),
        "lufs_integrado_sesion": integrado(z, 0, n),
        "lra_sesion_lu": lra(z, 0, n),
        "truepeak_max_dbtp": float(tpd.max()),
        "bloques_truepeak_sobre_-1dbtp": int((tpd > -1).sum()),
        "shortterm_mediana_lufs": med,
        "pct_tiempo_shortterm_fuera_2lu": float(np.mean(np.abs(st_m - med) > 2) * 100) if len(st_m) else np.nan,
        "pct_tiempo_shortterm_fuera_4lu": float(np.mean(np.abs(st_m - med) > 4) * 100) if len(st_m) else np.nan,
        "pistas_analizadas": len(li),
        "lufs_integrado_por_pista": stats(li),
        "pct_pistas_dentro_1lu_mediana": float(np.mean(np.abs(li - np.median(li)) <= 1) * 100) if len(li) else np.nan,
        "pct_pistas_dentro_2lu_mediana": float(np.mean(np.abs(li - np.median(li)) <= 2) * 100) if len(li) else np.nan,
        "transiciones": len(sal),
        "salto_transicion_abs_lu": stats(np.abs(sal)),
        "transiciones_sobre_3lu": int((np.abs(sal) > 3).sum()),
        "transiciones_sobre_5lu": int((np.abs(sal) > 5).sum()),
    }
    with open(os.path.join(salida, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, ensure_ascii=False, default=float)
    texto = resumen_texto(resumen, transiciones)
    with open(os.path.join(salida, "resumen.txt"), "w", encoding="utf-8") as f:
        f.write(texto)
    print(texto)

    etiquetas_audacity(salida, archivos, sr, pistas, transiciones, args.umbral_etiqueta)
    if not args.sin_graficos:
        graficos(salida, etiqueta, t, st, pistas, transiciones, li)
    return resumen, li, sal


def escribir_csv(ruta, filas):
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        if not filas:
            f.write("sin datos\n")
            return
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        w.writeheader()
        for r in filas:
            w.writerow({k: (f"{v:.2f}" if isinstance(v, float) else v) for k, v in r.items()})


def resumen_texto(r, transiciones):
    def g(d, k):
        v = d.get(k)
        return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.1f}"
    L = r["lufs_integrado_por_pista"]
    S = r["salto_transicion_abs_lu"]
    lineas = [
        f"Sesión: {r['etiqueta']}   {r.get('notas') or ''}",
        f"Duración: {r['horas_grabadas']} h ({r['horas_con_musica']} h con música)",
        "",
        "GLOBAL",
        f"  LUFS integrado de toda la sesión: {g(r, 'lufs_integrado_sesion')} LUFS",
        f"  Loudness Range de la sesión:      {g(r, 'lra_sesion_lu')} LU",
        f"  True peak máximo:                 {g(r, 'truepeak_max_dbtp')} dBTP "
        f"({r['bloques_truepeak_sobre_-1dbtp']} bloques de 100 ms sobre -1 dBTP)",
        f"  Short-term mediano:               {g(r, 'shortterm_mediana_lufs')} LUFS",
        f"  % tiempo a más de ±2 LU / ±4 LU de la mediana: "
        f"{g(r, 'pct_tiempo_shortterm_fuera_2lu')}% / {g(r, 'pct_tiempo_shortterm_fuera_4lu')}%",
        "",
        f"POR CANCIÓN ({r['pistas_analizadas']} canciones)",
        f"  LUFS integrado: media {g(L, 'media')}, desv. {g(L, 'desv_std')}, "
        f"rango {g(L, 'min')} a {g(L, 'max')} ({g(L, 'rango')} LU), p5-p95 {g(L, 'p5')} a {g(L, 'p95')}",
        f"  Canciones a ±1 LU de la mediana: {g(r, 'pct_pistas_dentro_1lu_mediana')}%   "
        f"a ±2 LU: {g(r, 'pct_pistas_dentro_2lu_mediana')}%",
        "",
        f"TRANSICIONES ({r['transiciones']}) - salto entre últimos 10 s y primeros 10 s",
        f"  |salto| medio {g(S, 'media')} LU, p95 {g(S, 'p95')} LU, máx {g(S, 'max')} LU",
        f"  Saltos > 3 LU: {r['transiciones_sobre_3lu']}   > 5 LU: {r['transiciones_sobre_5lu']}",
    ]
    peores = sorted([x for x in transiciones if not np.isnan(x["salto_10s_lu"])],
                    key=lambda x: -abs(x["salto_10s_lu"]))[:10]
    if peores:
        lineas += ["", "  Peores 10:"]
        for x in peores:
            lineas.append(f"   {x['t']:>9}  {x['salto_10s_lu']:+5.1f} LU   {x['de'][:40]}  ->  {x['a'][:40]}")
    return "\n".join(lineas) + "\n"


def etiquetas_audacity(salida, archivos, sr, pistas, transiciones, umbral):
    """Un archivo de etiquetas por WAV (en Audacity: Archivo > Importar > Etiquetas)."""
    saltos = {x["t_s"]: x["salto_10s_lu"] for x in transiciones}
    for a in archivos:
        ini_s = a["frame_global_inicio"] / sr
        fin_s = ini_s + a["frames"] / sr
        lineas = []
        for p in pistas:
            t0 = p["inicio_s"]
            if not (ini_s <= t0 < fin_s):
                continue
            rel = t0 - ini_s
            txt = f"{p['artista']} - {p['titulo']}"
            if not np.isnan(p["lufs_integrado"]):
                txt += f" ({p['lufs_integrado']:.1f} LUFS)"
            s = saltos.get(t0)
            if s is not None and not np.isnan(s) and abs(s) >= umbral:
                txt = f"!! SALTO {s:+.1f} LU | " + txt
            lineas.append(f"{rel:.3f}\t{rel:.3f}\t{txt}")
        with open(os.path.join(salida, a["archivo"].replace(".wav", "_etiquetas.txt")), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lineas) + ("\n" if lineas else ""))


def graficos(salida, etiqueta, t, st, pistas, transiciones, li):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    horas = t / 3600
    fig, ax = plt.subplots(figsize=(16, 5))
    ax.plot(horas, st, lw=0.4, color="#4a78b5", label="Short-term (3 s)")
    for p in pistas:
        if not np.isnan(p["lufs_integrado"]):
            x0 = p["inicio_s"] / 3600
            ax.hlines(p["lufs_integrado"], x0, x0 + p["duracion_s"] / 3600, color="#d1495b", lw=1.6)
    ax.plot([], [], color="#d1495b", lw=1.6, label="Integrado por canción")
    ax.set_ylim(max(-50, np.nanpercentile(st, 1) - 3), min(0, np.nanmax(st) + 2))
    ax.set_xlabel("Horas desde el inicio")
    ax.set_ylabel("LUFS")
    ax.set_title(f"{etiqueta}: loudness a lo largo de la grabación")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(os.path.join(salida, "curva_loudness.png"), dpi=130)
    plt.close(fig)

    if len(li):
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.hist(li, bins=np.arange(np.floor(li.min()) - 0.5, np.ceil(li.max()) + 1, 0.5), color="#4a78b5")
        ax.axvline(np.median(li), color="#d1495b", ls="--", label=f"mediana {np.median(li):.1f}")
        ax.set_xlabel("LUFS integrado por canción")
        ax.set_ylabel("Canciones")
        ax.set_title(f"{etiqueta}: distribución de volumen entre canciones")
        ax.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(salida, "histograma_canciones.png"), dpi=130)
        plt.close(fig)

    if transiciones:
        x = np.array([tr["t_s"] for tr in transiciones]) / 3600
        y = np.array([tr["salto_10s_lu"] for tr in transiciones])
        fig, ax = plt.subplots(figsize=(16, 4))
        ax.axhspan(-3, 3, color="#9bc995", alpha=0.25, label="±3 LU")
        ax.scatter(x, y, s=12, color=np.where(np.abs(y) > 3, "#d1495b", "#4a78b5"))
        ax.axhline(0, color="k", lw=0.6)
        ax.set_xlabel("Horas desde el inicio")
        ax.set_ylabel("Salto (LU)")
        ax.set_title(f"{etiqueta}: salto de volumen en cada cambio de canción (últimos 10 s → primeros 10 s)")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right")
        fig.tight_layout()
        fig.savefig(os.path.join(salida, "saltos_transiciones.png"), dpi=130)
        plt.close(fig)


def comparar(resultados, destino):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    os.makedirs(destino, exist_ok=True)
    filas = []
    for r, li, sal in resultados:
        L, S = r["lufs_integrado_por_pista"], r["salto_transicion_abs_lu"]
        filas.append({
            "sesion": r["carpeta"], "horas": r["horas_grabadas"], "canciones": r["pistas_analizadas"],
            "lufs_sesion": r["lufs_integrado_sesion"], "lufs_cancion_media": L.get("media"),
            "lufs_cancion_desv": L.get("desv_std"), "lufs_cancion_rango": L.get("rango"),
            "pct_canciones_±2lu": r["pct_pistas_dentro_2lu_mediana"],
            "salto_abs_medio": S.get("media"), "salto_abs_p95": S.get("p95"), "salto_abs_max": S.get("max"),
            "saltos_>3lu": r["transiciones_sobre_3lu"],
            "pct_saltos_>3lu": (r["transiciones_sobre_3lu"] / r["transiciones"] * 100) if r["transiciones"] else None,
            "truepeak_max": r["truepeak_max_dbtp"], "pct_tiempo_±4lu": r["pct_tiempo_shortterm_fuera_4lu"],
        })
    escribir_csv(os.path.join(destino, "comparacion.csv"), filas)

    fig, axs = plt.subplots(1, 2, figsize=(14, 5))
    datos_l = [li for _, li, _ in resultados]
    datos_s = [sal for _, _, sal in resultados]
    nombres = [r["carpeta"] for r, _, _ in resultados]
    if any(len(d) for d in datos_l):
        axs[0].boxplot([d if len(d) else [np.nan] for d in datos_l], showfliers=True)
        axs[0].set_xticks(range(1, len(nombres) + 1), nombres, rotation=15)
    axs[0].set_title("LUFS integrado por canción")
    axs[0].set_ylabel("LUFS")
    axs[0].grid(alpha=0.3)
    if any(len(d) for d in datos_s):
        axs[1].boxplot([d if len(d) else [np.nan] for d in datos_s], showfliers=True)
        axs[1].set_xticks(range(1, len(nombres) + 1), nombres, rotation=15)
    axs[1].axhspan(-3, 3, color="#9bc995", alpha=0.25)
    axs[1].set_title("Salto en transiciones (LU)")
    axs[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(destino, "comparacion.png"), dpi=130)
    plt.close(fig)
    print(f"\nComparación guardada en {destino}/")
    for f in filas:
        print("  " + " | ".join(f"{k}: {v:.1f}" if isinstance(v, float) else f"{k}: {v}" for k, v in f.items()))


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Análisis de loudness BS.1770 / EBU R128 de sesiones grabadas.")
    ap.add_argument("sesiones", nargs="+", help="carpeta(s) de sesión creadas por grabar.py")
    ap.add_argument("--refinar", type=float, default=3.0,
                    help="segundos alrededor del cambio reportado donde buscar el corte real (0 = no ajustar)")
    ap.add_argument("--min-dur", type=float, default=30.0, help="canciones más cortas se excluyen de estadísticas")
    ap.add_argument("--umbral-etiqueta", type=float, default=3.0, help="LU para marcar saltos en Audacity")
    ap.add_argument("--sin-true-peak", action="store_true", help="más rápido; usa sample peak")
    ap.add_argument("--sin-graficos", action="store_true")
    ap.add_argument("--comparacion", default="comparacion", help="carpeta para la comparación")
    args = ap.parse_args()

    carpetas = []
    for s in args.sesiones:
        carpetas += [c for c in glob.glob(s) if os.path.isdir(c)] or [s]
    resultados = [analizar_sesion(c, args) for c in carpetas]
    if len(resultados) > 1:
        comparar(resultados, args.comparacion)


if __name__ == "__main__":
    sys.exit(main())
