#!/usr/bin/env python3
"""Ordena las peliculas y series que YA estan en la biblioteca de Jellyfin.

Para cada pelicula/episodio sin subtitulo (ni audio) en espanol segun Jellyfin:
  1. Peliculas: busca el titulo correcto en TMDB (via Jellyfin), eligiendo por anio
     exacto y titulo; asi se corrigen las mal identificadas (ej. "Wild 2014" -> no
     "Into the Wild"). Series: usa la carpeta de la serie si ya trae [tmdbid-N].
  2. Busca subtitulos en espanol en la carpeta y subcarpetas (".srt" sin codigo de
     idioma, "Subs\\spa.srt", "Subs\\<episodio>\\2_English.srt", "*-spanish-yify\\...")
     y los valida contra el video con las mismas reglas de jellyfin_subs.py.
  3. Si no hay ninguno en espanol: con --traducir traduce uno en ingles (o italiano)
     que cuadre; con --generar, como ultimo recurso, lo genera con Whisper.
  4. Renombra EN SU LUGAR, con la convencion de Jellyfin:
        Titulo (Anio) [tmdbid-N]\\Titulo (Anio).mkv  + .spa.srt (+ .eng/.ita.srt)
        Serie (Anio) [tmdbid-N]\\Season 01\\Serie S01E02.mkv  + .spa.srt (+ .eng/.ita.srt)
     "[tmdbid-N]" en la carpeta hace que Jellyfin identifique sin adivinar.
     La carpeta de una serie solo se renombra con --todas (afecta a todos sus episodios).

Uso:
  py ordenar_biblioteca.py                          # simulacion (no toca nada)
  py ordenar_biblioteca.py --descargar --traducir   # baja subs/traduce (no renombra)
  py ordenar_biblioteca.py --aplicar                # renombra y pide a Jellyfin reescanear
  py ordenar_biblioteca.py --buscar "Wild"          # solo las que coincidan
  py ordenar_biblioteca.py --solo peliculas|series

Requiere en RUTAS.txt: JELLYFIN_URL, JELLYFIN_API_KEY, BIBLIOTECA_PELICULAS y
BIBLIOTECA_SERIES (las carpetas de red que Jellyfin ve como sus bibliotecas).
Carpetas de peliculas con varios videos (sagas, "movies\\") se listan para revisar a mano.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from jellyfin_subs import (CARPETAS_SUBS, MARCA_TRADUCIDO, PROVEEDORES, SUB_EXT, SUFIJO_FUENTE, VIDEO_EXT, Info,
                           Resultado, Sub, analizar_nombre, descargar, evaluar_candidatos, ffmpeg_bin, ffprobe_bin,
                           idioma_por_nombre, leer_rutas, leer_texto, mover_sub, nombre_seguro, norm, original_de,
                           paso_traducir, probar_video, subs_candidatos)
from verificar_jellyfin import Jellyfin, sub_espanol

# Codigos/nombres de otros idiomas en nombres de subtitulos ("Subs\\por.srt", "Brazilian.por.srt").
# Portugues, catalan y gallego se parecen demasiado al espanol para fiarse solo del texto.
# Ingles e italiano NO estan: sirven como fuente para traducir.
OTROS_IDIOMAS = {
    "por", "pt", "brazilian", "portuguese", "cat", "ca", "catalan", "glg", "gl", "galician",
    "fre", "fra", "fr", "french", "ger", "deu", "de", "german", "dut", "nld", "nl", "pol", "rum",
    "ron", "swe", "dan", "nob", "nor", "fin", "tur", "hun", "hrv", "cze", "ces", "slo", "slv", "srp", "bul",
    "vie", "ind", "may", "msa", "fil", "baq", "eus", "ara", "heb", "gre", "ell", "rus", "ukr", "jpn", "kor",
    "chi", "zho", "tha", "dutch", "polish", "swedish",
}

# Ediciones que estorban al buscar en TMDB
RE_EDICION = re.compile(r"(?i)\b(extended and unrated|directors?|cut|extended|unrated|uncut|remastered|theatrical|edition)\b")

# Archivos que dejan --descargar / --traducir y que sobran despues de elegir:
# "<video>.<proveedor><n>.<es|es-MX|en|it>.srt", "<video>.interno<N>.en.srt", "*.traducido.es.srt"
RE_CANDIDATO = re.compile(r"(\.(?:" + "|".join(["opensubtitlescom"] + PROVEEDORES) + r")\d+\.(?:es(?:-MX)?|en|it)"
                          r"|\.interno\d+\.(?:en|it))(?:" + re.escape(MARCA_TRADUCIDO) + r"\.es)?\.\w+$"
                          r"|" + re.escape(MARCA_TRADUCIDO) + r"\.es\.srt$", re.I)
RE_TMDB = re.compile(r"(.+) \[tmdbid-(\d+)\]")


def ya_ordenada(ruta_jf: str) -> bool:
    """Ya sigue la convencion: Titulo (Anio) [tmdbid-N]/Titulo (Anio).ext"""
    carpeta, archivo = ruta_jf.rsplit("/", 2)[-2:]
    m = RE_TMDB.fullmatch(carpeta)
    return bool(m) and archivo.rsplit(".", 1)[0] == m.group(1)


def ep_codigo(item: dict) -> str | None:
    t, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
    if t is None or e is None:
        return None
    cod = f"S{t:02d}E{e:02d}"
    if item.get("IndexNumberEnd") and item["IndexNumberEnd"] != e:
        cod += f"-E{item['IndexNumberEnd']:02d}"
    return cod


def ya_ordenado_ep(ruta_jf: str, item: dict) -> bool:
    """Serie/Season 01/<Titulo> S01E02.ext"""
    partes = ruta_jf.split("/")
    cod = ep_codigo(item)
    return (cod is not None and len(partes) >= 3 and partes[-2] == f"Season {item['ParentIndexNumber']:02d}"
            and partes[-1].rsplit(".", 1)[0].endswith(f" {cod}"))


@dataclass
class Plan:
    item: dict
    video: Path
    tipo: str = "pelicula"        # pelicula / episodio
    titulo: str = ""              # pelicula: "Titulo (Anio)"; episodio: "Serie S01E02"
    tmdb: str | None = None
    carpeta_destino: Path | None = None   # carpeta donde queda el video (antes de renombrar la de la serie)
    elegido: Sub | None = None
    descartados: list[Sub] = field(default_factory=list)
    original: Sub | None = None   # fuente en ingles/italiano del traducido
    ingles: Path | None = None    # .srt en ingles junto al video, a conservar como .eng
    problema: str = ""            # si no se puede automatizar

    @property
    def carpeta_nueva(self) -> Path:
        if self.tipo == "episodio":
            return self.carpeta_destino
        sufijo = f" [tmdbid-{self.tmdb}]" if self.tmdb else ""
        return self.video.parent.parent / f"{self.titulo}{sufijo}"

    @property
    def nombre(self) -> str:
        if self.tipo == "episodio":
            return self.titulo or f"{self.item.get('SeriesName')} {ep_codigo(self.item) or '?'}"
        return f"{self.item.get('Name')} ({self.item.get('ProductionYear')})"


def post_json(jf: Jellyfin, ruta: str, datos: dict):
    req = urllib.request.Request(f"{jf.url}{ruta}", data=json.dumps(datos).encode(), method="POST",
                                 headers={"X-Emby-Token": jf.clave, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"null")


def buscar_tmdb(jf: Jellyfin, titulo: str, anio: str | None, tmdb_actual: str | None = None,
                tipo: str = "Movie") -> tuple[str, str, str] | None:
    """(titulo en ingles, anio, tmdb id) del resultado que mejor coincide.
    Prioridad: titulo exacto > titulo contenido; luego el que Jellyfin ya tenia (hay
    varias "Enemy (2013)" en TMDB y el anio puede variar en 1); luego anio exacto."""
    info = {"Name": titulo, "MetadataLanguage": "en"}
    if anio:
        info["Year"] = int(anio)
    res = post_json(jf, f"/Items/RemoteSearch/{tipo}", {"SearchInfo": info, "SearchProviderName": "TheMovieDb"}) or []
    t = norm(titulo)
    if anio:
        res = [x for x in res if x.get("ProductionYear") and abs(x["ProductionYear"] - int(anio)) <= 1]

    def puntaje(x):
        n, y = norm(x.get("Name", "")), x.get("ProductionYear")
        if n == t:
            titulo_ok = 3
        elif min(len(n), len(t)) >= 5 and (n in t or t in n):   # "Avatar Aang The Last Airbender"
            titulo_ok = 1
        else:
            return None   # titulo distinto: no adivinar aunque el anio cuadre
        return (titulo_ok, x["ProviderIds"]["Tmdb"] == tmdb_actual, anio is not None and str(y) == anio)

    res = [x for x in res if (x.get("ProviderIds") or {}).get("Tmdb") and x.get("ProductionYear") and puntaje(x)]
    if not res:
        return None
    mejor = max(res, key=puntaje)   # max() se queda con el primero ante empate (el mas relevante en TMDB)
    return mejor["Name"], str(mejor["ProductionYear"]), mejor["ProviderIds"]["Tmdb"]


def tmdb_por_id(jf: Jellyfin, tmdb: str, tipo: str = "Movie") -> tuple[str, str | None] | None:
    res = post_json(jf, f"/Items/RemoteSearch/{tipo}", {"SearchInfo": {"ProviderIds": {"Tmdb": tmdb},
                    "MetadataLanguage": "en"}, "SearchProviderName": "TheMovieDb"}) or []
    if res and res[0].get("Name"):
        return res[0]["Name"], str(res[0]["ProductionYear"]) if res[0].get("ProductionYear") else None
    return None


def es_otro_idioma(p: Path, video: Path) -> bool:
    stem = p.stem.lower()
    if stem.startswith(video.stem.lower()):   # "Cinema.Paradiso.ITALIAN...srt": el idioma es del audio
        stem = stem[len(video.stem):]
    tokens = set(re.split(r"[^a-z]+", stem))
    return bool(tokens & OTROS_IDIOMAS) and not (tokens & {"spa", "es", "spanish", "esp", "lat", "latino"})


def subs_de(plan: Plan) -> list[Path]:
    v = plan.video
    if plan.tipo == "pelicula":   # la carpeta es solo de esta pelicula
        todos = sorted(p for p in v.parent.rglob("*") if p.is_file() and p.suffix.lower() in SUB_EXT)
    else:                         # carpeta compartida con otros episodios: solo los que le tocan
        cerca = [p for p in v.parent.iterdir() if p.is_file() and p.suffix.lower() in SUB_EXT]
        for d in v.parent.iterdir():
            if d.is_dir() and d.name.lower() in CARPETAS_SUBS:
                cerca += [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in SUB_EXT]
        videos = [p for p in v.parent.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
        info = analizar_nombre(v.stem)
        if not info.es_serie:
            info = Info(info.titulo, None, plan.item.get("ParentIndexNumber"), plan.item.get("IndexNumber"))
        todos = sorted(subs_candidatos(v, info, sorted(cerca), videos))
    return [p for p in todos if not es_otro_idioma(p, v)]


def evaluar(plan: Plan, ff: str | None) -> None:
    v = plan.video
    dur, internos = probar_video(ff, v)
    rutas_subs = subs_de(plan)
    plan.ingles = next((sp for sp in rutas_subs if sp.parent == v.parent and norm(sp.stem).startswith(norm(v.stem))
                        and idioma_por_nombre(sp.stem[len(v.stem):]) == "en"
                        and MARCA_TRADUCIDO not in sp.name.lower()), None)
    elegido, descartados = evaluar_candidatos(v, analizar_nombre(v.stem), rutas_subs, dur, internos, ff)
    todos = ([elegido] if elegido else []) + descartados
    for s in todos:
        if s.valido and s.ruta and re.search(r"(?i)(?<![a-z])(sdh|hi|cc)(?![a-z])", s.ruta.stem):
            s.puntaje -= 0.2   # prefiere el que no trae descripciones de sonidos
    validos = sorted((s for s in todos if s.valido), key=lambda s: -s.puntaje)
    plan.elegido = validos[0] if validos else None
    plan.descartados = [s for s in todos if s is not plan.elegido]
    plan.original = original_de(plan.elegido, plan.descartados)
    if plan.original or (plan.elegido and plan.elegido.ruta == plan.ingles):
        plan.ingles = None


def ruta_local(ruta_jf: str, base_jf: str, base: Path) -> Path:
    return base.joinpath(*ruta_jf[len(base_jf):].lstrip("/").split("/"))


# --------------------------------------------------------------------------- peliculas

def planear(jf: Jellyfin, item: dict, base_jf: str, base: Path, ff: str | None) -> Plan:
    video = ruta_local(item["Path"], base_jf, base)
    plan = Plan(item, video)
    if video.parent == base:
        plan.problema = "el video esta suelto en la raiz de la biblioteca"
    elif video.parent.parent != base:
        plan.problema = "el video esta en una subcarpeta de otra carpeta"
    elif not video.exists():
        plan.problema = "no se encontro el archivo en la red"
    else:
        hermanos = [p for p in video.parent.iterdir()
                    if p.is_file() and p.suffix.lower() in VIDEO_EXT and "sample" not in p.stem.lower()]
        anidados = [p for p in video.parent.rglob("*") if p.parent != video.parent
                    and p.suffix.lower() in VIDEO_EXT and "sample" not in p.stem.lower()]
        if len(hermanos) > 1:
            plan.problema = f"la carpeta tiene {len(hermanos)} videos (saga/partes)"
        elif anidados:
            plan.problema = f"la carpeta tiene otros {len(anidados)} videos en subcarpetas"
    if plan.problema:
        return plan

    # titulo: del nombre de la carpeta (suele ser el mas limpio), si no del archivo
    # (si la carpeta no trae anio, igual se prefiere: "The ABCs Of Death 1\excess-720p.avi")
    info = analizar_nombre(video.parent.name)
    if not info.anio and analizar_nombre(video.stem).anio:
        info = analizar_nombre(video.stem)
    titulo = info.titulo
    if info.anio:   # "Interstellar (2014) (2014)"
        titulo = re.sub(rf"\b{info.anio}\b", " ", titulo)
    titulo = re.sub(r"\s+", " ", RE_EDICION.sub(" ", titulo)).strip() or info.titulo
    anio = info.anio or (str(item["ProductionYear"]) if item.get("ProductionYear") else None)
    tmdb_actual = (item.get("ProviderIds") or {}).get("Tmdb")
    encontrado = buscar_tmdb(jf, titulo, anio, tmdb_actual)
    # si no (ej. titulo en otro idioma: "Cidade de Deus"), confia en lo que Jellyfin ya
    # identifico, siempre que el anio cuadre con el del nombre del archivo
    if not encontrado and tmdb_actual and info.anio and item.get("ProductionYear") \
            and abs(item["ProductionYear"] - int(info.anio)) <= 1:
        r = tmdb_por_id(jf, tmdb_actual)
        if r:
            encontrado = r[0], r[1] or str(item["ProductionYear"]), tmdb_actual
    if encontrado:
        nombre, anio, plan.tmdb = encontrado
        if info.anio and abs(int(anio) - int(info.anio)) <= 1:   # TMDB a veces da el estreno local
            anio = info.anio
        plan.titulo = nombre_seguro(f"{nombre} ({anio})")
    else:   # sin TMDB no se adivina el nombre
        plan.problema = f"no se encontro en TMDB (buscado: {titulo} {info.anio or ''})"
        return plan
    plan.carpeta_destino = video.parent
    evaluar(plan, ff)
    return plan


# --------------------------------------------------------------------------- series

@dataclass
class Serie:
    item: dict
    carpeta: Path                 # carpeta actual en la red
    nombre_nuevo: str = ""        # "Serie (Anio) [tmdbid-N]"
    titulo_ep: str = ""           # "Serie" (como empiezan los nombres de episodio)
    problema: str = ""


def planear_serie(jf: Jellyfin, item: dict, base_jf: str, base: Path, renombrar: bool) -> Serie:
    carpeta = ruta_local(item.get("Path") or "", base_jf, base)
    s = Serie(item, carpeta)
    if not item.get("Path") or carpeta.parent != base:
        s.problema = "la carpeta de la serie no esta directo en la biblioteca"
        return s
    if not carpeta.is_dir():
        s.problema = "no se encontro la carpeta de la serie en la red"
        return s
    m = RE_TMDB.fullmatch(carpeta.name)
    tmdb_actual = (item.get("ProviderIds") or {}).get("Tmdb")
    if m and (not tmdb_actual or m.group(2) == tmdb_actual):   # ya ordenada: no se toca la carpeta
        s.nombre_nuevo = carpeta.name
    elif not renombrar:   # renombrar la carpeta afecta a todos los episodios: solo con --todas
        s.nombre_nuevo = carpeta.name
    else:
        info = analizar_nombre(carpeta.name)
        anio = info.anio or (str(item["ProductionYear"]) if item.get("ProductionYear") else None)
        encontrado = buscar_tmdb(jf, info.titulo, anio, tmdb_actual, "Series")
        if not encontrado and tmdb_actual:
            r = tmdb_por_id(jf, tmdb_actual, "Series")
            if r and (r[1] or anio):
                encontrado = r[0], r[1] or anio, tmdb_actual
        if not encontrado:
            s.problema = f"no se encontro la serie en TMDB (buscado: {info.titulo} {anio or ''})"
            return s
        nombre, anio, tmdb = encontrado
        s.nombre_nuevo = nombre_seguro(f"{nombre} ({anio})") + f" [tmdbid-{tmdb}]"
    base_nombre = RE_TMDB.fullmatch(s.nombre_nuevo).group(1) if RE_TMDB.fullmatch(s.nombre_nuevo) else s.nombre_nuevo
    s.titulo_ep = nombre_seguro(re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", base_nombre)) or base_nombre
    return s


def planear_ep(item: dict, serie: Serie, base_jf: str, base: Path, ff: str | None) -> Plan:
    video = ruta_local(item["Path"], base_jf, base)
    plan = Plan(item, video, tipo="episodio")
    cod = ep_codigo(item)
    if serie.problema:
        plan.problema = serie.problema
    elif cod is None:
        plan.problema = "Jellyfin no sabe la temporada/episodio"
    elif serie.carpeta not in video.parents:
        plan.problema = "el episodio no esta dentro de la carpeta de la serie"
    elif not video.exists():
        plan.problema = "no se encontro el archivo en la red"
    if plan.problema:
        return plan
    plan.titulo = f"{serie.titulo_ep} {cod}"
    plan.carpeta_destino = serie.carpeta / f"Season {item['ParentIndexNumber']:02d}"
    evaluar(plan, ff)
    return plan


# --------------------------------------------------------------------------- aplicar

def destinos(plan: Plan) -> tuple[Path, Path | None, Path | None]:
    """(video, sub en espanol, original/ingles) antes de renombrar carpetas."""
    carpeta = plan.video.parent if plan.tipo == "pelicula" else plan.carpeta_destino
    base = plan.titulo
    dv = carpeta / f"{base}{plan.video.suffix.lower()}"
    ds = carpeta / f"{base}.spa.srt" if plan.elegido and plan.elegido.ruta else None
    do = None
    if plan.original:
        do = carpeta / f"{base}.{SUFIJO_FUENTE[plan.original.idioma]}{plan.original.ruta.suffix.lower()}"
    elif plan.ingles:
        do = carpeta / f"{base}.eng{plan.ingles.suffix.lower()}"
    return dv, ds, do


def limpiar_candidatos(plan: Plan) -> None:
    v = plan.video
    if plan.tipo == "pelicula":
        sobrantes = [p for p in v.parent.rglob("*") if p.is_file()]
    else:
        sobrantes = [p for p in v.parent.iterdir() if p.is_file() and p.name.startswith(v.stem)]
        for d in v.parent.iterdir():
            if d.is_dir() and d.name.lower() in CARPETAS_SUBS:
                sobrantes += [p for p in d.rglob("*") if p.is_file() and norm(p.parent.name) == norm(v.stem)]
    for p in sobrantes:
        if p.suffix.lower() in SUB_EXT and RE_CANDIDATO.search(p.name):
            p.unlink()


def aplicar(plan: Plan) -> None:
    v, e = plan.video, plan.elegido
    dv, ds, do = destinos(plan)
    if e.ruta and ds and ds.exists() and e.ruta != ds and ds.read_bytes() == e.ruta.read_bytes():
        e.ruta.unlink()   # copia identica ("X.spa.0.srt" junto a "X.spa.srt"): se queda la de buen nombre
        e.ruta = ds
    for d in (dv, ds, do):
        if d and d.exists() and d != v and d != e.ruta:
            raise FileExistsError(f"Ya existe {d}; no se sobrescribe.")
    nueva = plan.carpeta_nueva
    if plan.tipo == "pelicula" and nueva.exists() and nueva != v.parent:
        raise FileExistsError(f"Ya existe la carpeta {nueva}; no se sobrescribe.")

    dv.parent.mkdir(parents=True, exist_ok=True)
    if e.ruta and ds:
        texto = e.texto if e.texto is not None else leer_texto(e.ruta)[0]
        ds.write_text(texto, encoding="utf-8")   # Jellyfin necesita UTF-8
        if e.ruta != ds:
            e.ruta.unlink()
    if do and plan.original:
        mover_sub(plan.original, do)
    elif do and plan.ingles:
        plan.ingles.rename(do)
    limpiar_candidatos(plan)
    v.rename(dv)
    if plan.tipo == "pelicula" and nueva != v.parent:
        v.parent.rename(nueva)


# --------------------------------------------------------------------------- pasos comunes

def completar(planes: list[Plan], rutas: dict[str, str], ff: str | None, a) -> None:
    """--descargar / --traducir / --generar para los que siguen sin sub en espanol."""
    pendientes = [p for p in planes if not p.problema and not p.elegido]
    if not pendientes:
        return

    def como_resultados() -> list[Resultado]:
        out = []
        for p in pendientes:
            evaluar(p, ff)
            r = Resultado(p.video, analizar_nombre(p.video.stem), None)
            r.elegido, r.descartados = p.elegido, p.descartados
            out.append(r)
        return out

    if a.descargar:
        descargar([Resultado(p.video, analizar_nombre(p.video.stem), None) for p in pendientes], rutas)
        como_resultados()
    if a.traducir:
        paso_traducir(como_resultados, rutas, ff, a.descargar)
        como_resultados()
    if a.generar:
        import whisper_subs
        falta = whisper_subs.disponible()
        ffmpeg = ffmpeg_bin(rutas)
        if falta or not ffmpeg:
            print(f"{falta or 'ffmpeg no encontrado'}  (generacion omitida)")
            return
        modelo = rutas.get("WHISPER_MODELO") or whisper_subs.MODELO_DEF
        faltan = [p for p in pendientes if not p.elegido]
        for n, p in enumerate(faltan, 1):
            print(f"[{n}/{len(faltan)}] generando subtitulo: {p.video.name}")
            ruta, nota = whisper_subs.generar(p.video, ff, ffmpeg, modelo)
            print(f"     {'OK: ' + ruta.name + ' - ' if ruta else ''}{nota}")
            evaluar(p, ff)


def imprimir(planes: list[Plan], base: Path) -> None:
    for p in planes:
        if p.problema:
            continue
        marca = "[OK]" if p.elegido else "[--]"
        dv = destinos(p)[0]
        nuevo = p.carpeta_nueva.name if p.tipo == "pelicula" else str(dv.relative_to(base))
        print(f"{marca} {p.nombre}  ->  {nuevo}")
        print(f"     antes: {p.video.relative_to(base)}")
        e = p.elegido
        if e:
            origen = f"interno pista {e.pista}" if e.interno else str(e.ruta.relative_to(p.video.parent))
            print(f"     sub: {origen} ({e.idioma})" + (f" - {e.nota}" if e.nota else ""))
            if p.original:
                print(f"     original: {p.original.ruta.name} -> .{SUFIJO_FUENTE[p.original.idioma]}")
            elif p.ingles:
                print(f"     ingles: {p.ingles.name} -> .eng")
        else:
            for s in p.descartados:
                print(f"     descartado {s.ruta.name if s.ruta else s.pista}: {s.motivo}")
            print("     sin subtitulo en espanol valido")


# --------------------------------------------------------------------------- main

def con_espanol(item: dict) -> bool:
    """Como sub_espanol, pero sin contar los candidatos de --descargar/--traducir que
    aun no se eligieron (Jellyfin ya los ve como sub en espanol)."""
    streams = [m for m in item.get("MediaStreams") or []
               if not (m.get("Type") == "Subtitle" and m.get("IsExternal")
                       and RE_CANDIDATO.search((m.get("Path") or "").replace("\\", "/").rsplit("/", 1)[-1]))]
    return sub_espanol({**item, "MediaStreams": streams}) is not None


def biblioteca(jf: Jellyfin, tipo: str) -> dict | None:
    libs = [l for l in jf.pedir("/Library/VirtualFolders") if l.get("CollectionType") == tipo]
    if len(libs) != 1 or len(libs[0]["Locations"]) != 1:
        return None
    return libs[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Renombra peliculas y series de la biblioteca de Jellyfin y sus subtitulos")
    ap.add_argument("--aplicar", action="store_true", help="Renombrar de verdad (sin esto solo simula)")
    ap.add_argument("--descargar", action="store_true", help="Bajar subs para las que no tengan")
    ap.add_argument("--traducir", action="store_true",
                    help="Sin sub en espanol: traducir uno en ingles/italiano (con --descargar tambien los baja)")
    ap.add_argument("--generar", action="store_true", help="Ultimo recurso: generar con Whisper (GPU)")
    ap.add_argument("--todas", action="store_true",
                    help="Incluir tambien las que ya tienen sub en espanol (si no estan ordenadas) "
                         "y renombrar carpetas de series sin [tmdbid]")
    ap.add_argument("--solo", choices=["peliculas", "series"], help="Revisar solo una biblioteca")
    ap.add_argument("--buscar", help="Solo las que contengan este texto en el nombre o la ruta")
    a = ap.parse_args()

    rutas = leer_rutas()
    url, clave = rutas.get("JELLYFIN_URL"), rutas.get("JELLYFIN_API_KEY")
    if not (url and clave):
        print("Faltan JELLYFIN_URL y/o JELLYFIN_API_KEY en RUTAS.txt")
        return 2
    jf = Jellyfin(url, clave)
    ff = ffprobe_bin(rutas)
    b = norm(a.buscar) if a.buscar else None

    def coincide(i: dict) -> bool:
        return not b or b in norm(i.get("Name", "")) or b in norm(i.get("SeriesName", "")) or b in norm(i.get("Path", ""))

    planes_pel: list[Plan] = []
    planes_ep: list[Plan] = []
    series: dict[str, Serie] = {}
    base_pel = base_ser = None

    if a.solo != "series":
        base_pel = Path(rutas.get("BIBLIOTECA_PELICULAS") or "")
        lib = biblioteca(jf, "movies")
        if not rutas.get("BIBLIOTECA_PELICULAS") or not base_pel.is_dir():
            print(f"Peliculas: no se puede abrir BIBLIOTECA_PELICULAS ({base_pel}); se omiten")
        elif not lib:
            print("Peliculas: se esperaba una sola biblioteca de peliculas con una sola carpeta en Jellyfin")
        else:
            base_jf = lib["Locations"][0].rstrip("/")
            items = jf.pedir("/Items", Recursive="true", IncludeItemTypes="Movie", Fields="MediaStreams,Path,ProviderIds",
                             ParentId=lib["ItemId"], SortBy="SortName", EnableImages="false")["Items"]
            items = [i for i in items if (i.get("Path") or "").startswith(base_jf + "/") and coincide(i)
                     and (not con_espanol(i) or a.todas and not ya_ordenada(i["Path"]))]
            for i in items:
                print(f"Revisando {i.get('Name')}...", file=sys.stderr, flush=True)
                planes_pel.append(planear(jf, i, base_jf, base_pel, ff))

    if a.solo != "peliculas":
        base_ser = Path(rutas.get("BIBLIOTECA_SERIES") or "")
        lib = biblioteca(jf, "tvshows")
        if not rutas.get("BIBLIOTECA_SERIES") or not base_ser.is_dir():
            print(f"Series: no se puede abrir BIBLIOTECA_SERIES ({base_ser}); se omiten")
        elif not lib:
            print("Series: se esperaba una sola biblioteca de series con una sola carpeta en Jellyfin")
        else:
            base_jf = lib["Locations"][0].rstrip("/")
            eps = jf.pedir("/Items", Recursive="true", IncludeItemTypes="Episode", ParentId=lib["ItemId"],
                           Fields="MediaStreams,Path,ProviderIds", SortBy="SeriesSortName,ParentIndexNumber,IndexNumber",
                           EnableImages="false")["Items"]
            eps = [i for i in eps if (i.get("Path") or "").startswith(base_jf + "/") and coincide(i)
                   and (not con_espanol(i) or a.todas and not ya_ordenado_ep(i["Path"], i))]
            for i in eps:
                sid = i.get("SeriesId")
                if sid not in series:
                    si = jf.pedir(f"/Items", Ids=sid, Fields="Path,ProviderIds", EnableImages="false")["Items"][0]
                    series[sid] = planear_serie(jf, si, base_jf, base_ser, a.todas)
                print(f"Revisando {i.get('SeriesName')} {ep_codigo(i) or ''}...", file=sys.stderr, flush=True)
                planes_ep.append(planear_ep(i, series[sid], base_jf, base_ser, ff))

    planes = planes_pel + planes_ep
    completar(planes, rutas, ff, a)

    listos = [p for p in planes if not p.problema and p.elegido]
    print(f"\nPeliculas a revisar: {len(planes_pel)} | episodios a revisar: {len(planes_ep)} | "
          f"se pueden ordenar: {len(listos)}\n")
    if planes_pel:
        print("== PELICULAS ==")
        imprimir(planes_pel, base_pel)
    if planes_ep:
        print("== SERIES ==")
        for s in series.values():
            if not s.problema and s.carpeta.name != s.nombre_nuevo:
                print(f"[>>] carpeta de serie: {s.carpeta.name}  ->  {s.nombre_nuevo}")
        imprimir(planes_ep, base_ser)
    manuales = [p for p in planes if p.problema]
    if manuales:
        print("\n== REVISAR A MANO ==")
        for p in manuales:
            print(f"[??] {p.nombre}: {p.problema}\n     {p.item.get('Path')}")

    if a.aplicar and listos:
        print()
        for p in listos:
            try:
                aplicar(p)
                print(f"Renombrado: {p.nombre}")
            except OSError as e:
                print(f"ERROR en {p.nombre}: {e}")
        for s in series.values():   # al final, para no romper las rutas de sus episodios
            if s.problema or s.carpeta.name == s.nombre_nuevo:
                continue
            nueva = s.carpeta.with_name(s.nombre_nuevo)
            try:
                if nueva.exists():
                    raise FileExistsError(f"Ya existe la carpeta {nueva}; no se sobrescribe.")
                s.carpeta.rename(nueva)
                print(f"Serie renombrada: {nueva.name}")
            except OSError as e:
                print(f"ERROR renombrando la serie {s.carpeta.name}: {e}")
        jf.pedir("/Library/Refresh", "POST")
        print("Jellyfin esta reescaneando; en unos minutos corre: py verificar_jellyfin.py")
    elif not a.aplicar:
        print("\nSIMULACION: no se movio nada. Usa --aplicar para renombrar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
