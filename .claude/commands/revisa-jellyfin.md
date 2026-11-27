---
description: Revisa toda la biblioteca de Jellyfin (películas y series), consigue subtítulos y ordena nombres
argument-hint: [texto a buscar opcional]
---
Revisa la biblioteca de Jellyfin en esta conversación (sin subagente). Si $ARGUMENTS
no está vacío, agrega `--buscar "$ARGUMENTS"` a cada comando. Usa siempre `py`.
Los pasos que bajan/traducen tardan: usa timeouts largos o `run_in_background`.

1. Diagnóstico: `py verificar_jellyfin.py` → cuántos videos no tienen sub ni audio
   en español (películas y episodios).
2. Conseguir subtítulos (simulación, no renombra):
   `py ordenar_biblioteca.py --descargar --traducir`
   Orden: latino > castellano > inglés traducido > italiano traducido. Revisa el
   reporte con criterio (títulos de TMDB raros, motivos de descarte dudosos).
3. Aplicar (no hace falta pedir luz verde): `py ordenar_biblioteca.py --aplicar`.
   Renombra en su lugar a la convención de Jellyfin (`.spa.srt`, original en
   `.eng/.ita.srt`). Ojo: Jellyfin pierde el "visto" de lo que se renombra.
4. Espera a que Jellyfin reescanee y confirma: `py verificar_jellyfin.py --escanear`.
5. Entrega, corto:
   - cuántos se ordenaron (películas / episodios) y cuáles quedaron con sub **traducido**
     (para que el usuario revise la traducción);
   - los que siguen sin subtítulo (se pueden mandar a "crea subtítulos");
   - la lista de "REVISAR A MANO" con su motivo.
