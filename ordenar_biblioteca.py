#!/usr/bin/env python3
"""Ordena las peliculas que YA estan en la biblioteca de Jellyfin.

Para cada pelicula sin subtitulo (ni audio) en espanol segun Jellyfin:
  1. Busca el titulo correcto en TMDB (via Jellyfin), eligiendo por anio exacto y
     titulo; asi se corrigen las mal identificadas (ej. "Wild 2014" -> no "Into the Wild").
  2. Busca subtitulos en espanol en la carpeta y subcarpetas (".srt" sin codigo de
     idioma, "Subs\\spa.srt", "*-spanish-yify\\...") y los valida contra el video
     con las mismas reglas de jellyfin_subs.py (duracion, dialogos, rescate, ffmpeg).
  3. Renombra EN SU LUGAR, con la convencion de Jellyfin:
        Titulo (Anio) [tmdbid-N]\\Titulo (Anio).mkv
        Titulo (Anio) [tmdbid-N]\\Titulo (Anio).spa.srt   (UTF-8)
        Titulo (Anio) [tmdbid-N]\\Titulo (Anio).eng.srt   (si habia uno en ingles)
     "[tmdbid-N]" en la carpeta hace que Jellyfin identifique la pelicula sin adivinar.

Uso:
  py ordenar_biblioteca.py                 # simulacion (no toca nada)
  py ordenar_biblioteca.py --descargar     # baja subs para las que no tengan (no renombra)
  py ordenar_biblioteca.py --aplicar       # renombra y pide a Jellyfin reescanear
  py ordenar_biblioteca.py --buscar "Wild" # solo las que coincidan

Requiere en RUTAS.txt: JELLYFIN_URL, JELLYFIN_API_KEY y BIBLIOTECA_PELICULAS
(la carpeta de red que Jellyfin ve como su biblioteca de peliculas).
Carpetas con varios videos (sagas, "movies\\") se listan para revisar a mano.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from jellyfin_subs import (PROVEEDORES, SUB_EXT, VIDEO_EXT, Resultado, Sub, analizar_nombre, analizar_sub, descargar,
                           ffmpeg_lee, ffprobe_bin, idioma_por_nombre, leer_rutas, leer_texto, nombre_seguro,
                           norm, probar_video, rescatar, validar_contra_video)
from verificar_jellyfin import Jellyfin, sub_espanol

# Codigos/nombres de otros idiomas en nombres de subtitulos ("Subs\\por.srt", "Brazilian.por.srt").
# Portugues, catalan, gallego e italiano se parecen demasiado al espanol para fiarse solo del texto.
OTROS_IDIOMAS = {
    "por", "pt", "brazilian", "portuguese", "cat", "ca", "catalan", "glg", "gl", "galician", "ita", "it",
    "italian", "fre", "fra", "fr", "french", "ger", "deu", "de", "german", "dut", "nld", "nl", "pol", "rum",
    "ron", "swe", "dan", "nob", "nor", "fin", "tur", "hun", "hrv", "cze", "ces", "slo", "slv", "srp", "bul",
    "vie", "ind", "may", "msa", "fil", "baq", "eus", "ara", "heb", "gre", "ell", "rus", "ukr", "jpn", "kor",
    "chi", "zho", "tha", "eng", "english", "dutch", "polish", "swedish",
}

# Ediciones que estorban al buscar en TMDB
RE_EDICION = re.compile(r"(?i)\b(extended and unrated|directors?|cut|extended|unrated|uncut|remastered|theatrical|edition)\b")

# Nombre que les pone jellyfin_subs.descargar(): "<video>.<proveedor><n>.es[-MX].srt"
RE_DESCARGADO = re.compile(r"\.(?:" + "|".join(["opensubtitlescom"] + PROVEEDORES) + r")\d+\.es(?:-MX)?\.\w+$")


def ya_ordenada(ruta_jf: str) -> bool:
    """Ya sigue la convencion: Titulo (Anio) [tmdbid-N]/Titulo (Anio).ext"""
    carpeta, archivo = ruta_jf.rsplit("/", 2)[-2:]
    m = re.fullmatch(r"(.+) \[tmdbid-\d+\]", carpeta)
    return bool(m) and archivo.rsplit(".", 1)[0] == m.group(1)


@dataclass
class Plan:
    item: dict
    video: Path
    titulo: str = ""              # "Titulo (Anio)"
    tmdb: str | None = None
    elegido: Sub | None = None
    descartados: list[Sub] = field(default_factory=list)
    ingles: Path | None = None    # .srt en ingles a conservar como .eng
    problema: str = ""            # si no se puede automatizar

    @property
    def carpeta_nueva(self) -> Path:
        sufijo = f" [tmdbid-{self.tmdb}]" if self.tmdb else ""
        return self.video.parent.parent / f"{self.titulo}{sufijo}"


def post_json(jf: Jellyfin, ruta: str, datos: dict):
    req = urllib.request.Request(f"{jf.url}{ruta}", data=json.dumps(datos).encode(), method="POST",
                                 headers={"X-Emby-Token": jf.clave, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"null")


def buscar_tmdb(jf: Jellyfin, titulo: str, anio: str | None,
                tmdb_actual: str | None = None) -> tuple[str, str, str] | None:
    """(titulo en ingles, anio, tmdb id) del resultado que mejor coincide.
    Prioridad: titulo exacto > titulo contenido; luego el que Jellyfin ya tenia (hay
    varias "Enemy (2013)" en TMDB y el anio puede variar en 1); luego anio exacto."""
    info = {"Name": titulo, "MetadataLanguage": "en"}
    if anio:
        info["Year"] = int(anio)
    res = post_json(jf, "/Items/RemoteSearch/Movie", {"SearchInfo": info, "SearchProviderName": "TheMovieDb"}) or []
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


def es_otro_idioma(p: Path, video: Path) -> bool:
    stem = p.stem.lower()
    if stem.startswith(video.stem.lower()):   # "Cinema.Paradiso.ITALIAN...srt": el idioma es del audio
        stem = stem[len(video.stem):]
    tokens = set(re.split(r"[^a-z]+", stem))
    return bool(tokens & OTROS_IDIOMAS) and not (tokens & {"spa", "es", "spanish", "esp", "lat", "latino"})


def evaluar(plan: Plan, ff: str | None) -> None:
    v = plan.video
    dur, internos = probar_video(ff, v)
    evaluados = []
    for sp in sorted(p for p in v.parent.rglob("*") if p.is_file() and p.suffix.lower() in SUB_EXT):
        if es_otro_idioma(sp, v):
            continue
        s = analizar_sub(sp)
        if idioma_por_nombre(sp.stem) == "en" and s.idioma not in ("es", "es-419"):
            s.idioma = "en"
        if s.idioma == "en" and sp.parent == v.parent and norm(sp.stem).startswith(norm(v.stem)):
            plan.ingles = sp
        validar_contra_video(s, dur)
        rescatar(s, v, dur)
        ffmpeg_lee(s, ff)
        if s.valido and re.search(r"(?i)(?<![a-z])(sdh|hi|cc)(?![a-z])", sp.stem):
            s.puntaje -= 0.2   # prefiere el que no trae descripciones de sonidos
        evaluados.append(s)
    evaluados += internos
    validos = sorted((s for s in evaluados if s.valido), key=lambda s: -s.puntaje)
    plan.elegido = validos[0] if validos else None
    plan.descartados = [s for s in evaluados if s is not plan.elegido]
    if plan.elegido and plan.elegido.ruta == plan.ingles:
        plan.ingles = None


def planear(jf: Jellyfin, item: dict, base_jf: str, base: Path, ff: str | None) -> Plan:
    rel = item["Path"][len(base_jf):].lstrip("/")
    video = base.joinpath(*rel.split("/"))
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
        res = post_json(jf, "/Items/RemoteSearch/Movie", {"SearchInfo": {"ProviderIds": {"Tmdb": tmdb_actual},
                        "MetadataLanguage": "en"}, "SearchProviderName": "TheMovieDb"}) or []
        if res and res[0].get("Name"):
            encontrado = res[0]["Name"], str(res[0].get("ProductionYear") or item["ProductionYear"]), tmdb_actual
    if encontrado:
        nombre, anio, plan.tmdb = encontrado
        if info.anio and abs(int(anio) - int(info.anio)) <= 1:   # TMDB a veces da el estreno local
            anio = info.anio
        plan.titulo = nombre_seguro(f"{nombre} ({anio})")
    else:   # sin TMDB no se adivina el nombre
        plan.problema = f"no se encontro en TMDB (buscado: {titulo} {info.anio or ''})"
        return plan
    evaluar(plan, ff)
    return plan


def aplicar(plan: Plan) -> None:
    v, base = plan.video, plan.titulo
    nueva = plan.carpeta_nueva
    destinos = [v.with_name(f"{base}{v.suffix.lower()}")]
    if plan.elegido.ruta:
        destinos.append(v.with_name(f"{base}.spa.srt"))
    if plan.ingles:
        destinos.append(v.with_name(f"{base}.eng{plan.ingles.suffix.lower()}"))
    e = plan.elegido
    if e.ruta and len(destinos) > 1 and destinos[1].exists() and e.ruta != destinos[1] \
            and destinos[1].read_bytes() == e.ruta.read_bytes():
        e.ruta.unlink()   # copia identica ("X.spa.0.srt" junto a "X.spa.srt"): se queda la de buen nombre
        e.ruta = destinos[1]
    for d in destinos:
        if d.exists() and d != v and d != e.ruta:
            raise FileExistsError(f"Ya existe {d}; no se sobrescribe.")
    if nueva.exists() and nueva != v.parent:
        raise FileExistsError(f"Ya existe la carpeta {nueva}; no se sobrescribe.")

    if e.ruta:
        texto = e.texto if e.texto is not None else leer_texto(e.ruta)[0]
        destinos[1].write_text(texto, encoding="utf-8")   # Jellyfin necesita UTF-8
        if e.ruta != destinos[1]:
            e.ruta.unlink()
    if plan.ingles:
        plan.ingles.rename(destinos[-1])
    for p in v.parent.iterdir():   # candidatos de --descargar que no se eligieron
        if p.is_file() and p.suffix.lower() in SUB_EXT and RE_DESCARGADO.search(p.name):
            p.unlink()
    v.rename(destinos[0])
    if nueva != v.parent:
        v.parent.rename(nueva)


def main() -> int:
    ap = argparse.ArgumentParser(description="Renombra peliculas de la biblioteca de Jellyfin y sus subtitulos")
    ap.add_argument("--aplicar", action="store_true", help="Renombrar de verdad (sin esto solo simula)")
    ap.add_argument("--descargar", action="store_true", help="Bajar subs en espanol para las que no tengan")
    ap.add_argument("--todas", action="store_true",
                    help="Incluir tambien las que Jellyfin ya ve con sub en espanol (si no estan ordenadas)")
    ap.add_argument("--buscar", help="Solo peliculas cuyo nombre o ruta contenga este texto")
    a = ap.parse_args()

    rutas = leer_rutas()
    url, clave, base = rutas.get("JELLYFIN_URL"), rutas.get("JELLYFIN_API_KEY"), rutas.get("BIBLIOTECA_PELICULAS")
    if not (url and clave and base):
        print("Faltan JELLYFIN_URL, JELLYFIN_API_KEY y/o BIBLIOTECA_PELICULAS en RUTAS.txt")
        return 2
    base = Path(base)
    if not base.is_dir():
        print(f"No se puede abrir {base} (conecta la unidad de red)")
        return 2
    jf = Jellyfin(url, clave)
    ff = ffprobe_bin(rutas)

    libs = [l for l in jf.pedir("/Library/VirtualFolders") if l.get("CollectionType") == "movies"]
    if len(libs) != 1 or len(libs[0]["Locations"]) != 1:
        print("Se esperaba una sola biblioteca de peliculas con una sola carpeta en Jellyfin")
        return 2
    base_jf = libs[0]["Locations"][0].rstrip("/")
    items = jf.pedir("/Items", Recursive="true", IncludeItemTypes="Movie", Fields="MediaStreams,Path,ProviderIds",
                     ParentId=libs[0]["ItemId"], SortBy="SortName", EnableImages="false")["Items"]
    items = [i for i in items if (i.get("Path") or "").startswith(base_jf + "/")
             and (not sub_espanol(i) or a.todas and not ya_ordenada(i["Path"]))]
    if a.buscar:
        b = norm(a.buscar)
        items = [i for i in items if b in norm(i.get("Name", "")) or b in norm(i.get("Path", ""))]

    planes = []
    for i in items:
        print(f"Revisando {i.get('Name')}...", file=sys.stderr, flush=True)
        planes.append(planear(jf, i, base_jf, base, ff))

    if a.descargar:
        faltan = [Resultado(p.video, analizar_nombre(p.video.stem), None) for p in planes
                  if not p.problema and not p.elegido]
        descargar(faltan, rutas)
        for p in planes:
            if not p.problema and not p.elegido:
                evaluar(p, ff)

    listos = [p for p in planes if not p.problema and p.elegido]
    print(f"\nPeliculas a revisar: {len(planes)} | se pueden ordenar: {len(listos)}\n")
    for p in planes:
        if p.problema:
            continue
        marca = "[OK]" if p.elegido else "[--]"
        print(f"{marca} {p.item.get('Name')} ({p.item.get('ProductionYear')})  ->  {p.carpeta_nueva.name}"
             )
        print(f"     antes: {p.video.relative_to(base)}")
        e = p.elegido
        if e:
            origen = f"interno pista {e.pista}" if e.interno else str(e.ruta.relative_to(p.video.parent))
            print(f"     sub: {origen} ({e.idioma})" + (f" - {e.nota}" if e.nota else ""))
            if p.ingles:
                print(f"     ingles: {p.ingles.name} -> .eng")
        else:
            for s in p.descartados:
                if s.idioma != "en":
                    print(f"     descartado {s.ruta.name if s.ruta else s.pista}: {s.motivo}")
            print("     sin subtitulo en espanol valido")
    manuales = [p for p in planes if p.problema]
    if manuales:
        print("\n== REVISAR A MANO ==")
        for p in manuales:
            print(f"[??] {p.item.get('Name')}: {p.problema}\n     {p.item.get('Path')}")

    if a.aplicar and listos:
        print()
        for p in listos:
            try:
                aplicar(p)
                print(f"Renombrada: {p.carpeta_nueva.name}")
            except OSError as e:
                print(f"ERROR en {p.item.get('Name')}: {e}")
        jf.pedir("/Library/Refresh", "POST")
        print("Jellyfin esta reescaneando; en unos minutos corre: py verificar_jellyfin.py")
    elif not a.aplicar:
        print("\nSIMULACION: no se movio nada. Usa --aplicar para renombrar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
