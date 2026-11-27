# Gustos del tema de Jellyfin

Fuente de verdad del proceso "tema jellyfin": qué me gusta y qué no de cada
repo/mod, y dónde quedó en `custom.css`.

## Base actual
- **jellyfin-nebula** (`nemesbak/jellyfin-nebula@main/nebula.css`) — tema base.
  Opciones: `cabecera-minima`, `titulo-en-texto`, `tarjetas-texto-centrado`, `acento-blanco`.
- (Antes: JellyFlix estilo Netflix + ajustes propios; reemplazado por Nebula.)

## Me gusta
- De Nebula: cómo se ven los menús → se conserva tal cual (`@import`).
- De Nebula: la ficha de película/serie (detalle) → se conserva tal cual.

## No me gusta
- De Nebula: el póster en la ficha sale centrado y tapa la info (título, sinopsis,
  datos). Causa: `.layout-desktop .detailImageContainer { position: relative }`
  ("v4 fixes"). Arreglo en `custom.css`: `position: static` → póster a la izquierda
  como en Jellyfin original. (2026-11-26)

- De Nebula: al entrar a la ficha el póster aparecía en medio y luego saltaba a la
  izquierda. Causa: la animación `nbRise` (transform) en `.detailImageContainer`.
  Arreglo: la misma animación pasa a `.detailImageContainer .cardBox`. (2026-11-26)

## Pendiente
- Confirmar que el póster ya entra directo a la izquierda.
