---
description: Diseña el tema CSS de Jellyfin juntando lo que te gusta de varios repos/mods
argument-hint: [repo o URL + lo que te gusta de él (opcional)]
---
Proceso "tema jellyfin": vamos a armar un solo CSS personalizado para Jellyfin
juntando partes de varios temas/mods. Trabaja en esta conversación (es un ida y
vuelta conmigo), en español, corto y directo. Petición de ahora: $ARGUMENTS

## Archivos (carpeta `tema\`)
- `GUSTOS.md` — la fuente de verdad: qué me gusta y qué NO de cada repo/mod, y qué
  regla de `custom.css` lo implementa. Léelo siempre al empezar y actualízalo con
  cada decisión mía (también cuando algo no me gustó, para no repetirlo).
- `custom.css` — el tema final que se sube a Jellyfin (va en git).
- `actual.css` — lo que tiene el servidor ahora (`--leer`). `fuentes\` — CSS de
  los repos bajados (`--fuentes`). `respaldos\` — copia automática antes de cada
  `--aplicar`. Estos tres están en `.gitignore`.

## Herramienta: `py tema_jellyfin.py`
`--leer` · `--fuentes [URL ...]` (sin URL baja los `@import` del servidor, y
recursivamente los que tengan dentro) · `--aplicar [archivo]` (por defecto
`tema\custom.css`; respalda antes y verifica que se guardó) · `--restaurar [archivo]`.

## Pasos
1. Al empezar: `--leer` y lee `GUSTOS.md`. Si `custom.css` no existe, créalo a
   partir de `actual.css`.
2. Cuando te pase un repo o mod: lee su README (WebFetch) para encontrar el CSS
   real (normalmente un `@import` de jsDelivr/GitHub raw) y sus opciones/addons;
   bájalo con `--fuentes <URL>`. Ubica en `tema\fuentes\` las reglas que hacen lo
   que describí (busca por selector: `.cardBox`, `.skinHeader`, `.itemDetailPage`,
   `.backdropContainer`, `.headerTabs`, `.osdControls`…). Si no queda claro qué
   parte es, pregúntame con opciones concretas.
3. Junta en `custom.css` con este orden:
   1. como máximo **un** tema base completo por `@import` (todos los `@import` van
      al inicio del archivo, si no el navegador los ignora);
   2. addons/opciones pequeñas por `@import` si el repo las ofrece;
   3. las piezas copiadas de otros repos, cada bloque con un comentario
      `/* De <repo> (<URL>): <qué hace> */` — copia solo las reglas necesarias,
      no temas enteros (dos temas completos se pisan entre sí);
   4. mis ajustes personales al final (ganan por cascada).
   Usa variables en `:root` para colores/radios compartidos, así un cambio de color
   aplica a todo. Si dos piezas chocan (mismo selector), dime cuál gana y por qué.
4. Aplica con `--aplicar` sin pedirme luz verde (siempre hay respaldo) y dime que
   recargue con **Ctrl+F5**. Resume en 2-3 líneas qué cambió y qué debería notar.
5. Si digo que algo quedó mal: corrige, o `--restaurar` si pido volver atrás.
6. Cada vez que el tema quede bien: commit de `custom.css` y `GUSTOS.md`.

## Cuidado
- Jellyfin tiene web, app de TV y apps móviles; el CSS personalizado solo afecta
  la web (y las apps que la usan, como Jellyfin Media Player / Android TV web).
  Avísame si algo que pido no se puede con CSS (p. ej. cambiar textos o agregar
  botones → necesita un plugin o JavaScript, que es otro tema).
- Prefiere versiones fijas (`@v1.2.3` o un commit) a `@main`/`@latest` en los
  `@import`: si el autor cambia el repo, mi tema no se rompe solo. Si fijas una,
  avísame.
- Si te pido que lo veas tú, usa Claude in Chrome sobre `JELLYFIN_URL` para sacar
  una captura (sin iniciar sesión por mí: si pide login, avísame).
