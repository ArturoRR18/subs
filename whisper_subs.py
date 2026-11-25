#!/usr/bin/env python3
"""Genera subtitulos en espanol con Whisper (faster-whisper) usando la GPU.

Uso directo:
  py whisper_subs.py "video.mkv"                  # escribe video.whisper.es.srt
  py whisper_subs.py "video.mkv" --modelo small --cpu

Normalmente se usa desde jellyfin_subs.py --generar.

Flujo por video:
  1. Elige la pista de audio (prefiere una etiquetada en espanol).
  2. La extrae a WAV 16 kHz mono con ffmpeg.
  3. Transcribe con faster-whisper (large-v3-turbo por defecto).
  4. Si el audio no es espanol pero es ingles, traduce cada linea con el modelo
     Opus-MT/Argos (CTranslate2) que vive en modelos/traductor-en-es.
  5. Escribe un .srt en UTF-8 con los tiempos de Whisper.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
MODELOS = RAIZ / "modelos"
TRADUCTOR_ZIP = MODELOS / "translate-en_es-1_0.argosmodel"
TRADUCTOR_DIR = MODELOS / "traductor-en-es"
TRADUCTOR_URL = "https://argos-net.com/v1/translate-en_es-1_0.argosmodel"

MARCA = ".whisper"              # queda en el nombre: <video>.whisper.es.srt
MODELO_DEF = "large-v3-turbo"   # ~1-2 min por episodio en una GTX 1060 6GB
IDIOMAS_TRADUCIBLES = {"en"}

MAX_CARACTERES_LINEA = 42       # se parte en 2 lineas como los subs comerciales


# --------------------------------------------------------------------------- CUDA

def preparar_dlls() -> None:
    """Las DLL de CUDA instaladas con pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)
    no estan en el PATH: hay que anunciarlas antes de importar ctranslate2."""
    if os.name != "nt":
        return
    for base in sys.path:
        nvidia = Path(base) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass


def disponible() -> str | None:
    """None si se puede generar; si no, el motivo."""
    preparar_dlls()
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return "faster-whisper no esta instalado: py -m pip install faster-whisper"
    return None


# --------------------------------------------------------------------------- audio

def pistas_audio(video: Path, ff: str | None) -> list[dict]:
    if not ff:
        return []
    try:
        r = subprocess.run([ff, "-v", "error", "-select_streams", "a", "-print_format", "json",
                            "-show_streams", str(video)],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    pistas = []
    for st in streams:
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        pistas.append({"index": st.get("index"),
                       "idioma": (tags.get("language") or "").lower(),
                       "titulo": tags.get("title", ""),
                       "defecto": bool((st.get("disposition") or {}).get("default"))})
    return pistas


def elegir_pista(pistas: list[dict]) -> dict | None:
    """Prefiere una pista en espanol; si no hay, la pista por defecto."""
    for p in pistas:
        if p["idioma"] in ("spa", "es", "es-419", "es-mx", "esp"):
            return p
    for p in pistas:
        if p["defecto"]:
            return p
    return pistas[0] if pistas else None


def extraer_wav(video: Path, pista: dict | None, ffmpeg: str, destino: Path) -> bool:
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(video)]
    if pista and pista.get("index") is not None:
        cmd += ["-map", f"0:{pista['index']}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destino)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and destino.exists() and destino.stat().st_size > 1024


# --------------------------------------------------------------------------- traductor

def preparar_traductor() -> Path | None:
    """Descomprime el .argosmodel la primera vez. None si no esta descargado."""
    if (TRADUCTOR_DIR / "sentencepiece.model").exists() and (TRADUCTOR_DIR / "model").is_dir():
        return TRADUCTOR_DIR
    if not TRADUCTOR_ZIP.exists():
        return None
    TRADUCTOR_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(TRADUCTOR_ZIP) as z:
        z.extractall(TRADUCTOR_DIR)
    # el zip trae todo dentro de una carpeta: translate-en_es-1_0/...
    if not (TRADUCTOR_DIR / "sentencepiece.model").exists():
        for sp in TRADUCTOR_DIR.rglob("sentencepiece.model"):
            interior = sp.parent
            for item in interior.iterdir():
                item.rename(TRADUCTOR_DIR / item.name)
            interior.rmdir()
            break
    if (TRADUCTOR_DIR / "sentencepiece.model").exists() and (TRADUCTOR_DIR / "model").is_dir():
        return TRADUCTOR_DIR
    return None


class Traductor:
    """Opus-MT ingles->espanol en CTranslate2 (el mismo modelo que usa Argos Translate).
    Corre en CPU para no pelear por la VRAM con Whisper."""

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
            res = self.motor.translate_batch(tokens, beam_size=4, replace_unknowns=True,
                                             max_batch_size=32)
            for original, r in zip(lote, res):
                try:
                    salida.append(self.sp.DecodePieces(r.hypotheses[0]).strip() or original)
                except (IndexError, TypeError):
                    salida.append(original)
        return salida


# --------------------------------------------------------------------------- srt

def fmt_srt(seg: float) -> str:
    seg = max(0.0, seg)
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def partir_lineas(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= MAX_CARACTERES_LINEA:
        return texto
    palabras, lineas, actual = texto.split(" "), [], ""
    for w in palabras:
        if actual and len(actual) + 1 + len(w) > MAX_CARACTERES_LINEA:
            lineas.append(actual)
            actual = w
        else:
            actual = f"{actual} {w}".strip()
    if actual:
        lineas.append(actual)
    if len(lineas) > 2:   # junta el sobrante en dos lineas parejas
        mitad = len(texto) // 2
        corte = texto.rfind(" ", 0, mitad + 10)
        lineas = [texto[:corte].strip(), texto[corte:].strip()] if corte > 0 else [texto]
    return "\n".join(lineas)


def escribir_srt(bloques: list[tuple[float, float, str]], destino: Path) -> int:
    partes = []
    n = 0
    for inicio, fin, texto in bloques:
        texto = partir_lineas(texto)
        if not texto:
            continue
        n += 1
        partes.append(f"{n}\n{fmt_srt(inicio)} --> {fmt_srt(max(fin, inicio + 0.4))}\n{texto}\n")
    destino.write_text("\n".join(partes), encoding="utf-8")
    return n


# --------------------------------------------------------------------------- generar

def _cargar_whisper(modelo: str, cpu: bool):
    from faster_whisper import WhisperModel
    intentos = ([("cpu", "int8")] if cpu else
                # la GTX 1060 (Pascal) es lenta en float16: int8 le sienta mejor
                [("cuda", "int8_float16"), ("cuda", "float16"), ("cpu", "int8")])
    ultimo = None
    for dispositivo, tipo in intentos:
        try:
            return WhisperModel(modelo, device=dispositivo, compute_type=tipo), dispositivo, tipo
        except Exception as e:  # noqa: BLE001 - sin CUDA/cuDNN cae a CPU
            ultimo = e
    raise RuntimeError(f"no se pudo cargar el modelo {modelo}: {ultimo}")


def generar(video: Path, ff: str | None, ffmpeg: str | None, modelo: str = MODELO_DEF,
            cpu: bool = False, traducir: bool = True, avisar=print) -> tuple[Path | None, str]:
    """Genera <video>.whisper.es.srt. Devuelve (ruta, nota) o (None, motivo)."""
    falta = disponible()
    if falta:
        return None, falta
    if not ffmpeg:
        return None, "ffmpeg no encontrado (hace falta para extraer el audio)"

    pista = elegir_pista(pistas_audio(video, ff))
    etiqueta = (pista or {}).get("idioma", "")
    tmp = Path(tempfile.mkdtemp(prefix="whisper_subs_"))
    wav = tmp / "audio.wav"
    try:
        avisar(f"  extrayendo audio{' (pista ' + str(pista['index']) + ' ' + etiqueta + ')' if pista else ''}...")
        if not extraer_wav(video, pista, ffmpeg, wav):
            return None, "ffmpeg no pudo extraer el audio"

        mdl, dispositivo, tipo = _cargar_whisper(modelo, cpu)
        forzado = "es" if etiqueta in ("spa", "es", "es-419", "es-mx", "esp") else None
        avisar(f"  transcribiendo con {modelo} en {dispositivo} ({tipo})...")
        segmentos, info = mdl.transcribe(str(wav), language=forzado, task="transcribe",
                                         vad_filter=True, beam_size=5,
                                         condition_on_previous_text=False)
        idioma = (info.language or "?").lower()
        bloques: list[tuple[float, float, str]] = []
        anterior = ""
        repetidos = 0
        for s in segmentos:
            texto = re.sub(r"\s+", " ", s.text or "").strip()
            if not texto:
                continue
            if texto == anterior:         # Whisper se traba y repite la misma linea
                repetidos += 1
                if repetidos > 2:
                    continue
            else:
                repetidos = 0
            anterior = texto
            bloques.append((s.start, s.end, texto))
        del mdl

        if not bloques:
            return None, "Whisper no encontro dialogos en el audio"

        nota = f"generado con Whisper ({modelo}, audio {idioma})"
        if idioma not in ("es",):
            if not traducir:
                return None, f"el audio esta en {idioma} y la traduccion esta desactivada"
            if idioma not in IDIOMAS_TRADUCIBLES:
                return None, f"el audio esta en {idioma}; solo se traduce desde {'/'.join(IDIOMAS_TRADUCIBLES)}"
            carpeta = preparar_traductor()
            if not carpeta:
                return None, (f"falta el modelo traductor: baja {TRADUCTOR_URL} "
                              f"a {TRADUCTOR_ZIP.relative_to(RAIZ)}")
            avisar(f"  traduciendo {len(bloques)} lineas de {idioma} a espanol...")
            tr = Traductor(carpeta)
            textos = tr([b[2] for b in bloques])
            bloques = [(b[0], b[1], t) for b, t in zip(bloques, textos)]
            nota = f"generado con Whisper ({modelo}) y traducido {idioma}->es"

        destino = video.with_name(f"{video.stem}{MARCA}.es.srt")
        if destino.exists():
            destino.unlink()
        n = escribir_srt(bloques, destino)
        return destino, f"{nota}, {n} lineas"
    except Exception as e:  # noqa: BLE001 - un video que falla no detiene el lote
        return None, f"error generando con Whisper: {e}"
    finally:
        for p in tmp.glob("*"):
            p.unlink(missing_ok=True)
        tmp.rmdir()


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Genera subtitulos en espanol con Whisper")
    ap.add_argument("video", nargs="+", help="Archivos de video")
    ap.add_argument("--modelo", default=MODELO_DEF, help=f"Modelo de Whisper (por defecto {MODELO_DEF})")
    ap.add_argument("--cpu", action="store_true", help="Forzar CPU (sin GPU)")
    ap.add_argument("--sin-traducir", action="store_true", help="No traducir audios en ingles")
    a = ap.parse_args()

    import jellyfin_subs as js
    rutas = js.leer_rutas()
    ff = js.ffprobe_bin(rutas)
    ffmpeg = js.ffmpeg_bin(rutas)

    codigo = 0
    for v in a.video:
        p = Path(v).expanduser()
        if not p.is_file():
            print(f"[--] no existe: {p}")
            codigo = 1
            continue
        print(f"[..] {p.name}")
        ruta, nota = generar(p, ff, ffmpeg, a.modelo, a.cpu, not a.sin_traducir)
        if ruta:
            print(f"[OK] {ruta.name}: {nota}")
        else:
            print(f"[--] {p.name}: {nota}")
            codigo = 1
    return codigo


if __name__ == "__main__":
    sys.exit(main())
