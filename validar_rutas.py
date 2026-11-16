#!/usr/bin/env python3
"""Completa/valida RUTAS.txt de esta maquina con selector grafico.

- Si no existe RUTAS.txt, lo crea a partir de RUTAS.txt.example.
- Para cada CLAVE=valor: si la ruta no existe (o trae <TU_USUARIO>), abre un
  selector de carpeta (o de .exe si el valor termina en .exe) y reescribe solo
  esa linea. Deja respaldo RUTAS.txt.bak antes de modificar.
- FFPROBE vacio es valido si ffprobe esta en el PATH.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
RUTAS_TXT = RAIZ / "RUTAS.txt"
RUTAS_EXAMPLE = RAIZ / "RUTAS.txt.example"
OPCIONALES_EN_PATH = {"FFPROBE": "ffprobe"}
NO_SON_RUTAS = {"OPENSUBTITLES_USUARIO", "OPENSUBTITLES_CLAVE"}  # credenciales opcionales


def elegir(clave: str, actual: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        try:
            return input(f"Ruta para {clave} (actual: {actual or 'vacia'}): ").strip() or None
        except EOFError:
            return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if actual.lower().endswith(".exe"):
        r = filedialog.askopenfilename(title=f"Selecciona {clave}", filetypes=[("Ejecutable", "*.exe"), ("Todos", "*.*")])
    else:
        r = filedialog.askdirectory(title=f"Selecciona carpeta para {clave}")
    root.destroy()
    return str(Path(r)) if r else None


def main() -> int:
    if not RUTAS_TXT.exists():
        if not RUTAS_EXAMPLE.exists():
            print("Falta RUTAS.txt.example")
            return 1
        shutil.copy(RUTAS_EXAMPLE, RUTAS_TXT)
        print("RUTAS.txt creado desde RUTAS.txt.example")

    lineas = RUTAS_TXT.read_text(encoding="utf-8").splitlines()
    cambios = False
    faltan = []
    for i, linea in enumerate(lineas):
        s = linea.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        clave, valor = (x.strip() for x in s.split("=", 1))
        valor = valor.strip('"')
        if clave in NO_SON_RUTAS:
            print(f"[OK] {clave}: {'configurado' if valor else 'vacio (opcional)'}")
            continue
        if not valor and clave in OPCIONALES_EN_PATH and shutil.which(OPCIONALES_EN_PATH[clave]):
            print(f"[OK] {clave}: en el PATH")
            continue
        if valor and "<" not in valor and Path(valor).exists():
            print(f"[OK] {clave}: {valor}")
            continue
        print(f"[--] {clave}: no existe -> {valor or '(vacio)'}")
        nuevo = elegir(clave, valor)
        if nuevo and Path(nuevo).exists():
            if not cambios:
                shutil.copy(RUTAS_TXT, RUTAS_TXT.with_suffix(".txt.bak"))
            lineas[i] = f"{clave}={nuevo}"
            cambios = True
            print(f"     -> {nuevo}")
        else:
            faltan.append(clave)

    if cambios:
        RUTAS_TXT.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        print("RUTAS.txt actualizado (respaldo en RUTAS.txt.bak)")
    if faltan:
        print("Pendientes: " + ", ".join(faltan))
        return 1
    print("Todas las rutas OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
