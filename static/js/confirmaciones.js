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
