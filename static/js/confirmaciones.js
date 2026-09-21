/*
 * Confirmaciones de formularios que antes armaban su mensaje directo en
 * onsubmit="return confirm('...{{ variable }}...')" -- vulnerable a XSS
 * porque el navegador decodifica las entidades HTML del atributo ANTES
 * de tratar su contenido como código JavaScript. Aquí el valor llega en
 * un data-*, que solo se lee como texto vía dataset, nunca como código.
 */

document.querySelectorAll('.js-confirm-avance').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    const d = form.dataset;
    const msg = `¿Avanzar a ${d.alumno} del ${d.de}° al ${d.a}° ${d.periodo}? ` +
                'Se generará su reinscripción, mensualidades y carga académica nueva.';
    if (!confirm(msg)) evento.preventDefault();
  });
});

document.querySelectorAll('.js-confirm-toggle-usuario').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    const d = form.dataset;
    const msg = `¿${d.accion} la cuenta de ${d.usuario}?`;
    if (!confirm(msg)) evento.preventDefault();
  });
});

document.querySelectorAll('.js-confirm-eliminar-materia').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    const d = form.dataset;
    const msg = `¿Eliminar '${d.materia}'? Solo funciona si nunca se ha usado en boletas.`;
    if (!confirm(msg)) evento.preventDefault();
  });
});

document.querySelectorAll('.js-confirm-cancelar-cargo').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    const d = form.dataset;
    const msg = `¿Cancelar el cargo '${d.concepto}'? Esta acción no se puede deshacer.`;
    if (!confirm(msg)) evento.preventDefault();
  });
});

// A diferencia de los de arriba, este NO usa data-*: el valor a
// confirmar es lo que el operador ACABA de escribir en su propio
// formulario (periodo_escolar), no un dato ya guardado de otro usuario
// -- se lee directo del campo al momento del submit.
document.querySelectorAll('.js-confirm-beca-anual').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    const periodo = form.elements.periodo_escolar.value.trim();
    if (/^\d{4}$/.test(periodo)) {
      const msg = `Esta beca aplicará a TODOS los periodos de ${periodo}, no solo a uno. ¿Continuar?`;
      if (!confirm(msg)) evento.preventDefault();
    }
  });
});

// Confirmación genérica: el mensaje viaja en data-mensaje (texto, nunca código).
document.querySelectorAll('.js-confirm').forEach((form) => {
  form.addEventListener('submit', (evento) => {
    if (!confirm(form.dataset.mensaje)) evento.preventDefault();
  });
});

// Catálogo de conceptos: el campo de precio se bloquea SOLO mientras "Es la mensualidad" está marcada.
// Se reconcilia al cargar y al volver a la página (pageshow): el navegador puede restaurar la casilla
// sin restaurar el estado "disabled" (o al revés), y el campo se quedaba bloqueado aunque se desmarcara.
function sincronizarPrecioConcepto(form) {
  const casilla = form.querySelector('.js-es-mensualidad');
  const precio = form.querySelector('.js-campo-precio');
  const fila = form.querySelector('.js-fila-aplicar');
  if (!casilla || !precio) return;
  precio.disabled = casilla.checked;
  precio.placeholder = casilla.checked ? precio.dataset.placeholderMensualidad : precio.dataset.placeholderNormal;
  if (fila) fila.hidden = casilla.checked;
}
document.querySelectorAll('.js-precio-concepto').forEach((form) => {
  form.querySelector('.js-es-mensualidad').addEventListener('change', () => sincronizarPrecioConcepto(form));
  sincronizarPrecioConcepto(form);
});
window.addEventListener('pageshow', () => document.querySelectorAll('.js-precio-concepto').forEach(sincronizarPrecioConcepto));
