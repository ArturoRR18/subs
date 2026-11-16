# Revisor de subtítulos para Jellyfin

Este proyecto prepara películas y series para subirlas a Jellyfin: revisa que cada
video tenga subtítulos en **español latino** (o, en su defecto, español), que
cuadren con el video, los nombra con la convención de Jellyfin y los acomoda en
carpetas dentro de `REV_SUBTITULOS`.

Responde siempre en español, corto y directo.

## Comando: "iniciar proceso"
Cuando yo escriba **"iniciar proceso"** (o `/iniciar-proceso`), delega en el
subagente `revisor-subtitulos` (`.claude/agents/revisor-subtitulos.md`), que hace:

1. Lee `CARPETA_ENTRADA` de `RUTAS.txt` (si escribo otra ruta, usa esa con `--carpeta`).
2. Simulación: `python jellyfin_subs.py` → revisa el reporte.
3. Si hay videos sin subtítulo válido y `subliminal` está instalado:
   `python jellyfin_subs.py --descargar` (latino primero, luego español) y vuelve a simular.
4. Aplica: `python jellyfin_subs.py --aplicar` (no hace falta pedirme luz verde).
5. Me entrega el resumen: cuántos pasaron a `REV_SUBTITULOS`, y la lista de
   `SIN_SUBTITULOS.txt` con el motivo de cada uno.

## Reglas del proceso
- **Idioma:** prioridad `es-419` (latino) > `es` (castellano). Inglés u otro idioma = no válido.
- **Válido contra el video** (lo compara con la duración de `ffprobe`):
  - el último diálogo termina entre el 60 % de la duración y +3 min después;
  - el primer diálogo no aparece después del 25 % del video;
  - suficientes diálogos (descarta subtítulos de solo "forzados");
  - si el `.mkv` ya trae una pista interna en español (no forzada), cuenta como válido.
- **Carpetas** dentro de `<CARPETA_ENTRADA>\REV_SUBTITULOS\`:
  - Película: `Titulo (Año)\Titulo (Año).mkv` + `Titulo (Año).spa.srt`
  - Serie (detecta `S01E02`, `1x02`, `Temporada 1 Capitulo 2`): todos los
    episodios de la misma serie van juntos →
    `Serie\Season 01\Serie S01E02.mkv` + `Serie S01E02.spa.srt`
- **Sufijo de idioma `.spa`:** Jellyfin solo reconoce códigos ISO 639 (`es`/`spa`).
  Un código como `es-419` no lo reconoce y toma todo el sufijo como título, así
  que latino y castellano se guardan ambos como `.spa` (el reporte indica cuál es).
- Los subtítulos se guardan en **UTF-8** (convierte cp1252/latin-1 para que se
  vean bien acentos y ñ).
- **Nunca sobrescribe:** si el destino ya existe, se detiene y avisa.
- Lo que no pasa la revisión **se queda donde está** y se lista en
  `<CARPETA_ENTRADA>\SIN_SUBTITULOS.txt`.

## Requisitos
- Python 3.10+
- `ffprobe` (viene con FFmpeg) — en el PATH o en `FFPROBE` de `RUTAS.txt`.
- Opcional para descargar: `pip install subliminal`

## Comando: "nueva computadora"
Cuando yo escriba **"nueva computadora"**, o al arrancar en un equipo donde el
repo se acaba de clonar (falta RUTAS.txt), guíame por este checklist:

1. Clonar el repo (si no está clonado ya): `git clone <URL-DEL-REPO>`
2. Confirmar que la carpeta de videos (Escritorio/NAS) existe y ya sincronizó en esta máquina.
3. Completar RUTAS.txt de esta máquina: `python validar_rutas.py` (o doble clic
   en `validar_rutas.bat`) — abre selector gráfico para cada ruta faltante.
4. Verificar con "revisar rutas" (Test-Path de cada clave).
5. Prueba: `python jellyfin_subs.py` (simulación) para confirmar que el equipo funciona.

No hace falta pedirme luz verde para los pasos 2-5 una vez que confirme el clone.

**Regla dura:** nunca escribas una ruta específica de UNA máquina (usuario,
carpeta elegida por una sola persona, etc.) en este `CLAUDE.md` — se sincroniza
vía git a todos los equipos. Las rutas de máquina van siempre en `RUTAS.txt`
(gitignored).

## Control de versiones (git)
- **Commit automático:** al terminar cada tarea que deje el código en un estado
  estable, haz commit sin pedirme luz verde.
- **Push/pull automáticos** vía hooks en `.claude/settings.json`:
  - `SessionStart` → `git fetch` + `git pull --ff-only` (nunca crea merges ni pisa
    cambios locales; si hay conflicto solo avisa).
  - `Stop` → `git push` de lo que ya esté comiteado.
- **Por qué `Stop` y no un hook de "cierre de sesión":** ningún sistema de hooks
  garantiza dispararse si el usuario cierra la ventana/terminal a la fuerza (mata
  el proceso en vez de salir limpio). `Stop` sí es confiable — se dispara al final
  de cada respuesta del agente — así que el push queda al día constantemente en
  vez de depender de un cierre limpio que podría no llegar a ocurrir.
- El hook de `Stop` solo hace `push`, nunca `add`/`commit`: decidir qué vale la
  pena guardar es criterio del agente, no de un script que correría tras cada respuesta.
- **Trabajar offline:** quita o comenta temporalmente el bloque `hooks` de
  `.claude/settings.json`.
- Nunca se suben videos ni subtítulos: están en `.gitignore`.
