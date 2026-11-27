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
2. Simulación: `py jellyfin_subs.py` → revisa el reporte.
3. Si hay videos sin subtítulo válido y `subliminal` está instalado:
   `py jellyfin_subs.py --descargar` (latino primero, luego español) y vuelve a simular.
4. Aplica: `py jellyfin_subs.py --aplicar` (no hace falta pedirme luz verde).
5. Me entrega el resumen: cuántos pasaron a `REV_SUBTITULOS`, y la lista de
   `SIN_SUBTITULOS.txt` con el motivo de cada uno.

## Comando: "verificar jellyfin"
Después de mover `REV_SUBTITULOS` a la biblioteca: `py verificar_jellyfin.py --escanear
[--buscar "<titulo>"]`. Le pregunta a Jellyfin (API, `JELLYFIN_URL` + `JELLYFIN_API_KEY`
en `RUTAS.txt`) qué subtítulos detectó por película/episodio. Si sale `[OK] sub externo`,
Jellyfin ya lo leyó y aparece en el menú — no hace falta probarlos uno a uno. Un video
con audio en español también cuenta como OK. Ojo: Jellyfin muestra el título traducido
(ej. "Avatar: Los Siete Refugios"); `--buscar` también busca en la ruta.

## Comando: "ordenar biblioteca"
Para películas que YA están en la biblioteca de Jellyfin (`BIBLIOTECA_PELICULAS` en
`RUTAS.txt` = la carpeta de red que Jellyfin ve como biblioteca de películas):
`py ordenar_biblioteca.py [--todas] [--descargar] [--aplicar] [--buscar "<texto>"]`.
- Sin `--todas`: solo las que Jellyfin ve sin sub/audio en español. Con `--todas`: también
  las que no siguen la convención.
- Busca el título en TMDB vía Jellyfin. Se queda con el título exacto, luego con el
  id que Jellyfin ya tenía y luego con el año exacto. Si no hay coincidencia, no adivina:
  la deja en "REVISAR A MANO".
