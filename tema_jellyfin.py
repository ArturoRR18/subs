"""Maneja el CSS personalizado de Jellyfin (Panel > General > Codigo CSS personalizado).

    py tema_jellyfin.py --leer                 # guarda el CSS del servidor en tema/actual.css
    py tema_jellyfin.py --fuentes [URL ...]    # baja el CSS de cada @import (o URL dada) a tema/fuentes/
    py tema_jellyfin.py --aplicar [archivo]    # respalda y sube tema/custom.css (o el archivo dado)
    py tema_jellyfin.py --restaurar [archivo]  # sube un respaldo (por defecto el mas reciente)
    py tema_jellyfin.py --js                   # sube tema/js/*.js al plugin JavaScript Injector

Usa JELLYFIN_URL + JELLYFIN_API_KEY de RUTAS.txt (la clave de API tiene permisos de admin).
Los respaldos van a tema/respaldos/ (gitignored) antes de cada cambio: nada se pierde.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from jellyfin_subs import leer_rutas
from verificar_jellyfin import Jellyfin

BASE = Path(__file__).resolve().parent / "tema"
RESPALDOS = BASE / "respaldos"
FUENTES = BASE / "fuentes"
RE_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*['"]?([^'")\s;]+)['"]?\s*\)?[^;]*;""")
RE_COMENTARIO = re.compile(r"/\*.*?\*/", re.S)
JS = BASE / "js"
PREFIJO_JS = "tema/"   # nombre de las entradas que maneja este script en JavaScript Injector


def subir_js(jf: Jellyfin) -> None:
    """Sincroniza tema/js/*.js con las entradas 'tema/<archivo>' de JavaScript Injector.
    No toca los scripts que no empiezan con 'tema/' (los que agregues a mano)."""
    plugin = next((p for p in jf.pedir("/Plugins") if p["Name"] == "JavaScript Injector"), None)
    if not plugin:
        sys.exit("Falta el plugin JavaScript Injector en Jellyfin")
    ruta = f"/Plugins/{plugin['Id']}/Configuration"
    cfg = jf.pedir(ruta)
    RESPALDOS.mkdir(parents=True, exist_ok=True)
    (RESPALDOS / f"js_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    otros = [e for e in cfg.get("CustomJavaScripts", []) if not e.get("Name", "").startswith(PREFIJO_JS)]
    nuevos = [{"Name": PREFIJO_JS + f.name, "Script": f.read_text(encoding="utf-8"), "Enabled": True,
               "RequiresAuthentication": False} for f in sorted(JS.glob("*.js"))]
    cfg["CustomJavaScripts"] = otros + nuevos
    req = urllib.request.Request(jf.url + ruta, method="POST", data=json.dumps(cfg).encode("utf-8"),
                                 headers={"X-Emby-Token": jf.clave, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).close()
    print(f"JavaScript Injector: {len(nuevos)} script(s) del tema ({', '.join(e['Name'] for e in nuevos)}). "
          "Recarga Jellyfin con Ctrl+F5.")


def conectar() -> Jellyfin:
    r = leer_rutas()
    if not r.get("JELLYFIN_URL") or not r.get("JELLYFIN_API_KEY"):
        sys.exit("Falta JELLYFIN_URL o JELLYFIN_API_KEY en RUTAS.txt")
    return Jellyfin(r["JELLYFIN_URL"], r["JELLYFIN_API_KEY"])


def branding(jf: Jellyfin) -> dict:
    return jf.pedir("/System/Configuration/branding")


def respaldar(css: str) -> Path:
    RESPALDOS.mkdir(parents=True, exist_ok=True)
    p = RESPALDOS / f"custom_{time.strftime('%Y%m%d_%H%M%S')}.css"
    p.write_text(css, encoding="utf-8")
    return p


def subir(jf: Jellyfin, css: str) -> None:
    cfg = branding(jf)
    print(f"Respaldo del CSS anterior: {respaldar(cfg.get('CustomCss') or '')}")
    cfg["CustomCss"] = css
    req = urllib.request.Request(f"{jf.url}/System/Configuration/branding", method="POST",
                                 data=json.dumps(cfg).encode("utf-8"),
                                 headers={"X-Emby-Token": jf.clave, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).close()
    if (branding(jf).get("CustomCss") or "") != css:
        sys.exit("ERROR: Jellyfin no guardo el CSS (revisa que la clave de API sea de admin)")
    print(f"Aplicado ({len(css)} caracteres). Recarga Jellyfin con Ctrl+F5 para verlo.")


def nombre_fuente(url: str) -> str:
    """Nombre de archivo legible: repo + ruta (ej. jellyfin-nebula__opciones__acento-blanco.css)."""
    partes = [p for p in urllib.parse.urlparse(url).path.split("/") if p and p not in ("gh", "npm", "dist")]
    partes = [re.sub(r"@.*$", "", p) for p in partes]
    if len(partes) > 1:
        partes = partes[1:]   # quita el usuario/organizacion
    nombre = "__".join(partes) or "fuente"
    return re.sub(r"[^\w.-]", "_", nombre if nombre.endswith(".css") else nombre + ".css")


def bajar(url: str, vistos: set[str]) -> None:
    """Baja un CSS y, recursivamente, los @import que tenga dentro."""
    if url in vistos:
        return
    vistos.add(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            css = resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - una fuente caida no detiene las demas
        print(f"  ERROR {url}: {e}")
        return
    FUENTES.mkdir(parents=True, exist_ok=True)
    destino = FUENTES / nombre_fuente(url)
    destino.write_text(f"/* Fuente: {url} */\n{css}", encoding="utf-8")
    print(f"  {destino.relative_to(BASE.parent)}  ({len(css)} caracteres)")
    for sub in RE_IMPORT.findall(RE_COMENTARIO.sub("", css)):
        bajar(urllib.parse.urljoin(url, sub), vistos)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--leer", action="store_true")
    g.add_argument("--fuentes", nargs="*", metavar="URL")
    g.add_argument("--aplicar", nargs="?", const=str(BASE / "custom.css"), metavar="ARCHIVO")
    g.add_argument("--restaurar", nargs="?", const="", metavar="ARCHIVO")
    g.add_argument("--js", action="store_true")
    a = ap.parse_args()

    if a.js:
        subir_js(conectar())
        return 0

    if a.fuentes is not None:
        urls = a.fuentes
        if not urls:
            css = RE_COMENTARIO.sub("", branding(conectar()).get("CustomCss") or "")
            urls = RE_IMPORT.findall(css)
            print(f"{len(urls)} @import en el CSS del servidor")
        vistos: set[str] = set()
        for u in urls:
            bajar(u, vistos)
        return 0

    jf = conectar()
    if a.leer:
        css = branding(jf).get("CustomCss") or ""
        BASE.mkdir(exist_ok=True)
        (BASE / "actual.css").write_text(css, encoding="utf-8")
        print(f"tema/actual.css ({len(css)} caracteres)")
    elif a.aplicar:
        subir(jf, Path(a.aplicar).read_text(encoding="utf-8"))
    else:
        archivo = Path(a.restaurar) if a.restaurar else max(RESPALDOS.glob("custom_*.css"), default=None)
        if not archivo:
            sys.exit("No hay respaldos en tema/respaldos/")
        print(f"Restaurando {archivo}")
        subir(jf, archivo.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
