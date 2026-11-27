#!/usr/bin/env python3
"""Traduce subtitulos (.srt) de ingles o italiano a espanol, conservando los tiempos.

Es el paso intermedio entre "no hay subtitulo en espanol" y "generarlo con Whisper":
un subtitulo humano en otro idioma ya trae los tiempos correctos y todos los
dialogos; solo se cambia el texto.

Motor: modelos Opus-MT de Argos Translate corriendo en CTranslate2 (CPU, sin
internet una vez bajados). Los modelos viven en modelos/ (en .gitignore) y se
bajan solos la primera vez (~90 MB cada uno):
  ingles   -> espanol: translate-en_es
  italiano -> espanol: translate-it_en + translate-en_es (no hay modelo directo it->es)

Uso directo:
  py traducir_subs.py "pelicula.en.srt"            # escribe pelicula.en.traducido.es.srt
  py traducir_subs.py "pelicula.it.srt" --idioma it
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
MODELOS = RAIZ / "modelos"
URL_MODELO = "https://argos-net.com/v1/translate-{par}-1_0.argosmodel"

# idioma de origen -> cadena de modelos hasta el espanol
CADENAS = {"en": ["en_es"], "it": ["it_en", "en_es"]}
NOMBRE_IDIOMA = {"en": "ingles", "it": "italiano"}

MARCA = ".traducido"          # <fuente>.traducido.es.srt
MAX_CARACTERES_LINEA = 42
MAX_UNIR = 3                  # bloques seguidos que forman una sola frase

# Argos/Opus-MT traduce con vocabulario de Espana; cambios seguros a latino
# (sustantivos y conjugaciones de "vosotros" sin ambiguedad).
LATINO = [
    (r"\bvosotros\b", "ustedes"), (r"\bvosotras\b", "ustedes"), (r"\bvuestro\b", "su"),
    (r"\bvuestra\b", "su"), (r"\bvuestros\b", "sus"), (r"\bvuestras\b", "sus"),
    (r"\bhabéis\b", "han"), (r"\btenéis\b", "tienen"), (r"\bsois\b", "son"), (r"\bestáis\b", "están"),
    (r"\bqueréis\b", "quieren"), (r"\bpodéis\b", "pueden"), (r"\bvais\b", "van"), (r"\bsabéis\b", "saben"),
    (r"\bhacéis\b", "hacen"), (r"\bdecís\b", "dicen"), (r"\bvenís\b", "vienen"),
    (r"\bordenador(es)?\b", r"computadora\1"), (r"\bmóvil\b", "celular"), (r"\bmóviles\b", "celulares"),
    (r"\bcoche\b", "auto"), (r"\bcoches\b", "autos"), (r"\bzumo\b", "jugo"), (r"\bgafas\b", "lentes"),
    (r"\bpiso\b(?= (?:de|en|nuevo|grande|pequeño))", "departamento"),
    (r"(^|[.!?¡¿]\s*|- )Vale\b", r"\1Está bien"), (r"\bconducir\b", "manejar"), (r"\bconduce\b", "maneja"),
    (r"\bconduces\b", "manejas"), (r"\bconduzco\b", "manejo"), (r"\bcoger\b", "tomar"),
]


def partir_lineas(texto: str, maximo: int = MAX_CARACTERES_LINEA) -> str:
    """Parte el texto en a lo mas 2 lineas parejas, como los subs comerciales."""
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= maximo:
        return texto
    mitad = len(texto) // 2
    izq, der = texto.rfind(" ", 0, mitad + 1), texto.find(" ", mitad)
    candidatos = [c for c in (izq, der) if c > 0]
    if not candidatos:
        return texto
    corte = min(candidatos, key=lambda c: abs(c - mitad))
    return texto[:corte].strip() + "\n" + texto[corte:].strip()


# --------------------------------------------------------------------------- modelos

def preparar(par: str, bajar: bool = True) -> Path | None:
    """Carpeta del modelo `par` (ej. "en_es") lista para CTranslate2; la baja y
    descomprime la primera vez. None si no se pudo."""
    carpeta = MODELOS / f"traductor-{par.replace('_', '-')}"
    if (carpeta / "sentencepiece.model").exists() and (carpeta / "model").is_dir():
        return carpeta
    zip_ = MODELOS / f"translate-{par}-1_0.argosmodel"
    if not zip_.exists():
        if not bajar:
            return None
        MODELOS.mkdir(parents=True, exist_ok=True)
        print(f"Bajando modelo traductor {par} (~90 MB)...", file=sys.stderr, flush=True)
        try:
            tmp = zip_.with_suffix(".part")
            urllib.request.urlretrieve(URL_MODELO.format(par=par), tmp)
            tmp.rename(zip_)
        except OSError as e:
            print(f"No se pudo bajar el modelo {par}: {e}", file=sys.stderr)
            return None
    carpeta.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_) as z:
        z.extractall(carpeta)
    if not (carpeta / "sentencepiece.model").exists():   # el zip trae una carpeta interior
        for sp in carpeta.rglob("sentencepiece.model"):
            interior = sp.parent
            for item in interior.iterdir():
                item.rename(carpeta / item.name)
            interior.rmdir()
            break
    if (carpeta / "sentencepiece.model").exists() and (carpeta / "model").is_dir():
        return carpeta
    return None


class Traductor:
    """Un modelo Opus-MT (Argos) en CTranslate2. Corre en CPU para no pelear por la
    VRAM con Whisper."""

    def __init__(self, carpeta: Path):
        import ctranslate2
        import sentencepiece
        self.sp = sentencepiece.SentencePieceProcessor()
        self.sp.Load(str(carpeta / "sentencepiece.model"))
        self.motor = ctranslate2.Translator(str(carpeta / "model"), device="cpu", compute_type="int8")

    def __call__(self, textos: list[str]) -> list[str]:
        salida: list[str] = []
        for i in range(0, len(textos), 32):
            lote = textos[i:i + 32]
            tokens = [self.sp.EncodeAsPieces(t) for t in lote]
            res = self.motor.translate_batch(tokens, beam_size=4, replace_unknowns=True, max_batch_size=32)
            for original, r in zip(lote, res):
                try:
                    salida.append(self.sp.DecodePieces(r.hypotheses[0]).strip() or original)
                except (IndexError, TypeError):
                    salida.append(original)
        return salida


def disponible(idioma: str) -> str | None:
    """None si se puede traducir desde `idioma`; si no, el motivo."""
    if idioma not in CADENAS:
        return f"no se traduce desde {idioma}"
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return "falta el traductor: py -m pip install ctranslate2 sentencepiece"
    for par in CADENAS[idioma]:
        if not preparar(par):
            return f"falta el modelo traductor {par} ({URL_MODELO.format(par=par)})"
    return None


_cache: dict[str, Traductor] = {}


def traducir_textos(textos: list[str], idioma: str) -> list[str]:
    for par in CADENAS[idioma]:
        if par not in _cache:
            _cache[par] = Traductor(preparar(par))
        textos = _cache[par](textos)
    return [a_latino(t) for t in textos]


def a_latino(t: str) -> str:
    for patron, cambio in LATINO:
        t = re.sub(patron, cambio, t)
    return t


# --------------------------------------------------------------------------- srt

RE_ETIQUETA = re.compile(r"</?(?:i|b|u|font)[^>]*>", re.I)
RE_POS = re.compile(r"^\{\\an?\d+\}")
FIN_FRASE = tuple(".?!…\"'»♪:)]")


class Bloque:
    def __init__(self, tiempo: str, lineas: list[str]):
        self.tiempo = tiempo
        crudo = "\n".join(lineas)
        m = RE_POS.match(crudo)
        self.pos = m.group(0) if m else ""
        crudo = crudo[len(self.pos):]
        self.italica = bool(re.match(r"^\s*<i>", crudo, re.I)) and crudo.rstrip().lower().endswith("</i>")
        limpio = [RE_ETIQUETA.sub("", l).strip() for l in crudo.split("\n")]
        limpio = [l for l in limpio if l]
        # "- Hola. / - Adios." son dos personas: se traducen por separado
        self.dialogo = len(limpio) > 1 and all(l.startswith("-") for l in limpio)
        self.partes = [l.lstrip("- ").strip() for l in limpio] if self.dialogo else [" ".join(limpio)]
        self.traducido: list[str] = []

    @property
    def tiene_texto(self) -> bool:
        return any(re.search(r"[A-Za-zÀ-ÿ]", p) for p in self.partes)

    def salida(self) -> str:
        if self.dialogo:
            texto = "\n".join(f"- {t}" for t in self.traducido)
        else:
            texto = partir_lineas(" ".join(self.traducido))
        if self.italica:
            texto = f"<i>{texto}</i>"
        return self.pos + texto


def leer_bloques(texto: str) -> list[Bloque]:
    bloques = []
    for b in re.split(r"\n\s*\n", texto.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lineas = b.split("\n")
        i = next((k for k, l in enumerate(lineas) if "-->" in l), None)
        if i is None:
            continue
        bloques.append(Bloque(lineas[i].strip(), lineas[i + 1:]))
    return bloques


def _repartir(texto: str, pesos: list[int]) -> list[str]:
    """Reparte una frase traducida entre varios bloques segun el largo del original."""
    palabras = texto.split()
    if len(pesos) == 1 or len(palabras) < len(pesos):
        return [texto] + [""] * (len(pesos) - 1)
    total, salida, i = sum(pesos), [], 0
    acumulado = 0
    for n, w in enumerate(pesos):
        acumulado += w
        hasta = len(palabras) if n == len(pesos) - 1 else max(i + 1, round(len(palabras) * acumulado / total))
        hasta = min(hasta, len(palabras) - (len(pesos) - n - 1))
        salida.append(" ".join(palabras[i:hasta]))
        i = hasta
    return salida


def traducir_srt(texto: str, idioma: str) -> tuple[str, int]:
    """Devuelve (srt traducido, numero de dialogos). Los tiempos quedan identicos."""
    bloques = leer_bloques(texto)
    # agrupa frases que siguen en el bloque siguiente ("I was going to" / "the store.")
    grupos: list[list[Bloque]] = []
    for b in bloques:
        if not b.tiene_texto:
            b.traducido = b.partes
            continue
        g = grupos[-1] if grupos else None
        if (g and len(g) < MAX_UNIR and not b.dialogo and not g[-1].dialogo and g[-1].tiene_texto
                and g[-1].italica == b.italica and not g[-1].partes[0].endswith(FIN_FRASE)):
            g.append(b)
        else:
            grupos.append([b])

    unidades: list[str] = []
    for g in grupos:
        if g[0].dialogo:
            unidades.extend(g[0].partes)
        else:
            unidades.append(" ".join(b.partes[0] for b in g).replace("... ...", " ").replace(".. ..", " "))
    traducidas = traducir_textos(unidades, idioma)

    k = 0
    for g in grupos:
        if g[0].dialogo:
            n = len(g[0].partes)
            g[0].traducido = traducidas[k:k + n]
            k += n
        else:
            for b, t in zip(g, _repartir(traducidas[k], [max(len(b.partes[0]), 1) for b in g])):
                b.traducido = [t]
            k += 1

    salida, n = [], 0
    for b in bloques:
        cuerpo = b.salida().strip()
        if not cuerpo:
            continue
        n += 1
        salida.append(f"{n}\n{b.tiempo}\n{cuerpo}")
    return "\n\n".join(salida) + "\n", n


def nombre_traducido(fuente: Path) -> Path:
    return fuente.with_name(f"{fuente.stem}{MARCA}.es.srt")


def fuente_de(traducido: Path) -> Path | None:
    """Subtitulo original a partir del nombre del traducido."""
    sufijo = f"{MARCA}.es.srt"
    if not traducido.name.lower().endswith(sufijo):
        return None
    base = traducido.name[:-len(sufijo)]
    for ext in (".srt", ".ass", ".ssa", ".vtt"):
        p = traducido.with_name(base + ext)
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Traduce un .srt de ingles/italiano a espanol")
    ap.add_argument("srt", nargs="+")
    ap.add_argument("--idioma", default="en", choices=sorted(CADENAS), help="Idioma del subtitulo (en / it)")
    a = ap.parse_args()
    falta = disponible(a.idioma)
    if falta:
        print(falta)
        return 1
    sys.path.insert(0, str(RAIZ))
    from jellyfin_subs import leer_texto
    for s in a.srt:
        p = Path(s)
        texto, n = traducir_srt(leer_texto(p)[0], a.idioma)
        destino = nombre_traducido(p)
        destino.write_text(texto, encoding="utf-8")
        print(f"[OK] {destino.name}: {n} dialogos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
