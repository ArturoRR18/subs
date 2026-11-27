---
description: Último recurso - genera subtítulos en español con Whisper para lo que siga sin subtítulo
argument-hint: [s-rev | biblioteca | ambos (por defecto)]
---
Genera subtítulos con Whisper en esta conversación. Solo para lo que ya pasó por
"iniciar proceso" / "revisa jellyfin" y sigue sin subtítulo (ni en español, ni en
inglés/italiano para traducir). Usa siempre `py` y timeouts largos o
`run_in_background` (minutos por video).

1. Revisa que esta PC pueda: `py -c "import whisper_subs as w; print(w.disponible() or 'ok')"`.
   Si falta faster-whisper o no hay GPU NVIDIA, dilo y detente: se corre en la PC con GPU.
2. Alcance según $ARGUMENTS (vacío = ambos):
   - **S-REV**: `py jellyfin_subs.py --generar` → revisa el reporte →
     `py jellyfin_subs.py --aplicar`. Lo generado se mueve a `REV_SUBTITULOS`.
   - **biblioteca**: `py ordenar_biblioteca.py --generar` → revisa →
     `py ordenar_biblioteca.py --aplicar` y luego `py verificar_jellyfin.py --escanear`.
3. Entrega: cuántos se generaron, de qué idioma de audio (español directo o inglés/
   italiano traducido) y cuáles fallaron con su motivo. Recuerda al usuario que los
   tiempos de Whisper son aproximados y conviene revisarlos.
