#!/usr/bin/env python3
"""Revisor de subtitulos para Jellyfin.

Uso:
  python jellyfin_subs.py                 # revisa CARPETA_ENTRADA (de RUTAS.txt) en modo simulacion
  python jellyfin_subs.py --aplicar       # ejecuta: renombra, crea carpetas y mueve a REV_SUBTITULOS
  python jellyfin_subs.py --carpeta "C:\\ruta" [--aplicar] [--descargar]

Flujo por cada video:
  1. Busca subtitulos externos que le correspondan (mismo nombre, mismo episodio,
     o unico video de la carpeta) y subtitulos internos (pistas del .mkv).
  2. Valida cada subtitulo: legible, en espanol (latino preferido), y que su
     duracion cuadre con la del video (ffprobe).
  3. Valido  -> lo mueve a REV_SUBTITULOS con estructura y nombres de Jellyfin.
     Invalido -> lo deja donde esta y lo agrega a SIN_SUBTITULOS.txt.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
RUTAS_TXT = RAIZ / "RUTAS.txt"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
CARPETA_DESTINO = "REV_SUBTITULOS"
REPORTE = "SIN_SUBTITULOS.txt"

# Jellyfin solo reconoce codigos ISO 639 (es / spa). Un codigo que no reconoce
# (ej. es-419) hace que tome TODO el sufijo como titulo y no detecte el idioma.
SUFIJO_IDIOMA = "spa"

# Tolerancias para comparar subtitulo vs video
MARGEN_FINAL_SEG = 180        # el sub puede terminar hasta 3 min despues del video
MIN_COBERTURA = 0.60          # el ultimo dialogo debe llegar al menos al 60% del video
MIN_DIALOGOS_POR_HORA = 150

# --------------------------------------------------------------------------- utilidades

def leer_rutas() -> dict[str, str]:
    rutas: dict[str, str] = {}
    if not RUTAS_TXT.exists():
        return rutas
    for linea in RUTAS_TXT.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        rutas[k.strip()] = v.strip().strip('"')
    return rutas


def ffprobe_bin(rutas: dict[str, str]) -> str | None:
    cand = rutas.get("FFPROBE")
    if cand and Path(cand).exists():
        return cand
    return shutil.which("ffprobe")


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", sin_acentos(s).lower())


def nombre_seguro(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    return re.sub(r"\s+", " ", s).strip(" .")


# --------------------------------------------------------------------------- nombres

RE_EPISODIO = [
    re.compile(r"(?i)\bS(\d{1,2})[ ._-]?E(\d{1,3})(?:-?E(\d{1,3}))?(?!\d)"),
    re.compile(r"(?i)\b(\d{1,2})x(\d{2,3})\b"),
    re.compile(r"(?i)\bTemporada[ ._-]?(\d{1,2})[ ._-]+(?:Cap(?:itulo)?|Ep(?:isodio)?)[ ._-]?(\d{1,3})"),
]
RE_ANIO = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
RE_BASURA = re.compile(
    r"(?i)\b(2160p|1080p|720p|576p|480p|4k|uhd|hdr10?|dv|x\.?26[45]|h\.?26[45]|hevc|avc|"
    r"blu-?ray|brrip|bdrip|bdremux|remux|webrip|web-?dl|web|hdrip|dvdrip|hdtv|amzn|nf|dsnp|hmax|"
    r"aac\d?(\.\d)?|ac3|eac3|dts(-hd)?|ddp?\d?(\.\d)?|truehd|atmos|10bit|8bit|proper|repack|"
    r"latino|castellano|spanish|espanol|dual|multi|subs?|subtitulado|yts|yify|rarbg)\b"
)


@dataclass
class Info:
    titulo: str
    anio: str | None = None
    temporada: int | None = None
    episodio: int | None = None
    episodio_fin: int | None = None

    @property
    def es_serie(self) -> bool:
        return self.episodio is not None


def analizar_nombre(stem: str) -> Info:
    s = re.sub(r"\[[^\]]*\]", " ", stem)            # quita [grupo]
    s = re.sub(r"[._]+", " ", s)
    for rx in RE_EPISODIO:
        m = rx.search(s)
        if m:
            titulo = s[: m.start()]
            anio = None
            ma = list(RE_ANIO.finditer(titulo))
            if ma:
                anio = ma[-1].group(1)
                titulo = titulo[: ma[-1].start()]
            fin = int(m.group(3)) if m.lastindex and m.lastindex >= 3 and m.group(3) else None
            return Info(limpiar_titulo(titulo), anio, int(m.group(1)), int(m.group(2)), fin)
    mb = RE_BASURA.search(s)
    if mb:
        s = s[: mb.start()]
    anio = None
    ma = list(RE_ANIO.finditer(s))
    # el anio es el ultimo numero de 4 cifras que no este al inicio (ej. "1917 (2019)")
    ma = [m for m in ma if m.start() > 0] or ma
    if ma and (ma[-1].start() > 0 or len(s.strip()) > 4):
        anio = ma[-1].group(1)
        s = s[: ma[-1].start()]
    return Info(limpiar_titulo(s), anio)


def limpiar_titulo(s: str) -> str:
    s = re.sub(r"[\(\)\[\]\{\}]", " ", s)
    s = re.sub(r"\s*-\s*$", "", s.strip())
    s = re.sub(r"\s+", " ", s).strip(" -")
    if s and (s.islower() or s.isupper()):
        s = s.title()
    return nombre_seguro(s) or "Sin titulo"


def ruta_destino(info: Info, ext: str, serie_titulo: str | None = None) -> tuple[Path, str]:
    """Devuelve (carpeta relativa, nombre base sin extension) segun convencion Jellyfin."""
    if info.es_serie:
        serie = serie_titulo or (f"{info.titulo} ({info.anio})" if info.anio else info.titulo)
        ep = f"S{info.temporada:02d}E{info.episodio:02d}"
        if info.episodio_fin:
            ep += f"-E{info.episodio_fin:02d}"
        return Path(serie) / f"Season {info.temporada:02d}", f"{info.titulo} {ep}"
    base = f"{info.titulo} ({info.anio})" if info.anio else info.titulo
    return Path(base), base


# --------------------------------------------------------------------------- subtitulos

RE_TS_SRT = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
RE_TS_VTT = re.compile(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})")
RE_ASS = re.compile(r"^Dialogue:\s*\d+,(\d+):(\d{2}):(\d{2})\.(\d{2}),(\d+):(\d{2}):(\d{2})\.(\d{2}),(.*)$", re.M)

PAL_ES = {"que", "de", "no", "la", "el", "en", "y", "es", "los", "se", "lo", "por", "un", "una", "con",
          "para", "mi", "te", "me", "esta", "pero", "como", "si", "ya", "eso", "aqui", "bien", "tu", "yo",
          "las", "del", "al", "hay", "estoy", "puedo", "quiero", "vamos", "nada", "todo", "donde"}
PAL_EN = {"the", "you", "i", "to", "and", "a", "it", "is", "that", "of", "what", "in", "me", "this",
          "we", "he", "for", "my", "your", "be", "have", "do", "are", "was", "not", "with", "just", "don"}
PAL_ESPANA = {"vosotros", "vosotras", "os", "vale", "tio", "tia", "ordenador", "gilipollas", "joder",
              "habeis", "teneis", "sois", "estais", "quereis", "podeis", "vais", "hostia", "coger",
              "movil", "chaval", "guay", "venga", "conducir", "zumo", "coche", "mola"}
PAL_LATINO = {"ustedes", "carro", "computadora", "celular", "ahorita", "chamaco", "guey", "wey", "plata",
              "manejar", "jugo", "chido", "pendejo", "lindo", "apurate", "platicar", "papas", "rentar",
              "chamba", "boleto", "departamento", "cuadra", "tomar"}


@dataclass
class Sub:
    ruta: Path | None
    interno: bool = False
    pista: str = ""
    dialogos: int = 0
    inicio: float = 0.0
    fin: float = 0.0
    codificacion: str = ""
    idioma: str = "?"          # es-419 / es / en / ?
    valido: bool = False
    motivo: str = ""
    puntaje: float = 0.0


def leer_texto(p: Path) -> tuple[str, str]:
    data = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), "desconocida"


def analizar_sub(p: Path) -> Sub:
    s = Sub(ruta=p)
    if p.suffix.lower() == ".sub":
        s.motivo = "formato .sub (VobSub/MicroDVD) no verificable; convertir a .srt"
        return s
    try:
        texto, s.codificacion = leer_texto(p)
    except OSError as e:
        s.motivo = f"no se pudo leer: {e}"
        return s

    tiempos: list[tuple[float, float]] = []
    lineas_texto: list[str] = []
    ext = p.suffix.lower()
    if ext in (".ass", ".ssa"):
        for m in RE_ASS.finditer(texto):
            g = [int(x) for x in m.groups()[:8]]
            tiempos.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 100, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 100))
            lineas_texto.append(re.sub(r"\{[^}]*\}", "", m.group(9)).replace("\\N", " "))
    else:
        rx = RE_TS_VTT if ext == ".vtt" else RE_TS_SRT
        for m in rx.finditer(texto):
            g = [int(x) if x else 0 for x in m.groups()]
            tiempos.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000))
        lineas_texto = [l for l in texto.splitlines()
                        if l.strip() and not rx.search(l) and not l.strip().isdigit()]

    s.dialogos = len(tiempos)
    if not tiempos:
        s.motivo = "no tiene dialogos con tiempos validos (archivo vacio o corrupto)"
        return s
    s.inicio = min(t[0] for t in tiempos)
    s.fin = max(t[1] for t in tiempos)
    s.idioma = detectar_idioma(" ".join(lineas_texto))
    return s


def detectar_idioma(texto: str) -> str:
    palabras = re.findall(r"[a-z]+", sin_acentos(texto.lower()))
    if len(palabras) < 30:
        return "?"
    es = sum(w in PAL_ES for w in palabras)
    en = sum(w in PAL_EN for w in palabras)
    if es <= en * 1.2:
        return "en" if en > es else "?"
    espana = sum(w in PAL_ESPANA for w in palabras)
    latino = sum(w in PAL_LATINO for w in palabras)
    if espana >= 3 and espana > latino * 1.5:
        return "es"
    return "es-419"


def idioma_por_nombre(nombre: str) -> str | None:
    n = sin_acentos(nombre.lower())
    if re.search(r"(?<![a-z])(lat|latino|latam|es-?419|es-?mx|es-?la)(?![a-z])", n):
        return "es-419"
    if re.search(r"(?<![a-z])(castellano|es-?es|esp|spain)(?![a-z])", n):
        return "es"
    if re.search(r"(?<![a-z])(en|eng|english|ingles)(?![a-z])", n):
        return "en"
    if re.search(r"(?<![a-z])(es|spa|spanish|espanol)(?![a-z])", n):
        return "es-419?"
    return None


def validar_contra_video(s: Sub, duracion: float | None) -> None:
    if s.motivo:
        return
    if s.idioma == "en":
        s.motivo = "el subtitulo esta en ingles"
        return
    if s.idioma == "?":
        s.motivo = "no se pudo confirmar que este en espanol"
        return
    if duracion:
        if s.fin > duracion + MARGEN_FINAL_SEG:
            s.motivo = (f"termina en {fmt(s.fin)} pero el video dura {fmt(duracion)} "
                        "(otra version/corte o FPS distinto)")
            return
        if s.fin < duracion * MIN_COBERTURA:
            s.motivo = (f"termina en {fmt(s.fin)} y el video dura {fmt(duracion)} "
                        "(incompleto o de otra version)")
            return
        if s.inicio > duracion * 0.25:
            s.motivo = f"el primer dialogo aparece hasta {fmt(s.inicio)} (desfase)"
            return
        minimo = max(20, int(MIN_DIALOGOS_POR_HORA * duracion / 3600 * 0.5))
        if s.dialogos < minimo:
            s.motivo = f"solo {s.dialogos} dialogos para {fmt(duracion)} de video (incompleto o solo forzados)"
            return
    s.valido = True
    s.puntaje = (3 if s.idioma == "es-419" else 2) + min(s.dialogos, 3000) / 10000
    if s.ruta and s.ruta.suffix.lower() == ".srt":
        s.puntaje += 0.5


def fmt(seg: float) -> str:
    seg = int(seg)
    return f"{seg // 3600}:{seg % 3600 // 60:02d}:{seg % 60:02d}"


# --------------------------------------------------------------------------- video

def probar_video(ff: str | None, p: Path) -> tuple[float | None, list[Sub]]:
    if not ff:
        return None, []
    try:
        r = subprocess.run([ff, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, []
    dur = None
    try:
        dur = float(d.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    internos = []
    for st in d.get("streams", []):
        if st.get("codec_type") != "subtitle":
            continue
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        lang = (tags.get("language") or "").lower()
        titulo = sin_acentos((tags.get("title") or "").lower())
        forzado = (st.get("disposition") or {}).get("forced") or "forced" in titulo or "forzad" in titulo
        if lang in ("spa", "es", "es-419", "es-mx") and not forzado:
            latino = lang in ("es-419", "es-mx") or re.search(r"lat|419|mex", titulo)
            internos.append(Sub(ruta=None, interno=True, pista=f"#{st.get('index')} {tags.get('title', '')}".strip(),
                                idioma="es-419" if latino else "es", valido=True,
                                puntaje=2.5 if latino else 1.5))
    return dur, internos


# --------------------------------------------------------------------------- emparejar

def subs_candidatos(video: Path, info: Info, subs_dir: list[Path], videos_dir: list[Path]) -> list[Path]:
    vn = norm(video.stem)
    out = []
    for sp in subs_dir:
        sn = norm(sp.stem)
        if sn.startswith(vn):
            out.append(sp)
            continue
        si = analizar_nombre(sp.stem)
        if info.es_serie and si.es_serie and (si.temporada, si.episodio) == (info.temporada, info.episodio):
            if norm(si.titulo) == norm(info.titulo) or not norm(si.titulo):
                out.append(sp)
                continue
        if not info.es_serie and not si.es_serie and norm(si.titulo) == norm(info.titulo) and len(videos_dir) > 1:
            out.append(sp)
    if not out and len(videos_dir) == 1:
        out = [sp for sp in subs_dir if not analizar_nombre(sp.stem).es_serie]
    return out


# --------------------------------------------------------------------------- proceso

@dataclass
class Resultado:
    video: Path
    info: Info
    duracion: float | None
    elegido: Sub | None = None
    descartados: list[Sub] = field(default_factory=list)
    destino_video: Path | None = None
    destino_sub: Path | None = None


def recolectar(entrada: Path) -> dict[Path, tuple[list[Path], list[Path]]]:
    """Agrupa videos y subtitulos por carpeta (incluye subcarpetas 'Subs')."""
    grupos: dict[Path, tuple[list[Path], list[Path]]] = {}
    for p in entrada.rglob("*"):
        if not p.is_file() or CARPETA_DESTINO in p.parts[len(entrada.parts):]:
            continue
        ext = p.suffix.lower()
        if ext not in VIDEO_EXT and ext not in SUB_EXT:
            continue
        if "sample" in p.stem.lower() and ext in VIDEO_EXT:
            continue
        carpeta = p.parent
        if ext in SUB_EXT and carpeta.name.lower() in ("subs", "subtitles", "subtitulos", "sub"):
            carpeta = carpeta.parent
        v, s = grupos.setdefault(carpeta, ([], []))
        (v if ext in VIDEO_EXT else s).append(p)
    return grupos


def titulo_serie_comun(resultados: list[Resultado]) -> dict[str, str]:
    """Une variantes del mismo titulo de serie y conserva el anio si alguno lo trae."""
    mapa: dict[str, str] = {}
    for r in resultados:
        if not r.info.es_serie:
            continue
        k = norm(r.info.titulo)
        nombre = f"{r.info.titulo} ({r.info.anio})" if r.info.anio else r.info.titulo
        if k not in mapa or ("(" in nombre and "(" not in mapa[k]):
            mapa[k] = nombre
    return mapa


def procesar(entrada: Path, aplicar: bool, ff: str | None) -> list[Resultado]:
    destino_raiz = entrada / CARPETA_DESTINO
    resultados: list[Resultado] = []
    for carpeta, (videos, subs) in sorted(recolectar(entrada).items()):
        for v in sorted(videos):
            info = analizar_nombre(v.stem)
            dur, internos = probar_video(ff, v)
            r = Resultado(v, info, dur)
            evaluados = []
            for sp in subs_candidatos(v, info, subs, videos):
                s = analizar_sub(sp)
                pista_nombre = idioma_por_nombre(sp.stem[len(v.stem):] if norm(sp.stem).startswith(norm(v.stem)) else sp.stem)
                if pista_nombre == "en" and s.idioma != "es" and s.idioma != "es-419":
                    s.idioma = "en"
                validar_contra_video(s, dur)
                evaluados.append(s)
            evaluados += internos
            validos = sorted([s for s in evaluados if s.valido], key=lambda s: -s.puntaje)
            r.elegido = validos[0] if validos else None
            r.descartados = [s for s in evaluados if s is not r.elegido]
            resultados.append(r)

    series = titulo_serie_comun(resultados)
    for r in resultados:
        if not r.elegido:
            continue
        rel, base = ruta_destino(r.info, r.video.suffix, series.get(norm(r.info.titulo)))
        r.destino_video = destino_raiz / rel / f"{base}{r.video.suffix.lower()}"
        if r.elegido.ruta:
            r.destino_sub = destino_raiz / rel / f"{base}.{SUFIJO_IDIOMA}{r.elegido.ruta.suffix.lower()}"

    if aplicar:
        for r in resultados:
            if r.destino_video:
                mover(r)
    return resultados


def mover(r: Resultado) -> None:
    r.destino_video.parent.mkdir(parents=True, exist_ok=True)
    if r.destino_video.exists():
        raise FileExistsError(f"Ya existe {r.destino_video}; no se sobrescribe.")
    shutil.move(str(r.video), str(r.destino_video))
    if r.destino_sub and r.elegido and r.elegido.ruta:
        texto, enc = leer_texto(r.elegido.ruta)
        if enc in ("utf-8-sig", "utf-8"):
            shutil.move(str(r.elegido.ruta), str(r.destino_sub))
        else:  # Jellyfin necesita UTF-8 para mostrar bien acentos y enes
            r.destino_sub.write_text(texto, encoding="utf-8")
            r.elegido.ruta.unlink()


def reporte(entrada: Path, resultados: list[Resultado], aplicar: bool) -> str:
    ok = [r for r in resultados if r.elegido]
    faltan = [r for r in resultados if not r.elegido]
    L = [f"Revision de subtitulos - {datetime.now():%Y-%m-%d %H:%M}",
         f"Carpeta: {entrada}",
         f"Modo: {'APLICADO' if aplicar else 'SIMULACION (nada se movio)'}",
         f"Videos: {len(resultados)} | con subtitulo valido: {len(ok)} | sin subtitulo: {len(faltan)}", ""]
    if ok:
        L.append("== CON SUBTITULOS (-> REV_SUBTITULOS) ==")
        for r in ok:
            e = r.elegido
            origen = f"interno pista {e.pista}" if e.interno else e.ruta.name
            L.append(f"[OK] {r.video.name}")
            L.append(f"     sub: {origen} ({e.idioma}{', ' + e.codificacion if e.codificacion else ''})")
            L.append(f"     ->  {r.destino_video.relative_to(entrada)}")
            if r.destino_sub:
                L.append(f"     ->  {r.destino_sub.relative_to(entrada)}")
        L.append("")
    if faltan:
        L.append("== SIN SUBTITULOS VALIDOS ==")
        for r in faltan:
            L.append(f"[--] {r.video.name}  (duracion {fmt(r.duracion) if r.duracion else '?'})")
            for s in r.descartados:
                L.append(f"     descartado {s.ruta.name if s.ruta else s.pista}: {s.motivo}")
            if not r.descartados:
                L.append("     no se encontro ningun archivo de subtitulos")
    return "\n".join(L)


# --------------------------------------------------------------------------- descarga opcional

def descargar(resultados: list[Resultado]) -> None:
    """Intenta bajar subtitulos (latino primero) con subliminal para los videos sin sub."""
    try:
        from babelfish import Language
        from subliminal import download_best_subtitles, save_subtitles, scan_video
    except ImportError:
        print("subliminal no esta instalado: pip install subliminal  (descarga omitida)")
        return
    for r in resultados:
        if r.elegido:
            continue
        try:
            vid = scan_video(str(r.video))
            for lang in (Language("spa", "MX"), Language("spa")):
                subs = download_best_subtitles([vid], {lang}).get(vid, [])
                if subs:
                    save_subtitles(vid, subs[:1], single=False)
                    print(f"Descargado ({lang}): {r.video.name}")
                    break
            else:
                print(f"Sin resultados en linea: {r.video.name}")
        except Exception as e:  # noqa: BLE001 - un fallo de red no debe detener el lote
            print(f"Error descargando {r.video.name}: {e}")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Revisa y organiza subtitulos para Jellyfin")
    ap.add_argument("--carpeta", help="Carpeta a revisar (por defecto CARPETA_ENTRADA de RUTAS.txt)")
    ap.add_argument("--aplicar", action="store_true", help="Mover/renombrar de verdad (sin esto solo simula)")
    ap.add_argument("--descargar", action="store_true", help="Intentar descargar subs faltantes y re-evaluar")
    ap.add_argument("--json", action="store_true", help="Salida JSON para el agente")
    a = ap.parse_args()

    rutas = leer_rutas()
    carpeta = a.carpeta or rutas.get("CARPETA_ENTRADA")
    if not carpeta:
        print("Falta la carpeta: usa --carpeta o define CARPETA_ENTRADA en RUTAS.txt")
        return 2
    entrada = Path(carpeta).expanduser()
    if not entrada.is_dir():
        print(f"No existe la carpeta: {entrada}")
        return 2
    ff = ffprobe_bin(rutas)
    if not ff:
        print("AVISO: ffprobe no encontrado; no se podra comparar la duracion sub vs video.")

    if a.descargar:
        descargar(procesar(entrada, False, ff))
    resultados = procesar(entrada, a.aplicar, ff)
    texto = reporte(entrada, resultados, a.aplicar)

    faltan = [r for r in resultados if not r.elegido]
    if a.aplicar:
        rep = entrada / REPORTE
        if faltan:
            rep.write_text(texto.split("== SIN SUBTITULOS VALIDOS ==")[-1].strip() + "\n", encoding="utf-8")
        elif rep.exists():
            rep.unlink()

    if a.json:
        print(json.dumps([{
            "video": str(r.video), "serie": r.info.es_serie, "titulo": r.info.titulo, "anio": r.info.anio,
            "duracion": r.duracion, "valido": bool(r.elegido),
            "sub": (str(r.elegido.ruta) if r.elegido and r.elegido.ruta else (r.elegido.pista if r.elegido else None)),
            "idioma": r.elegido.idioma if r.elegido else None,
            "destino_video": str(r.destino_video) if r.destino_video else None,
            "destino_sub": str(r.destino_sub) if r.destino_sub else None,
            "descartados": [{"sub": str(s.ruta) if s.ruta else s.pista, "motivo": s.motivo} for s in r.descartados],
        } for r in resultados], ensure_ascii=False, indent=2))
    else:
        print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
