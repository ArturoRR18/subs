#!/usr/bin/env python3
"""Pregunta a Jellyfin si cada pelicula/episodio tiene subtitulo en espanol.

Si Jellyfin lo lista, ya lo leyo (nombre, permisos y formato correctos), asi que
aparecera en el menu de subtitulos sin tener que probar uno por uno.

Uso:
  py verificar_jellyfin.py                         # revisa toda la biblioteca, muestra solo faltantes
  py verificar_jellyfin.py --buscar "Seven Havens" # filtra por nombre o ruta (muestra todos)
  py verificar_jellyfin.py --escanear              # primero reescanea la biblioteca y espera

Requiere en RUTAS.txt: JELLYFIN_URL y JELLYFIN_API_KEY
(Jellyfin > Panel de control > Claves de API).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from jellyfin_subs import leer_rutas, norm

IDIOMAS_ES = {"spa", "es", "es-419", "es-mx", "esl"}


class Jellyfin:
    def __init__(self, url: str, clave: str):
        self.url, self.clave = url.rstrip("/"), clave

    def pedir(self, ruta: str, metodo: str = "GET", **q):
        req = urllib.request.Request(f"{self.url}{ruta}?{urllib.parse.urlencode(q)}", method=metodo,
                                     headers={"X-Emby-Token": self.clave})
        with urllib.request.urlopen(req, timeout=60) as resp:
            cuerpo = resp.read()
        return json.loads(cuerpo) if cuerpo else None


def escanear(jf: Jellyfin) -> None:
    jf.pedir("/Library/Refresh", "POST")
    print("Escaneando biblioteca...", end="", flush=True)
    time.sleep(5)
    while any(t.get("Key") == "RefreshLibrary" and t.get("State") != "Idle" for t in jf.pedir("/ScheduledTasks")):
        print(".", end="", flush=True)
        time.sleep(5)
    print(" listo")


def sub_espanol(item: dict) -> str | None:
    """Devuelve la descripcion del subtitulo en espanol (no forzado) o None.
    Un video con audio en espanol no necesita subtitulo y tambien cuenta."""
    streams = item.get("MediaStreams") or []
    for m in streams:
        if m.get("Type") != "Subtitle" or m.get("IsForced"):
            continue
        if (m.get("Language") or "").lower() in IDIOMAS_ES:
            return ("sub externo" if m.get("IsExternal") else "sub interno") + f" ({m.get('Codec', '?')})"
    if any(m.get("Type") == "Audio" and (m.get("Language") or "").lower() in IDIOMAS_ES for m in streams):
        return "audio en espanol"
    return None


def nombre(item: dict) -> str:
    if item.get("Type") == "Episode":
        return f"{item.get('SeriesName', '?')} S{item.get('ParentIndexNumber') or 0:02d}E{item.get('IndexNumber') or 0:02d}"
    anio = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    return f"{item.get('Name', '?')}{anio}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Verifica en Jellyfin que los videos tengan subtitulo en espanol")
    ap.add_argument("--buscar", help="Texto a buscar en el nombre o la ruta (muestra tambien los OK)")
    ap.add_argument("--escanear", action="store_true", help="Reescanear la biblioteca antes de revisar")
    a = ap.parse_args()

    rutas = leer_rutas()
    url, clave = rutas.get("JELLYFIN_URL"), rutas.get("JELLYFIN_API_KEY")
    if not url or not clave:
        print("Faltan JELLYFIN_URL y/o JELLYFIN_API_KEY en RUTAS.txt")
        return 2
    jf = Jellyfin(url, clave)
    try:
        if a.escanear:
            escanear(jf)
        items = jf.pedir("/Items", Recursive="true", IncludeItemTypes="Movie,Episode",
                         Fields="MediaStreams,Path", SortBy="SeriesSortName,SortName",
                         EnableImages="false", EnableUserData="false")["Items"]
    except urllib.error.HTTPError as e:
        print(f"Jellyfin respondio {e.code}: {'clave de API invalida' if e.code == 401 else e.reason}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"No se pudo conectar a {url}: {e}")
        return 1

    if a.buscar:
        b = norm(a.buscar)
        items = [i for i in items if b in norm(nombre(i)) or b in norm(i.get("Path") or "")]
    faltan = [i for i in items if not sub_espanol(i)]

    print(f"Revisados: {len(items)} | con subtitulo o audio en espanol: {len(items) - len(faltan)} | sin: {len(faltan)}")
    for i in items:
        s = sub_espanol(i)
        if s and not a.buscar:
            continue
        print(f"[{'OK' if s else '--'}] {nombre(i)}  {s or ''}")
        if not s:
            print(f"     {i.get('Path', '')}")
    return 1 if faltan else 0


if __name__ == "__main__":
    sys.exit(main())
