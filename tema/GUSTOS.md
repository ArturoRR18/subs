# Gustos del tema de Jellyfin

Fuente de verdad del proceso "tema jellyfin": qué me gusta y qué no de cada
repo/mod, y dónde quedó en `custom.css`.

## Base actual
- **jellyfin-nebula** (`nemesbak/jellyfin-nebula@main/nebula.css`) — tema base.
  Opciones: `cabecera-minima`, `titulo-en-texto`, `tarjetas-texto-centrado`, `acento-blanco`.
- (Antes: JellyFlix estilo Netflix + ajustes propios; reemplazado por Nebula.)

## Versiones aprobadas
- **tema-v1** (git tag, 2026-11-26): Nebula + póster a la izquierda sin salto +
  logos + pop-up + mezcla Media Bar angosta. Es el "último tema" que se recarga con
  `py tema_jellyfin.py --aplicar`.
- **tema-v2** (git tag, 2026-11-26): v1 + sin franja oscura bajo el título + botón
  ▶ en el póster (solo al pasar el cursor). Es el "último tema": `--aplicar` (CSS)
  y `--js` (botón). El botón trae su propio estilo y sigue activo aunque pruebes
  otro CSS; para quitarlo, desactiva `tema/poster-play.js` en JavaScript Injector.

## Me gusta
- De Nebula: cómo se ven los menús → se conserva tal cual (`@import`).
- De Nebula: la ficha de película/serie (detalle) → se conserva tal cual.

- De JellyFlix: títulos como logo distinto por película → Nebula ya lo hace; se
  quitó la opción `titulo-en-texto` (que lo cambiaba por texto). En el inicio lo
  pone el plugin Media Bar. (2026-11-26)
- De JellyFlix: "pop-up" al pasar el cursor → `.card:hover .cardBox { scale(1.15) }`
  + espacio en las filas para que no se corte. (2026-11-26)
- Carrusel del inicio (imagen fija → a los 3.5 s la imagen se va a la izquierda y
  sale el tráiler): es el **plugin Media Bar** (`trailerStartDelayMs: 3500`), no el
  CSS. Funciona igual con Nebula (probado en vivo). Si no sale el tráiler: esa
  película no tiene tráiler, o el navegador bloqueó la reproducción automática.
- Media Bar: la mezcla imagen/tráiler debe ser angosta, alrededor del 24–34 % del
  ancho (la imagen sólida casi hasta el texto). Original mezclaba del 10 al 34 %.
  → `--slideshow-video-side-fade` y máscara de `.backdrop.with-video`. (2026-11-26)

- Botón ▶ sobre el póster de la ficha (película, serie, capítulo): clic = el botón
  Play/Reanudar de la ficha. Necesita JavaScript → plugin **JavaScript Injector**
  3.5 (n00bcodr, instalado 2026-11-26) + `tema/js/poster-play.js` (se sube con
  `py tema_jellyfin.py --js`); estilos `.tjPosterPlay` en `custom.css`.

- De Abyss: pestañas del encabezado (Inicio/Favoritos) en cápsula de vidrio con la
  activa como píldora blanca → `.headerTabs .emby-tab-button*`. (2026-11-26)
- De Abyss: botón principal de la ficha como píldora con texto "Play" o "Resume"
  (Resume si se ve `.btnReplay`, o sea ya empezada) → `.btnPlay ...::after`. (2026-11-26)

## No me gusta
- De Nebula: el póster en la ficha sale centrado y tapa la info (título, sinopsis,
  datos). Causa: `.layout-desktop .detailImageContainer { position: relative }`
  ("v4 fixes"). Arreglo en `custom.css`: `position: static` → póster a la izquierda
  como en Jellyfin original. (2026-11-26)

- De Nebula: al entrar a la ficha el póster aparecía en medio y luego saltaba a la
  izquierda. Causa: en esta versión de Jellyfin el póster está dentro de
  `.infoWrapper`, que Nebula anima con `nbRise` (transform) → durante ~0.4 s el
  póster se ubica respecto a esa caja (x=654) y luego salta (x=63). Arreglo:
  `.infoWrapper` solo se desvanece (`nbSoloFade`) y el "subir" pasa a sus hijos
  menos el póster; el póster sube con `.cardBox`. Verificado en vivo: x=63 desde
  el primer instante. (2026-11-26)

- De Nebula: franja oscura bajo el título en la ficha (degradado de `.detailRibbon`
  que termina en negro). Arreglo: fondo transparente en la barra. (2026-11-26)

## Notas para probar
- Al medir en el navegador, recarga la página de verdad (no solo cambiar el `#`):
  si no, sigue con el CSS viejo.
