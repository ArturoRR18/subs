// Tema Jellyfin: boton Play sobre el poster de la ficha (pelicula, serie, capitulo).
// Se sube con `py tema_jellyfin.py --js` al plugin JavaScript Injector.
// Al hacer clic en el poster aprieta el mismo boton "Play/Reanudar" de la ficha,
// asi hace exactamente lo que haria ese boton (reanudar, siguiente capitulo, etc.).
// Los estilos del boton (.tjPosterPlay) estan en tema/custom.css.
(function () {
  'use strict';
  if (window.__tjPosterPlay) return;
  window.__tjPosterPlay = true;

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
