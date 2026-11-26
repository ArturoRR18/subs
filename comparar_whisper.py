#!/usr/bin/env python3
"""Compara un subtitulo generado con Whisper contra uno humano del MISMO video,
para detectar patrones de error (no para decidir cual usar: eso ya lo hace
jellyfin_subs.py). Util para diagnosticar whisper_subs.py con casos reales
sin tener que ver el video.

Uso:
  py comparar_whisper.py "humano.spa.srt" "whisper.es.srt"
  py comparar_whisper.py --lote "carpeta"   # busca pares *.spa.srt / *.whisper.es.srt

Metodo: por cada linea humana busca la linea Whisper mas cercana en el tiempo
(por punto medio). Si no hay ninguna a menos de TOLERANCIA segundos, se marca
"sin cobertura" (candidato a dialogo que Whisper no agarro). Al reves, una
linea Whisper sin ninguna humana cerca se marca "huerfana" (candidato a
alucinacion o a dialogo que el humano omitio a proposito, ej. canciones).
No compara el texto palabra por palabra (son traducciones independientes),
solo tiempos y longitud como senal aproximada.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TOLERANCIA_SEG = 3.0   # diferencia de punto medio para considerar "misma linea"

RE_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass
class Linea:
    inicio: float
    fin: float
    texto: str

    @property
    def medio(self) -> float:
        return (self.inicio + self.fin) / 2


def leer_texto(p: Path) -> str:
    data = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_srt(p: Path) -> list[Linea]:
    texto = leer_texto(p)
    lineas = []
    pos = 0
    matches = list(RE_TS.finditer(texto))
    for i, m in enumerate(matches):
        g = [int(x) if x else 0 for x in m.groups()]
        inicio = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        fin = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        fin_bloque = matches[i + 1].start() if i + 1 < len(matches) else len(texto)
        cuerpo = texto[m.end():fin_bloque]
        # quita la siguiente linea de indice numerico (la del bloque que sigue)
        partes = [l.strip() for l in cuerpo.splitlines() if l.strip()]
        if partes and partes[-1].isdigit():
            partes = partes[:-1]
        contenido = " ".join(partes)
        # subs SDH: "(Phone ringing)", "[music]", "<i>...</i>", notas musicales no son dialogo
        contenido = re.sub(r"<[^>]+>|\([^)]*\)|\[[^\]]*\]|♪.*", " ", contenido)
        contenido = re.sub(r"\s+", " ", contenido).strip(" -")
        if re.search(r"\w", contenido):
            lineas.append(Linea(inicio, fin, contenido))
    return lineas


def norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", "", s.lower())


def comparar(humano: list[Linea], whisper: list[Linea]) -> dict:
    sin_cobertura = []   # linea humana sin ninguna whisper cerca -> Whisper no la agarro
    huerfanas = []        # linea whisper sin ninguna humana cerca -> posible alucinacion/extra
    desfases = []          # para las que si tienen pareja, que tan lejos esta el punto medio

    for h in humano:
        candidatos = [w for w in whisper if abs(w.medio - h.medio) <= TOLERANCIA_SEG]
        if not candidatos:
            sin_cobertura.append(h)
        else:
            mejor = min(candidatos, key=lambda w: abs(w.medio - h.medio))
            desfases.append(abs(mejor.medio - h.medio))

    for w in whisper:
        candidatos = [h for h in humano if abs(h.medio - w.medio) <= TOLERANCIA_SEG]
        if not candidatos:
            huerfanas.append(w)

    cobertura = 1 - len(sin_cobertura) / len(humano) if humano else 0.0
    return {
        "total_humano": len(humano),
        "total_whisper": len(whisper),
        "cobertura": cobertura,
        "sin_cobertura": sin_cobertura,
        "huerfanas": huerfanas,
        "desfase_prom": sum(desfases) / len(desfases) if desfases else 0.0,
        "desfase_max": max(desfases) if desfases else 0.0,
        "largas": [w for w in whisper if w.fin - w.inicio > 8 or len(w.texto) > 90],
    }


def reporte(nombre: str, r: dict) -> str:
    L = [f"=== {nombre} ===",
         f"Lineas humanas: {r['total_humano']} | Whisper: {r['total_whisper']} | "
         f"cobertura: {r['cobertura']*100:.0f}% | desfase promedio: {r['desfase_prom']:.1f}s "
         f"(max {r['desfase_max']:.1f}s) | lineas demasiado largas: {len(r['largas'])}"]
    if r["sin_cobertura"]:
        L.append(f"-- Dialogo que Whisper NO agarro ({len(r['sin_cobertura'])}) --")
        for h in r["sin_cobertura"][:15]:
            m = int(h.inicio // 60)
            s = int(h.inicio % 60)
            L.append(f"  {m:02d}:{s:02d}  [humano] {h.texto[:80]}")
    if r["huerfanas"]:
        L.append(f"-- Lineas Whisper sin pareja humana cerca ({len(r['huerfanas'])}) --")
        for w in r["huerfanas"][:15]:
            m = int(w.inicio // 60)
            s = int(w.inicio % 60)
            L.append(f"  {m:02d}:{s:02d}  [whisper] {w.texto[:80]}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Compara subtitulo Whisper vs humano del mismo video")
    ap.add_argument("humano", nargs="?", help="Subtitulo humano (.spa.srt)")
    ap.add_argument("whisper", nargs="?", help="Subtitulo generado (.whisper.es.srt)")
    ap.add_argument("--lote", help="Carpeta: busca pares *.spa.srt / *.whisper.es.srt por nombre de video")
    a = ap.parse_args()

    pares = []
    if a.lote:
        carpeta = Path(a.lote)
        for sp in sorted(carpeta.rglob("*.spa.srt")):
            base = sp.name[:-len(".spa.srt")]
            wp = sp.with_name(f"{base}.whisper.es.srt")
            if wp.exists():
                pares.append((base, sp, wp))
    elif a.humano and a.whisper:
        pares.append((Path(a.humano).stem, Path(a.humano), Path(a.whisper)))
    else:
        print("Usa humano+whisper, o --lote carpeta")
        return 2

    if not pares:
        print("No se encontraron pares *.spa.srt / *.whisper.es.srt")
        return 1

    coberturas = []
    for nombre, sp, wp in pares:
        r = comparar(parse_srt(sp), parse_srt(wp))
        print(reporte(nombre, r))
        print()
        coberturas.append(r["cobertura"])

    if len(pares) > 1:
        print(f"=== PROMEDIO ({len(pares)} episodios) ===")
        print(f"Cobertura promedio: {sum(coberturas)/len(coberturas)*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
