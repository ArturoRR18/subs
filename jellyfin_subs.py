#!/usr/bin/env python3
"""Revisor de subtitulos para Jellyfin.

Uso:
  py jellyfin_subs.py                 # revisa CARPETA_ENTRADA (de RUTAS.txt) en modo simulacion
  py jellyfin_subs.py --aplicar       # ejecuta: renombra, crea carpetas y mueve a REV_SUBTITULOS
  py jellyfin_subs.py --carpeta "C:\\ruta" [--aplicar] [--descargar] [--traducir] [--generar]

Flujo por cada video:
  1. Busca subtitulos externos que le correspondan (mismo nombre, mismo episodio,
     o unico video de la carpeta) y subtitulos internos (pistas del .mkv).
  2. Valida cada subtitulo: legible, en espanol (latino preferido), y que su
     duracion cuadre con la del video (ffprobe).
  3. Si un subtitulo en espanol falla por tiempos, intenta rescatarlo: reacomoda
     las lineas con horas imposibles y, si no basta, lo sincroniza con ffsubsync.
  4. Con --traducir, si no hay ninguno en espanol, toma un subtitulo en ingles (o
     italiano) que cuadre con el video y lo traduce (traducir_subs.py); con
     --descargar tambien los baja. El original se guarda como .eng/.ita.
  5. Con --generar, si sigue sin ninguno valido, transcribe el audio con Whisper
     (whisper_subs.py, GPU) y traduce al espanol si el audio esta en ingles.
  6. Valido  -> lo mueve a REV_SUBTITULOS con estructura y nombres de Jellyfin.
     Invalido -> lo deja donde esta y lo agrega a SIN_SUBTITULOS.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
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

# Rescate de subtitulos con tiempos rotos
MAX_LINEAS_ROTAS = 0.10       # si mas del 10% esta roto no se reacomoda (es otra version -> ffsubsync)
CACHE_SYNC = Path(tempfile.gettempdir()) / "jellyfin_subs_sync"

# Proveedores de subliminal (subtitulamos y subtis son de habla hispana).
# opensubtitlescom se agrega si hay usuario/clave en RUTAS.txt.
# opensubtitles = OpenSubtitles.org (API XML-RPC, funciona sin cuenta).
PROVEEDORES = ["opensubtitles", "podnapisi", "gestdown", "addic7ed", "tvsubtitles", "bsplayer", "subtitulamos", "subtis"]
DESCARGAS_POR_VIDEO = 4       # baja varios por si el mejor puntuado viene roto

# Subtitulos hechos por nosotros con Whisper (--generar): se reconocen por el nombre
MARCA_GENERADO = ".whisper"
# Traducidos de otro idioma (--traducir): "<original>.traducido.es.srt"
MARCA_TRADUCIDO = ".traducido"

# Idiomas que sirven de fuente para traducir, en orden de preferencia, y su sufijo
# ISO 639-2 para Jellyfin cuando se guarda el original junto al traducido.
IDIOMAS_FUENTE = ("en", "it")
SUFIJO_FUENTE = {"en": "eng", "it": "ita"}
DESCARGAS_FUENTE = 2          # menos que en espanol: solo hace falta uno que cuadre
NOMBRE_IDIOMA = {"en": "ingles", "it": "italiano", "ko": "coreano", "ja": "japones", "zh": "chino",
                 "ru": "ruso", "ar": "arabe", "he": "hebreo", "el": "griego", "th": "tailandes"}
CARPETAS_SUBS = ("subs", "subtitles", "subtitulos", "sub")

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
    return usar_nas_alterno(rutas)


# Claves cuyo host es el NAS (ruta UNC \\host\... o URL http://host:puerto)
CLAVES_NAS = ("CARPETA_ENTRADA", "BIBLIOTECA_PELICULAS", "BIBLIOTECA_SERIES", "JELLYFIN_URL")
_HOST_RE = re.compile(r"^(\\\\|[a-z]+://)([^\\/:]+)", re.I)


def _host_responde(host: str, puertos=(445, 80, 443, 8096, 8899), espera: float = 1.5) -> bool:
    import socket
    for p in puertos:
        try:
            with socket.create_connection((host, p), timeout=espera):
                return True
        except OSError:
            continue
    return False


def usar_nas_alterno(rutas: dict[str, str]) -> dict[str, str]:
    """Si el NAS principal no responde y hay NAS_ALTERNO (ej. IP de Tailscale/VPN),
    cambia el host en CARPETA_ENTRADA, BIBLIOTECA_PELICULAS/SERIES y JELLYFIN_URL."""
    alterno = rutas.get("NAS_ALTERNO", "").strip().strip("\\/")
    if not alterno:
        return rutas
    hosts = {m.group(2) for k in CLAVES_NAS if (m := _HOST_RE.match(rutas.get(k, "")))}
    if not hosts or alterno in hosts:
        return rutas
    principal = next(iter(hosts))
    if _host_responde(principal) or not _host_responde(alterno):
        return rutas
    for k in CLAVES_NAS:
        v = rutas.get(k, "")
        m = _HOST_RE.match(v)
        if m and m.group(2) in hosts:
            rutas[k] = m.group(1) + alterno + v[m.end():]
    print(f"AVISO: el NAS {principal} no responde; uso NAS_ALTERNO {alterno}", file=sys.stderr)
    return rutas


def ffprobe_bin(rutas: dict[str, str]) -> str | None:
    cand = rutas.get("FFPROBE")
    if cand and Path(cand).exists():
        return cand
    return shutil.which("ffprobe")


def ffmpeg_bin(rutas: dict[str, str]) -> str | None:
    """ffmpeg (no ffprobe): hace falta para extraer el audio con --generar."""
    cand = rutas.get("FFMPEG")
    if cand and Path(cand).exists():
        return cand
    ff = rutas.get("FFPROBE")
    if ff:
        vecino = Path(ff).with_name("ffmpeg" + Path(ff).suffix)
        if vecino.exists():
            return str(vecino)
    return shutil.which("ffmpeg")


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
# solo palabras que no existen (o casi no se usan) en espanol
PAL_IT = {"che", "non", "il", "sono", "ho", "cosa", "questo", "questa", "sei", "perche", "anche", "qui",
          "gli", "hai", "della", "sto", "andiamo", "grazie", "niente", "tutto", "voglio", "posso", "allora",
          "adesso", "bene", "nel", "molto", "fatto", "sta", "dobbiamo", "stai", "lui", "lei", "io", "ci"}
# escrituras no latinas: un sub en coreano trae algo de ingles (creditos, nombres)
# y por palabras saldria "ingles"; se mira primero el alfabeto.
ESCRITURAS = [("ko", r"[\uac00-\ud7af\u1100-\u11ff]"), ("ja", r"[\u3040-\u30ff]"), ("zh", r"[\u4e00-\u9fff]"),
              ("ru", r"[\u0400-\u04ff]"), ("ar", r"[\u0600-\u06ff]"), ("he", r"[\u0590-\u05ff]"),
              ("el", r"[\u0370-\u03ff]"), ("th", r"[\u0e00-\u0e7f]")]
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
    idioma: str = "?"          # es-419 / es / en / it / ko... / ?
    valido: bool = False
    motivo: str = ""
    puntaje: float = 0.0
    texto: str | None = None   # contenido corregido a escribir en destino (None = copiar el original)
    nota: str = ""             # que se le corrigio
    cabecera: str = ""         # primeros dialogos (para detectar subs de otra serie/pelicula)
    fuente: bool = False       # en ingles/italiano y cuadra con el video: sirve para traducir
    tiempos_mal: bool = False  # fallo por duracion/desfase (se puede intentar rescatar)


# marcas que dejan algunos generadores automaticos: "[cite: 2]", "[cite_start]"
RE_CITA = re.compile(r"[ \t]*\[cite(?:_start|_end)?(?::[^\]\n]*)?\]")


def leer_texto(p: Path) -> tuple[str, str]:
    data = p.read_bytes()
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        pass
    # Mucho texto fuera de ASCII = alfabeto no latino en codificacion vieja (coreano
    # EUC-KR, chino, ruso...). Leido como cp1252 sale basura que parece "ingles".
    if sum(b >= 0x80 for b in data) > len(data) * 0.2:
        for enc in ("cp949", "gb18030", "big5", "shift_jis", "cp1251", "cp1256"):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                continue
    for enc in ("cp1252", "latin-1"):
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
    if RE_CITA.search(texto):
        texto = RE_CITA.sub("", texto)
        s.texto, s.nota = texto, "se quitaron marcas [cite]"
    medir_sub(s, texto, p.suffix.lower())
    return s


def medir_sub(s: Sub, texto: str, ext: str) -> None:
    tiempos: list[tuple[float, float]] = []
    lineas_texto: list[str] = []
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
    s.cabecera = "\n".join(lineas_texto[:12])
    if not tiempos:
        s.motivo = "no tiene dialogos con tiempos validos (archivo vacio o corrupto)"
        return
    s.inicio = min(t[0] for t in tiempos)
    s.fin = max(t[1] for t in tiempos)
    s.idioma = detectar_idioma(" ".join(lineas_texto))


def detectar_idioma(texto: str) -> str:
    letras = sum(c.isalpha() for c in texto)
    for codigo, rx in ESCRITURAS:
        if letras and len(re.findall(rx, texto)) > letras * 0.3:
            return codigo
    palabras = re.findall(r"[a-z]+", sin_acentos(texto.lower()))
    if len(palabras) < 30:
        return "?"
    es = sum(w in PAL_ES for w in palabras)
    en = sum(w in PAL_EN for w in palabras)
    it = sum(w in PAL_IT for w in palabras)
    if es <= max(en, it) * 1.2:
        if en > es and en >= it:
            return "en"
        if it > es and it > en:
            return "it"
        return "?"
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
    if re.search(r"(?<![a-z])(it|ita|italian|italiano)(?![a-z])", n):
        return "it"
    if re.search(r"(?<![a-z])(es|spa|spanish|espanol)(?![a-z])", n):
        return "es-419?"
    return None


# "Anteriormente en The Strain..." al inicio delata un sub de OTRA serie con el mismo SxxEyy
RE_RESUMEN = re.compile(
    r"^[\W_]*(?:(?:anteriormente|previamente)\s+en|en\s+(?:capitulos|episodios)\s+anteriores\s+(?:de|en)|"
    r"previously\s+on)\s", re.I)
PAL_VACIAS = {"the", "a", "an", "of", "and", "el", "la", "los", "las", "un", "una", "de", "del", "y"}


def _raices(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", sin_acentos(s.lower())) if w not in PAL_VACIAS and len(w) >= 3}


def revisar_titulo(s: Sub, info: Info) -> None:
    """Rechaza el sub si su resumen inicial nombra otra serie/pelicula."""
    if s.motivo or not s.cabecera:
        return
    propias = _raices(info.titulo)
    if not propias:
        return
    for linea in s.cabecera.splitlines():
        m = RE_RESUMEN.search(sin_acentos(linea))
        if not m:
            continue
        nombrado = re.sub(r"[.…!:,]+\s*$", "", linea[m.end():]).strip(" -\"'")
        ajenas = _raices(nombrado)
        if ajenas and not ajenas & propias:
            s.motivo = f'es de otra serie/pelicula (dice "{linea.strip(" -")}")'
            return


def motivo_tiempos(s: Sub, duracion: float | None) -> str:
    """"" si el sub cuadra con la duracion del video; si no, el motivo.
    Marca s.tiempos_mal cuando el problema se puede intentar rescatar."""
    if not duracion:
        return ""
    s.tiempos_mal = True
    if s.fin > duracion + MARGEN_FINAL_SEG:
        return f"termina en {fmt(s.fin)} pero el video dura {fmt(duracion)} (otra version/corte o FPS distinto)"
    if s.fin < duracion * MIN_COBERTURA:
        return f"termina en {fmt(s.fin)} y el video dura {fmt(duracion)} (incompleto o de otra version)"
    if s.inicio > duracion * 0.25:
        return f"el primer dialogo aparece hasta {fmt(s.inicio)} (desfase)"
    s.tiempos_mal = False
    minimo = max(20, int(MIN_DIALOGOS_POR_HORA * duracion / 3600 * 0.5))
    if s.dialogos < minimo:
        return f"solo {s.dialogos} dialogos para {fmt(duracion)} de video (incompleto o solo forzados)"
    return ""


def validar_contra_video(s: Sub, duracion: float | None) -> None:
    if s.motivo:
        return
    if s.idioma in IDIOMAS_FUENTE:
        tiempos = motivo_tiempos(s, duracion)
        s.motivo = f"el subtitulo esta en {NOMBRE_IDIOMA[s.idioma]}"
        if tiempos:
            s.motivo += f" y {tiempos}"
        else:
            s.fuente = True
            s.motivo += " (cuadra con el video: sirve para traducir)"
        return
    if s.idioma == "?":
        s.motivo = "no se pudo confirmar que este en espanol"
        return
    if s.idioma not in ("es", "es-419"):
        s.motivo = f"el subtitulo esta en {NOMBRE_IDIOMA.get(s.idioma, s.idioma)}"
        return
    s.motivo = motivo_tiempos(s, duracion)
    if s.motivo:
        return
    s.valido = True
    s.puntaje = (3 if s.idioma == "es-419" else 2) + min(s.dialogos, 3000) / 10000
    if s.ruta and s.ruta.suffix.lower() == ".srt":
        s.puntaje += 0.5


def fmt(seg: float) -> str:
    seg = int(seg)
    return f"{seg // 3600}:{seg % 3600 // 60:02d}:{seg % 60:02d}"


# --------------------------------------------------------------------------- rescate

RE_TIEMPO = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*$")


def a_seg(t: str) -> float | None:
    m = RE_TIEMPO.match(t)
    if not m:
        return None
    h, mi, se, ms = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se + ms / 10 ** len(m.group(4))


def fmt_srt(seg: float) -> str:
    ms = round(seg * 1000)
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def reacomodar_srt(texto: str, duracion: float) -> tuple[str, int] | None:
    """Reparte las lineas con horas imposibles (malformadas, que retroceden o pasan
    del final del video) entre el dialogo bueno anterior y el siguiente.
    Los tiempos resultantes son aproximados. None si no hay nada que corregir
    o si hay demasiado roto (seria otra version del video)."""
    bloques: list[list] = []   # [inicio, fin, lineas_de_texto]
    for b in re.split(r"\n\s*\n", texto.replace("\r\n", "\n").strip()):
        lineas = b.split("\n")
        i = next((k for k, l in enumerate(lineas) if "-->" in l), None)
        if i is None:
            if bloques:
                bloques[-1][2].extend(lineas)
            continue
        a, _, z = lineas[i].partition("-->")
        bloques.append([a_seg(a), a_seg(z), lineas[i + 1:]])
    if not bloques:
        return None

    rotos, ultimo_fin = [], 0.0
    for k, (ini, fin, _) in enumerate(bloques):
        if ini is None or fin is None or fin < ini or ini < ultimo_fin - 1 or fin > duracion:
            rotos.append(k)
        else:
            ultimo_fin = fin
    if not rotos or len(rotos) > max(3, len(bloques) * MAX_LINEAS_ROTAS):
        return None

    rotos_set = set(rotos)
    k = 0
    while k < len(bloques):
        if k not in rotos_set:
            k += 1
            continue
        j = k
        while j + 1 < len(bloques) and j + 1 in rotos_set:
            j += 1
        desde = bloques[k - 1][1] + 0.2 if k > 0 else 0.0
        hasta = bloques[j + 1][0] - 0.2 if j + 1 < len(bloques) else duracion - 0.5
        n = j - k + 1
        if hasta - desde < n:      # no caben ni a 1 s por linea
            return None
        pesos = [max(len(" ".join(bloques[x][2])), 10) for x in range(k, j + 1)]
        total, t = sum(pesos), desde
        for x, w in zip(range(k, j + 1), pesos):
            ranura = (hasta - desde) * w / total
            bloques[x][0], bloques[x][1] = t, t + min(ranura - 0.1, 6.0)
            t += ranura
        k = j + 1

    salida = [f"{n}\n{fmt_srt(ini)} --> {fmt_srt(fin)}\n" + "\n".join(lin)
              for n, (ini, fin, lin) in enumerate(bloques, 1)]
    return "\n\n".join(salida) + "\n", len(rotos)


def sincronizar(video: Path, sub: Path) -> str | None:
    """Alinea el subtitulo con el audio del video usando ffsubsync (con cache)."""
    try:
        import ffsubsync  # noqa: F401
    except ImportError:
        return None
    st = sub.stat()
    clave = hashlib.sha1(f"{video}|{sub}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    salida = CACHE_SYNC / f"{clave}.srt"
    if not salida.exists():
        CACHE_SYNC.mkdir(parents=True, exist_ok=True)
        print(f"Sincronizando con ffsubsync: {sub.name} ...", file=sys.stderr)
        try:
            subprocess.run([sys.executable, "-m", "ffsubsync.ffsubsync", str(video), "-i", str(sub), "-o", str(salida)],
                           capture_output=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not salida.exists():
            return None
    return leer_texto(salida)[0]


def ffmpeg_lee(s: Sub, ff: str | None) -> None:
    """Jellyfin convierte los .srt con ffmpeg; si ffmpeg no lee todos los dialogos,
    el sub saldria en el menu pero vacio o cortado."""
    if not s.valido or not s.ruta or s.ruta.suffix.lower() != ".srt":
        return
    ffmpeg = shutil.which("ffmpeg") or (str(Path(ff).with_name("ffmpeg.exe")) if ff else None)
    if not ffmpeg or not Path(ffmpeg).exists() and not shutil.which(ffmpeg):
        return
    texto = s.texto if s.texto is not None else leer_texto(s.ruta)[0]
    with tempfile.NamedTemporaryFile("w", suffix=".srt", encoding="utf-8", delete=False) as tmp:
        tmp.write(texto)
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-i", tmp.name, "-f", "webvtt", "-"],
                           capture_output=True, timeout=60)
        leidos = r.stdout.count(b"-->")
    except (OSError, subprocess.TimeoutExpired):
        return
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if leidos < s.dialogos * 0.98:
        s.valido = False
        s.motivo = f"ffmpeg (lo que usa Jellyfin) solo lee {leidos} de {s.dialogos} dialogos (formato roto)"


def rescatar(s: Sub, video: Path, duracion: float | None) -> None:
    """Si un sub en espanol fallo por tiempos, intenta reacomodarlo o sincronizarlo."""
    if s.valido or s.fuente or not duracion or not s.ruta or s.ruta.suffix.lower() != ".srt":
        return
    if s.idioma not in ("es", "es-419", *IDIOMAS_FUENTE) or not s.tiempos_mal:
        return
    base = s.texto if s.texto is not None else leer_texto(s.ruta)[0]
    nota_previa = s.nota + "; " if s.nota else ""

    def intentos():   # de mas barato a mas caro; ffsubsync solo corre si lo anterior no basto
        r = reacomodar_srt(base, duracion)
        if r:
            yield r[0], f"{r[1]} lineas con horas rotas reacomodadas (tiempos aproximados, revisar)"
        sinc = sincronizar(video, s.ruta)
        if sinc is not None:
            yield RE_CITA.sub("", sinc), "sincronizado con el audio (ffsubsync)"

    fallidos = []
    for texto, nota in intentos():
        prueba = Sub(ruta=s.ruta, codificacion=s.codificacion)
        medir_sub(prueba, texto, ".srt")
        if s.idioma in IDIOMAS_FUENTE:
            prueba.idioma = s.idioma
        validar_contra_video(prueba, duracion)
        if prueba.valido or prueba.fuente:
            s.texto, s.nota = texto, nota_previa + nota
            s.dialogos, s.inicio, s.fin, s.idioma = prueba.dialogos, prueba.inicio, prueba.fin, prueba.idioma
            s.valido, s.fuente, s.tiempos_mal = prueba.valido, prueba.fuente, False
            s.puntaje, s.motivo = prueba.puntaje - 0.3, prueba.motivo   # prefiere uno que no requirio arreglo
            return
        fallidos.append(nota.split(" (")[0])
    if fallidos:
        s.motivo += " | rescate fallido: " + ", ".join(fallidos)


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
        if sn.startswith(vn) or norm(sp.parent.name) == vn:   # "Subs\<video>\2_English.srt" (RARBG)
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
    original: Sub | None = None        # fuente en ingles/italiano del sub traducido (se guarda tambien)
    destino_original: Path | None = None


def marcar_origen(s: Sub) -> None:
    """Subs hechos por nosotros: siempre por debajo de cualquier sub humano en espanol.
    Orden: humano latino > humano castellano > traducido de ingles > de italiano > Whisper."""
    if not s.valido or not s.ruta:
        return
    nombre = s.ruta.name.lower()
    extra = min(s.dialogos, 3000) / 10000
    if MARCA_TRADUCIDO in nombre:
        from traducir_subs import fuente_de
        src = fuente_de(s.ruta)
        idioma = (analizar_sub(src).idioma if src else None) or "?"
        idioma = idioma if idioma in IDIOMAS_FUENTE else (idioma_por_nombre(src.name if src else nombre) or "en")
        nota = f"traducido del {NOMBRE_IDIOMA.get(idioma, idioma)} (revisar: traduccion automatica)"
        s.nota = f"{s.nota}; {nota}" if s.nota else nota
        s.puntaje = (1.0 if idioma == "en" else 0.9) + extra
    elif MARCA_GENERADO in nombre:
        s.nota = f"{s.nota}; generado con Whisper" if s.nota else "generado con Whisper"
        s.puntaje = 0.3 + extra


def evaluar_candidatos(video: Path, info: Info, rutas_subs: list[Path], dur: float | None,
                       internos: list[Sub], ff: str | None) -> tuple[Sub | None, list[Sub]]:
    """Analiza y valida los subs de un video; devuelve (elegido, descartados)."""
    evaluados = []
    for sp in rutas_subs:
        s = analizar_sub(sp)
        rel = sp.stem[len(video.stem):] if norm(sp.stem).startswith(norm(video.stem)) else sp.stem
        if MARCA_TRADUCIDO not in sp.name.lower():
            pista_nombre = idioma_por_nombre(rel) or idioma_por_nombre(sp.parent.name if sp.parent != video.parent else "")
            if pista_nombre == "en" and s.idioma not in ("es", "es-419"):
                s.idioma = "en"
            elif pista_nombre == "it" and s.idioma == "?":
                s.idioma = "it"
        if info:
            revisar_titulo(s, info)
        validar_contra_video(s, dur)
        if s.idioma not in IDIOMAS_FUENTE:
            rescatar(s, video, dur)
        ffmpeg_lee(s, ff)
        marcar_origen(s)
        evaluados.append(s)
    evaluados += internos
    validos = [s for s in evaluados if s.valido]
    # las fuentes en ingles/italiano solo se rescatan (ffsubsync tarda) si no hay nada mejor
    if not validos and not any(s.fuente for s in evaluados):
        for s in evaluados:
            if s.idioma in IDIOMAS_FUENTE:
                rescatar(s, video, dur)
    validos = sorted(validos, key=lambda s: -s.puntaje)
    elegido = validos[0] if validos else None
    return elegido, [s for s in evaluados if s is not elegido]


def original_de(elegido: Sub | None, descartados: list[Sub]) -> Sub | None:
    """Si el elegido es una traduccion, el sub original (para guardarlo como .eng/.ita)."""
    if not elegido or not elegido.ruta or MARCA_TRADUCIDO not in elegido.ruta.name.lower():
        return None
    from traducir_subs import fuente_de
    src = fuente_de(elegido.ruta)
    return next((s for s in descartados if s.ruta == src), None)


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
        if ext in SUB_EXT and carpeta.name.lower() in CARPETAS_SUBS:
            carpeta = carpeta.parent
        elif ext in SUB_EXT and carpeta.parent.name.lower() in CARPETAS_SUBS:   # Subs\<video>\x.srt
            carpeta = carpeta.parent.parent
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
            r.elegido, r.descartados = evaluar_candidatos(v, info, subs_candidatos(v, info, subs, videos),
                                                          dur, internos, ff)
            r.original = original_de(r.elegido, r.descartados)
            resultados.append(r)

    series = titulo_serie_comun(resultados)
    for r in resultados:
        if not r.elegido:
            continue
        rel, base = ruta_destino(r.info, r.video.suffix, series.get(norm(r.info.titulo)))
        r.destino_video = destino_raiz / rel / f"{base}{r.video.suffix.lower()}"
        if r.elegido.ruta:
            r.destino_sub = destino_raiz / rel / f"{base}.{SUFIJO_IDIOMA}{r.elegido.ruta.suffix.lower()}"
        if r.original:
            r.destino_original = destino_raiz / rel / f"{base}.{SUFIJO_FUENTE[r.original.idioma]}{r.original.ruta.suffix.lower()}"

    if aplicar:
        for r in resultados:
            if r.destino_video:
                mover(r)
    return resultados


def mover(r: Resultado) -> None:
    r.destino_video.parent.mkdir(parents=True, exist_ok=True)
    for d in (r.destino_video, r.destino_sub, r.destino_original):
        if d and d.exists():
            raise FileExistsError(f"Ya existe {d}; no se sobrescribe.")
    shutil.move(str(r.video), str(r.destino_video))
    if r.destino_sub and r.elegido and r.elegido.ruta:
        mover_sub(r.elegido, r.destino_sub)
    if r.destino_original and r.original:
        mover_sub(r.original, r.destino_original)


def mover_sub(s: Sub, destino: Path) -> None:
    texto, enc = leer_texto(s.ruta)
    if s.texto is not None:  # corregido (marcas, tiempos o sincronia)
        destino.write_text(s.texto, encoding="utf-8")
        s.ruta.unlink()
    elif enc in ("utf-8-sig", "utf-8"):
        shutil.move(str(s.ruta), str(destino))
    else:  # Jellyfin necesita UTF-8 para mostrar bien acentos y enes
        destino.write_text(texto, encoding="utf-8")
        s.ruta.unlink()


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
            if e.nota:
                etiqueta = ("generado" if "generado con Whisper" in e.nota else
                            "traducido" if "traducido del" in e.nota else "corregido")
                L.append(f"     {etiqueta}: {e.nota}")
            L.append(f"     ->  {r.destino_video.relative_to(entrada)}")
            if r.destino_sub:
                L.append(f"     ->  {r.destino_sub.relative_to(entrada)}")
            if r.destino_original:
                L.append(f"     ->  {r.destino_original.relative_to(entrada)}  (original)")
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

def descargar(resultados: list[Resultado], rutas: dict[str, str], idioma: str = "es") -> None:
    """Baja varios subtitulos (espanol: latino primero; o "en"/"it" como fuente para
    traducir) para los videos sin sub valido. Se guardan todos junto al video y la
    validacion posterior elige el que cuadre."""
    try:
        from babelfish import Language
        from subliminal import compute_score, download_subtitles, list_subtitles, region, scan_video
    except ImportError:
        print("subliminal no esta instalado: py -m pip install subliminal  (descarga omitida)")
        return
    if not region.is_configured:   # varios proveedores fallan si no hay cache
        region.configure("dogpile.cache.memory")
    logging.getLogger("subliminal").setLevel(logging.CRITICAL)   # un proveedor caido no es error nuestro
    proveedores, configs = list(PROVEEDORES), {}
    usuario, clave = rutas.get("OPENSUBTITLES_USUARIO"), rutas.get("OPENSUBTITLES_CLAVE")
    if usuario and clave:
        proveedores.insert(0, "opensubtitlescom")
        configs["opensubtitlescom"] = {"username": usuario, "password": clave}
    else:
        print("AVISO: sin OPENSUBTITLES_USUARIO/CLAVE en RUTAS.txt; se omite OpenSubtitles.com")
    idiomas = {"es": {Language("spa", "MX"), Language("spa")}, "en": {Language("eng")},
               "it": {Language("ita")}}[idioma]
    maximo = DESCARGAS_POR_VIDEO if idioma == "es" else DESCARGAS_FUENTE

    for r in resultados:
        if r.elegido:
            continue
        if idioma != "es" and any(s.fuente for s in r.descartados):
            continue   # ya hay uno en ingles/italiano que sirve
        try:
            vid = scan_video(str(r.video))
            encontrados = list_subtitles({vid}, idiomas, providers=proveedores, provider_configs=configs).get(vid, [])
            if not encontrados:
                print(f"Sin resultados en linea: {r.video.name}")
                continue
            # latino primero, luego por puntaje de coincidencia con el video
            encontrados.sort(key=lambda s: (s.language.country is not None, compute_score(s, vid)), reverse=True)
            elegidos = encontrados[:maximo]
            download_subtitles(elegidos, providers=proveedores, provider_configs=configs)
            existentes = {hashlib.sha1(p.read_bytes()).digest() for p in r.video.parent.iterdir()
                          if p.suffix.lower() in SUB_EXT}
            guardados = fallidos = 0
            for n, s in enumerate(elegidos, 1):
                if not s.content:
                    fallidos += 1   # el proveedor fallo al bajarlo (red, limite diario, etc.)
                    continue
                if hashlib.sha1(s.content).digest() in existentes:
                    continue   # identico a uno que ya esta
                ext = "." + (getattr(s, "subtitle_format", None) or "srt")
                tag = ("es-MX" if s.language.country else "es") if idioma == "es" else idioma
                destino = r.video.with_name(f"{r.video.stem}.{s.provider_name}{n}.{tag}{ext}")
                if destino.exists():
                    continue
                destino.write_bytes(s.content)
                existentes.add(hashlib.sha1(s.content).digest())
                guardados += 1
            aviso = f" ({fallidos} fallaron al bajar; reintenta mas tarde)" if fallidos else ""
            print(f"Descargados {guardados} de {len(encontrados)} encontrados{aviso}: {r.video.name}")
        except Exception as e:  # noqa: BLE001 - un fallo de red no debe detener el lote
            print(f"Error descargando {r.video.name}: {e}")


# --------------------------------------------------------------------------- traducir opcional

FORMATOS_TEXTO = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def extraer_internos(video: Path, ff: str | None, rutas: dict[str, str]) -> int:
    """Saca a .srt las pistas internas de texto en ingles/italiano (no forzadas) para
    poder traducirlas: "<video>.interno<N>.<en|it>.srt". Devuelve cuantas extrajo."""
    ffmpeg = ffmpeg_bin(rutas)
    if not ff or not ffmpeg:
        return 0
    try:
        r = subprocess.run([ff, "-v", "error", "-select_streams", "s", "-print_format", "json", "-show_streams",
                            str(video)], capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return 0
    n = 0
    for st in streams:
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        lang = {"eng": "en", "en": "en", "ita": "it", "it": "it"}.get((tags.get("language") or "").lower())
        titulo = sin_acentos((tags.get("title") or "").lower())
        forzado = (st.get("disposition") or {}).get("forced") or "forced" in titulo
        if not lang or forzado or st.get("codec_name") not in FORMATOS_TEXTO:
            continue
        destino = video.with_name(f"{video.stem}.interno{st.get('index')}.{lang}.srt")
        if destino.exists():
            continue
        try:
            subprocess.run([ffmpeg, "-v", "error", "-n", "-i", str(video), "-map", f"0:{st.get('index')}",
                            "-c:s", "srt", str(destino)], capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        n += destino.exists()
    return n


def traducir(resultados: list[Resultado]) -> int:
    """Para cada video sin sub en espanol: traduce el mejor sub en ingles (o, si no hay,
    italiano) que cuadre con el video. Escribe "<original>.traducido.es.srt" junto al
    original; la pasada siguiente lo valida como cualquier otro. Devuelve cuantos tradujo."""
    import traducir_subs
    hechos = 0
    for r in resultados:
        if r.elegido:
            continue
        fuentes = [s for s in r.descartados if s.fuente and s.ruta]
        fuentes.sort(key=lambda s: (IDIOMAS_FUENTE.index(s.idioma), s.texto is not None, -s.dialogos))
        for s in fuentes:
            destino = traducir_subs.nombre_traducido(s.ruta)
            if destino.exists():
                continue   # ya se tradujo antes y no sirvio (si sirviera, el video ya tendria sub)
            falta = traducir_subs.disponible(s.idioma)
            if falta:
                print(f"No se puede traducir {r.video.name}: {falta}")
                break
            print(f"Traduciendo del {NOMBRE_IDIOMA[s.idioma]}: {s.ruta.name} ...", flush=True)
            texto = s.texto if s.texto is not None else leer_texto(s.ruta)[0]
            try:
                salida, n = traducir_subs.traducir_srt(texto, s.idioma)
            except Exception as e:  # noqa: BLE001 - un sub roto no detiene el lote
                print(f"     error traduciendo: {e}")
                continue
            destino.write_text(salida, encoding="utf-8")
            print(f"     OK: {destino.name} ({n} dialogos)")
            hechos += 1
            break
    return hechos


def paso_traducir(procesar_fn, rutas: dict[str, str], ff: str | None, bajar: bool) -> None:
    """Cadena completa para los que no tienen sub en espanol:
    pistas internas -> fuentes ya presentes -> (bajar ingles) -> (bajar italiano) -> traducir."""
    res = procesar_fn()
    pendientes = [r for r in res if not r.elegido]
    if not pendientes:
        return
    if sum(extraer_internos(r.video, ff, rutas) for r in pendientes):
        res = procesar_fn()
    if bajar:
        for idioma in IDIOMAS_FUENTE:
            faltan = [r for r in res if not r.elegido and not any(s.fuente for s in r.descartados)]
            if not faltan:
                break
            print(f"Buscando subtitulos en {NOMBRE_IDIOMA[idioma]} para {len(faltan)} video(s)...")
            descargar(faltan, rutas, idioma)
            res = procesar_fn()
    traducir(res)


# --------------------------------------------------------------------------- generar opcional

def generar(resultados: list[Resultado], rutas: dict[str, str], ff: str | None) -> None:
    """Ultimo recurso: transcribe el audio con Whisper para los videos que siguen
    sin subtitulo valido. El .srt queda junto al video y la pasada siguiente lo valida."""
    try:
        import whisper_subs
    except ImportError as e:
        print(f"no se pudo cargar whisper_subs.py: {e}")
        return
    falta = whisper_subs.disponible()
    if falta:
        print(f"{falta}  (generacion omitida)")
        return
    ffmpeg = ffmpeg_bin(rutas)
    if not ffmpeg:
        print("ffmpeg no encontrado; no se puede extraer el audio (generacion omitida)")
        return
    modelo = rutas.get("WHISPER_MODELO") or whisper_subs.MODELO_DEF
    pendientes = [r for r in resultados if not r.elegido]
    for n, r in enumerate(pendientes, 1):
        print(f"[{n}/{len(pendientes)}] generando subtitulo: {r.video.name}")
        ruta, nota = whisper_subs.generar(r.video, ff, ffmpeg, modelo)
        print(f"     {'OK: ' + ruta.name + ' - ' if ruta else ''}{nota}")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Revisa y organiza subtitulos para Jellyfin")
    ap.add_argument("--carpeta", help="Carpeta a revisar (por defecto CARPETA_ENTRADA de RUTAS.txt)")
    ap.add_argument("--aplicar", action="store_true", help="Mover/renombrar de verdad (sin esto solo simula)")
    ap.add_argument("--descargar", action="store_true", help="Intentar descargar subs faltantes y re-evaluar")
    ap.add_argument("--traducir", action="store_true",
                    help="Sin sub en espanol: traducir uno en ingles/italiano que cuadre (con --descargar, tambien los baja)")
    ap.add_argument("--generar", action="store_true",
                    help="Generar con Whisper (GPU) los que sigan sin subtitulo y re-evaluar")
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
        descargar(procesar(entrada, False, ff), rutas)
    if a.traducir:
        paso_traducir(lambda: procesar(entrada, False, ff), rutas, ff, a.descargar)
    if a.generar:
        generar(procesar(entrada, False, ff), rutas, ff)
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
            "nota": r.elegido.nota if r.elegido else None,
            "destino_original": str(r.destino_original) if r.destino_original else None,
            "descartados": [{"sub": str(s.ruta) if s.ruta else s.pista, "motivo": s.motivo} for s in r.descartados],
        } for r in resultados], ensure_ascii=False, indent=2))
    else:
        print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
