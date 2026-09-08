"""Agregaciones para los reportes de cobros: corte del día, cartera vencida, dashboard."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from extensiones import db
from modelos import Cargo, EstatusCargo, Pago, ConfiguracionCobros
from utilidades.fechas import hoy_local, rango_utc_del_dia
from servicios.cobros import MESES_ES


def _calcular_reporte_cobros_del_dia(fecha_reporte):
    """
    Extraída de reporte_cobros_del_dia() para que la vista en pantalla y
    la exportación a Excel usen SIEMPRE el mismo cálculo -- nunca se
    desincronizan entre sí.

    fecha_reporte es un día CALENDARIO LOCAL (el que vivió la caja); como
    Pago.fecha_pago se guarda en UTC, el rango de búsqueda tiene que
    convertirse a UTC con rango_utc_del_dia() -- comparar contra
    medianoche-a-medianoche naive (como si fecha_reporte ya fuera UTC)
    hacía que un pago cobrado por la noche apareciera en el corte del
    día siguiente.
    """
    inicio_dia, fin_dia = rango_utc_del_dia(fecha_reporte)

    pagos_del_dia = (
        Pago.query
        .filter(Pago.fecha_pago >= inicio_dia, Pago.fecha_pago <= fin_dia)
        .order_by(Pago.fecha_pago.asc())
        .all()
    )

    # Los pagos anulados SÍ se muestran en la lista (transparencia total del
    # día), pero NO cuentan en los totales -- ese dinero no se quedó cobrado.
    pagos_vigentes = [p for p in pagos_del_dia if not p.anulado]
    total_del_dia = sum((p.monto_pagado for p in pagos_vigentes), Decimal('0.00'))

    totales_por_concepto = {}
    totales_por_metodo = {}
    for pago in pagos_vigentes:
        concepto = pago.cargo.concepto if pago.cargo else 'Sin concepto'
        totales_por_concepto[concepto] = totales_por_concepto.get(concepto, Decimal('0.00')) + pago.monto_pagado
        totales_por_metodo[pago.metodo_pago.value] = totales_por_metodo.get(pago.metodo_pago.value, Decimal('0.00')) + pago.monto_pagado

    return pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo


def _calcular_cartera_vencida():
    """
    Extraída de cartera_vencida() para reutilizar en la exportación a Excel.

    PERFORMANCE-NOTE: la versión anterior hacía Cargo.query...all() y luego,
    por cada cargo candidato, llamaba esta_vencido() / actualizar_recargo_si_vencido()
    / saldo_pendiente() -- cada una de esas dispara cargo.pagos (lazy='select'
    en la relación Cargo.pagos, ver clase Pago), y actualizar_recargo_si_vencido()
    además llamaba ConfiguracionCobros.obtener() en CADA iteración, repitiendo
    la misma consulta de una tabla de una sola fila. Con miles de cargos
    vencidos (justo el escenario de este reporte: "cartera vencida" tiende a
    crecer con el tiempo), esto son miles de consultas extra en una sola
    carga de página.

    Esta versión:
      1. Trae la config UNA sola vez, antes del loop.
      2. Calcula el total pagado de todos los cargos candidatos con UNA
         agregación SQL (mismo patrón que _matriculas_con_adeudo), en vez
         de que cada cargo dispare su propia consulta a Pago.
    """
    hoy = hoy_local()
    config = ConfiguracionCobros.obtener()  # una sola vez, no una vez por cargo

    candidatos = (
        Cargo.query
        .filter(Cargo.estatus.in_([EstatusCargo.PENDIENTE, EstatusCargo.PARCIAL]))
        .filter(Cargo.fecha_vencimiento.isnot(None))
        .filter(Cargo.fecha_vencimiento < hoy)  # ya filtra "vencido" en SQL, no solo en Python
        .all()
    )

    if not candidatos:
        return [], Decimal('0.00')

    cargo_ids = [c.id for c in candidatos]
    pagos_por_cargo = dict(
        db.session.query(
            Pago.cargo_fk,
            func.coalesce(func.sum(Pago.monto_pagado), 0)
        )
        .filter(Pago.cargo_fk.in_(cargo_ids))
        .filter(Pago.anulado.is_(False))
        .group_by(Pago.cargo_fk)
        .all()
    )

    filas = []
    for cargo in candidatos:
        # esta_vencido() ya no hace falta re-evaluarlo: el filtro SQL de
        # arriba (fecha_vencimiento < hoy) más el filtro de estatus ya
        # garantizan que todo lo que llega aquí está vencido.
        cargo.actualizar_recargo_si_vencido(config=config)
        total_pagado = pagos_por_cargo.get(cargo.id, Decimal('0.00'))
        saldo = (cargo.monto + cargo.recargo_aplicado) - total_pagado
        filas.append({
            'cargo': cargo,
            'dias_atraso': (hoy - cargo.fecha_vencimiento).days,
            'saldo': saldo,
        })
    db.session.commit()  # persiste cualquier recargo que se haya actualizado arriba

    filas.sort(key=lambda f: f['saldo'], reverse=True)
    total_vencido = sum((f['saldo'] for f in filas), Decimal('0.00'))

    return filas, total_vencido


def _rango_ultimos_n_meses(n=6):
    """Lista de diccionarios describiendo cada uno de los últimos n meses (incluye el actual)."""
    hoy = hoy_local()
    rangos = []
    for i in range(n - 1, -1, -1):
        mes_ref = hoy.month - i
        anio_ref = hoy.year
        while mes_ref <= 0:
            mes_ref += 12
            anio_ref -= 1
        inicio = date(anio_ref, mes_ref, 1)
        fin = (date(anio_ref + 1, 1, 1) if mes_ref == 12 else date(anio_ref, mes_ref + 1, 1)) - timedelta(days=1)
        rangos.append({
            'anio': anio_ref,
            'mes': mes_ref,
            'inicio': datetime.combine(inicio, datetime.min.time()),
            'fin': datetime.combine(fin, datetime.max.time()),
            'etiqueta': f'{MESES_ES[mes_ref]} {anio_ref}',
        })
    return rangos


def _calcular_dashboard_cobros():
    """Extraída para reutilizar entre la vista del dashboard y su exportación a Excel."""
    rangos = _rango_ultimos_n_meses(6)

    ingresos_mensuales = []
    for r in rangos:
        total = db.session.query(func.sum(Pago.monto_pagado)).filter(
            Pago.fecha_pago >= r['inicio'], Pago.fecha_pago <= r['fin'], Pago.anulado.is_(False)
        ).scalar() or Decimal('0.00')
        ingresos_mensuales.append({'etiqueta': r['etiqueta'], 'total': total})

    inicio_periodo = rangos[0]['inicio']
    fin_periodo = rangos[-1]['fin']

    ingresos_por_concepto_raw = (
        db.session.query(Cargo.concepto, func.sum(Pago.monto_pagado))
        .join(Pago, Pago.cargo_fk == Cargo.id)
        .filter(Pago.fecha_pago >= inicio_periodo, Pago.fecha_pago <= fin_periodo, Pago.anulado.is_(False))
        .group_by(Cargo.concepto)
        .order_by(func.sum(Pago.monto_pagado).desc())
        .all()
    )
    ingresos_por_concepto = [{'concepto': c, 'total': t} for c, t in ingresos_por_concepto_raw]

    total_cobrado_6_meses = sum((item['total'] for item in ingresos_mensuales), Decimal('0.00'))
    total_cobrado_mes_actual = ingresos_mensuales[-1]['total'] if ingresos_mensuales else Decimal('0.00')

    # Reutiliza el mismo recálculo que Cobros y Cartera Vencida -- nunca
    # se confía en un recargo_aplicado guardado sin refrescar primero.
    #
    # PERFORMANCE-NOTE: mismo patrón N+1 que tenía _calcular_cartera_vencida
    # (ver esa función) -- este dashboard es la pantalla que más seguido se
    # va a abrir, así que se arregla igual: config cargada una sola vez, y
    # el saldo de todos los cargos abiertos calculado con UNA agregación
    # SQL en vez de que cada cargo dispare su propia consulta a Pago.
    hoy = hoy_local()
    config = ConfiguracionCobros.obtener()
    cargos_abiertos = Cargo.query.filter(Cargo.estatus.in_([EstatusCargo.PENDIENTE, EstatusCargo.PARCIAL])).all()
    hubo_cambios = False
    for cargo in cargos_abiertos:
        antes = cargo.recargo_aplicado
        cargo.actualizar_recargo_si_vencido(config=config)
        if cargo.recargo_aplicado != antes:
            hubo_cambios = True
    if hubo_cambios:
        db.session.commit()

    if cargos_abiertos:
        cargo_ids = [c.id for c in cargos_abiertos]
        pagos_por_cargo = dict(
            db.session.query(
                Pago.cargo_fk,
                func.coalesce(func.sum(Pago.monto_pagado), 0)
            )
            .filter(Pago.cargo_fk.in_(cargo_ids))
            .filter(Pago.anulado.is_(False))
            .group_by(Pago.cargo_fk)
            .all()
        )
    else:
        pagos_por_cargo = {}

    total_por_cobrar = Decimal('0.00')
    total_vencido = Decimal('0.00')
    for cargo in cargos_abiertos:
        saldo = (cargo.monto + cargo.recargo_aplicado) - pagos_por_cargo.get(cargo.id, Decimal('0.00'))
        total_por_cobrar += saldo
        if cargo.fecha_vencimiento is not None and cargo.fecha_vencimiento < hoy:
            total_vencido += saldo

    return {
        'ingresos_mensuales': ingresos_mensuales,
        'ingresos_por_concepto': ingresos_por_concepto,
        'total_cobrado_6_meses': total_cobrado_6_meses,
        'total_cobrado_mes_actual': total_cobrado_mes_actual,
        'total_por_cobrar': total_por_cobrar,
        'total_vencido': total_vencido,
    }