- Busca subs en español en la carpeta y sus subcarpetas (`Subs\spa.srt`,
  `*-spanish-yify\`, `.srt` sin código de idioma). Ignora otros idiomas por su nombre
  (`por`, `cat`, `ita`…) y los valida con las reglas de abajo.
- Renombra en su lugar: `Titulo (Año) [tmdbid-N]\Titulo (Año).ext` + `.spa.srt`
  (+ `.eng.srt` si había uno en inglés). Borra los candidatos de `--descargar` que no se
  eligieron. Las carpetas con varios videos (sagas, partes) se revisan a mano.
- Ojo: Jellyfin trata la ruta nueva como otro elemento (se pierde el "visto").

## Comando: "tema jellyfin"
Cuando yo escriba **"tema jellyfin"** (o `/tema-jellyfin`), sigue
`.claude/commands/tema-jellyfin.md` en esta misma conversación (no en un subagente:
es un ida y vuelta conmigo). Junta lo que me gusta de varios temas/mods CSS en
`tema\custom.css`, anota mis decisiones en `tema\GUSTOS.md` y lo sube a Jellyfin
con `py tema_jellyfin.py --aplicar` (respalda antes; `--restaurar` vuelve atrás).

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
- **Rescate** de subs en español que fallan por tiempos (solo `.srt`):
  1. reacomoda líneas con horas rotas (malformadas, que retroceden o pasan del
     final) entre el diálogo anterior y el siguiente — solo si es ≤10 % del
     archivo; los tiempos quedan aproximados y el reporte lo indica;
  2. si no basta, `ffsubsync` lo alinea con el audio (cache en el temp del sistema).
  - Siempre quita marcas `[cite: N]` de generadores automáticos.
- **Descarga** (`--descargar`): baja hasta 4 candidatos por video (latino primero)
  de OpenSubtitles.com (si hay `OPENSUBTITLES_USUARIO`/`CLAVE` en `RUTAS.txt`),
  OpenSubtitles.org (sin cuenta), Subtitulamos, Subtis, Podnapisi, Gestdown, Addic7ed, TVsubtitles y BSPlayer.
  Subdivx bloquea el acceso automatizado, no se usa.
- **Generación con Whisper** (`--generar`, último recurso, necesita GPU): para los
  videos que siguen sin subtítulo, `whisper_subs.py` extrae el audio con ffmpeg y
  lo transcribe con faster-whisper (`large-v3-turbo`; cambia con `WHISPER_MODELO`
  en `RUTAS.txt`). Si el audio ya está en español transcribe directo; si está en
  inglés traduce línea por línea con el modelo Opus-MT/Argos en
  `modelos\traductor-en-es` (CPU, no pelea por la VRAM). Otros idiomas no se
  traducen: quedan en `SIN_SUBTITULOS.txt`.
  - El archivo se llama `<video>.whisper.es.srt` y pasa por la misma validación.
    En el reporte sale como "generado con Whisper", y puntúa por debajo de
    cualquier subtítulo humano (si aparece uno real, gana el real).
  - Es el paso más lento (minutos por video) y los tiempos son aproximados:
    úsalo solo cuando no haya nada en línea.
  - GPU Pascal (ej. GTX 1060) o más vieja: no soporta float16 eficiente, cae a
    `int8` en CUDA (`whisper_subs.py` ya intenta esa combinación). En Windows,
    además de `os.add_dll_directory`, las rutas de `nvidia-cublas-cu12`/
    `nvidia-cudnn-cu12` se agregan también al `PATH` del proceso — ctranslate2
    carga esas DLL por su cuenta (no vía el import de Python) y solo las
    encuentra ahí. Decodifica el audio con el WAV de ffmpeg directo (no con
    `av`/PyAV): las ruedas de `av` para Python 3.14 ya no aceptan el parámetro
    que usa faster-whisper 1.2.1 para abrir el archivo.
- Los subtítulos se guardan en **UTF-8** (convierte cp1252/latin-1 para que se
  vean bien acentos y ñ).
- **Nunca sobrescribe:** si el destino ya existe, se detiene y avisa.
- Lo que no pasa la revisión **se queda donde está** y se lista en
  `<CARPETA_ENTRADA>\SIN_SUBTITULOS.txt`.

## NAS por VPN (`NAS_ALTERNO`)
Si en `RUTAS.txt` está `NAS_ALTERNO=<host o IP>` (ej. la IP de Tailscale del NAS) y el
host de `CARPETA_ENTRADA`/`BIBLIOTECA_PELICULAS`/`JELLYFIN_URL` no responde, `leer_rutas()`
cambia ese host por el alterno en las tres claves y avisa en la salida. En la red de casa
no cambia nada.

## Requisitos
- Python 3.10+
- `ffprobe` (viene con FFmpeg) — en el PATH o en `FFPROBE` de `RUTAS.txt`.
- Opcional para descargar: `py -m pip install subliminal`
- Opcional para sincronizar: `py -m pip install ffsubsync` (usa `ffmpeg` del PATH)
- Opcional para generar (`--generar`, solo en la PC con GPU NVIDIA):
  `py -m pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 sentencepiece`
  y el traductor en `modelos\translate-en_es-1_0.argosmodel`
  (`https://argos-net.com/v1/translate-en_es-1_0.argosmodel`; `modelos\` está en
  `.gitignore`, hay que bajarlo en cada máquina). Sin GPU cae a CPU y tarda mucho.
- **Usa siempre `py`, no `python`:** el lanzador `py` elige el Python instalado
  aunque otro programa (p. ej. Inkscape) meta su propio `python.exe` en el PATH
  del sistema, que no maneja bien las rutas de red del NAS.

## Comando: "nueva computadora"
Cuando yo escriba **"nueva computadora"**, o al arrancar en un equipo donde el
repo se acaba de clonar (falta RUTAS.txt), guíame por este checklist:

1. Clonar el repo (si no está clonado ya): `git clone <URL-DEL-REPO>`
2. Confirmar que la carpeta de videos (Escritorio/NAS) existe y ya sincronizó en esta máquina.
3. Completar RUTAS.txt de esta máquina: `py validar_rutas.py` (o doble clic
   en `validar_rutas.bat`) — abre selector gráfico para cada ruta faltante.
4. Verificar con "revisar rutas" (Test-Path de cada clave).
5. Prueba: `py jellyfin_subs.py` (simulación) para confirmar que el equipo funciona.

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
