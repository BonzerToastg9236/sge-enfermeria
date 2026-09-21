/*
 * Pantalla de configuración de recargos: ayuda por tipo, límites del campo y SIMULADOR en vivo.
 * Los números vienen del servidor (misma fórmula que el cálculo real) y se pintan con textContent.
 */
(function () {
  const form = document.getElementById('formRecargos');
  if (!form) return;

  const tipo = document.getElementById('tipoRecargo');
  const valor = document.getElementById('valorRecargo');
  const gracia = document.getElementById('diasGracia');
  const monto = document.getElementById('montoEjemplo');
  const ayuda = document.getElementById('ayudaValor');
  const etiqueta = document.getElementById('labelValor');
  const tabla = document.getElementById('tablaSimulador');
  const errorBox = document.getElementById('errorSimulador');

  const TIPOS = {
    MONTO_FIJO: { max: 99999999.99, etiqueta: 'Monto ($)', ayuda: 'Monto en pesos que se cobra UNA sola vez al cargo vencido (pasada la gracia).' },
    PORCENTAJE: { max: 100, etiqueta: 'Porcentaje (%)', ayuda: 'Porcentaje (ej. 5 = 5%) del MONTO ORIGINAL del cargo, una sola vez. Máximo 100.' },
    POR_DIA: { max: 10000, etiqueta: 'Monto por día ($)', ayuda: 'Pesos que se multiplican por cada día de atraso después de la gracia (crece cada día). Máximo 10,000 por día.' },
    PORCENTAJE_MENSUAL: { max: 100, etiqueta: 'Porcentaje por mes (%)', ayuda: 'Porcentaje del monto original que se suma por cada mes (bloque de 30 días) de atraso: mes 1 = 1×, mes 2 = 2×... Máximo 100.' },
  };

  function actualizarTipo() {
    const t = TIPOS[tipo.value];
    if (!t) return;
    ayuda.textContent = t.ayuda;
    etiqueta.textContent = t.etiqueta;
    valor.max = String(t.max);
  }

  function pesos(n) { return Number(n).toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

  function celda(fila, texto, clase) {
    const td = document.createElement('td');
    td.textContent = texto;
    if (clase) td.className = clase;
    fila.appendChild(td);
  }

  function pintar(datos) {
    const cuerpo = tabla.querySelector('tbody');
    cuerpo.replaceChildren();
    datos.ejemplos.forEach((e) => {
      const fila = document.createElement('tr');
      celda(fila, e.dias + (e.dias === 1 ? ' día' : ' días'));
      celda(fila, '$' + pesos(e.recargo), 'text-end ' + (Number(e.recargo) > 0 ? 'fw-semibold' : 'text-muted'));
      celda(fila, '$' + pesos(e.total), 'text-end');
      cuerpo.appendChild(fila);
    });
    const i = datos.impacto;
    const poner = (id, texto) => { document.getElementById(id).textContent = texto; };
    poner('impVencidos', i.vencidos); poner('impSuben', i.suben); poner('impBajan', i.bajan); poner('impIguales', i.iguales);
    poner('impActual', pesos(i.total_actual)); poner('impRecalculado', pesos(i.total_recalculado)); poner('impSinRecalcular', pesos(i.total_sin_recalcular));
  }

  let temporizador = null;
  function simular() {
    clearTimeout(temporizador);
    temporizador = setTimeout(async () => {
      const params = new URLSearchParams({ tipo_recargo: tipo.value, valor_recargo: valor.value, dias_gracia: gracia.value || '0', monto: monto.value || '2500' });
      try {
        const r = await fetch(form.dataset.urlSimular + '?' + params.toString(), { headers: { Accept: 'application/json' } });
        const datos = await r.json();
        if (!r.ok) { errorBox.textContent = datos.error || 'Revisa los valores.'; errorBox.classList.remove('d-none'); tabla.classList.add('opacity-25'); return; }
        errorBox.classList.add('d-none');
        tabla.classList.remove('opacity-25');
        pintar(datos);
      } catch (e) {
        errorBox.textContent = 'No se pudo calcular la simulación.'; errorBox.classList.remove('d-none'); tabla.classList.add('opacity-25');
      }
    }, 250);
  }

  tipo.addEventListener('change', () => { actualizarTipo(); simular(); });
  [valor, gracia, monto].forEach((el) => el.addEventListener('input', simular));
  actualizarTipo();
  simular();
})();
