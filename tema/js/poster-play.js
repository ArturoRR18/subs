// Tema Jellyfin: boton Play sobre el poster de la ficha (pelicula, serie, capitulo).
// Se sube con `py tema_jellyfin.py --js` al plugin JavaScript Injector.
// Al hacer clic en el poster aprieta el mismo boton "Play/Reanudar" de la ficha,
// asi hace exactamente lo que haria ese boton (reanudar, siguiente capitulo, etc.).
// Trae sus propios estilos: se ve igual con cualquier tema CSS.
(function () {
  'use strict';
  if (window.__tjPosterPlay) return;
  window.__tjPosterPlay = true;

  var CSS = [
    '.itemDetailPage .detailImageContainer .tjPosterPlay {',
    '  position: absolute; inset: 0; z-index: 3; display: none;',
    '  align-items: center; justify-content: center; pointer-events: none;',
    '  background: radial-gradient(circle at center, rgba(0,0,0,.35), rgba(0,0,0,0) 60%);',
    '  opacity: 0; transition: opacity .25s ease;',
    '}',
    '.itemDetailPage .detailImageContainer .card.tjJugable { cursor: pointer; }',
    '.itemDetailPage .detailImageContainer .card.tjJugable .tjPosterPlay { display: flex; }',
    '.itemDetailPage .detailImageContainer .tjPosterPlay .material-icons {',
    '  font-size: 3.2em; width: 1.6em; height: 1.6em; display: flex; align-items: center; justify-content: center;',
    '  border-radius: 50%; color: #000; background: rgba(255,255,255,.92);',
    '  box-shadow: 0 10px 30px rgba(0,0,0,.55); transition: transform .25s cubic-bezier(.2,.8,.2,1);',
    '}',
    '.itemDetailPage .detailImageContainer .card.tjJugable:hover .tjPosterPlay { opacity: 1; }',
    '.itemDetailPage .detailImageContainer .card.tjJugable:hover .tjPosterPlay .material-icons { transform: scale(1.12); }'
  ].join('\n');
  var st = document.createElement('style');
  st.id = 'tjPosterPlayCss';
  st.textContent = CSS;
  document.head.appendChild(st);

  function pagina() { return document.querySelector('.itemDetailPage:not(.hide)'); }

  function botonPlay(p) {
    var b = p && p.querySelector('.mainDetailButtons .btnPlay:not(.hide)');
    return b && b.offsetParent !== null ? b : null;
  }

  function preparar() {
    var p = pagina();
    if (!p) return;
    var card = p.querySelector('.detailImageContainer .card');
    if (!card) return;
    var esc = card.querySelector('.cardScalable');
    if (esc && !esc.querySelector('.tjPosterPlay')) {
      var d = document.createElement('div');
      d.className = 'tjPosterPlay';
      d.setAttribute('aria-hidden', 'true');
      d.innerHTML = '<span class="material-icons play_arrow"></span>';
      esc.appendChild(d);
    }
    card.classList.toggle('tjJugable', !!botonPlay(p));
  }

  document.addEventListener('click', function (e) {
    var card = e.target.closest && e.target.closest('.itemDetailPage:not(.hide) .detailImageContainer .card');
    if (!card) return;
    var b = botonPlay(pagina());
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    b.click();
  }, true);

  var pendiente = false;
  new MutationObserver(function () {
    if (pendiente) return;
    pendiente = true;
    requestAnimationFrame(function () { pendiente = false; preparar(); });
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  preparar();
})();
