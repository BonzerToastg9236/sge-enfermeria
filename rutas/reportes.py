"""Reportes de cobros: corte del día, cartera vencida y dashboard, cada uno con su exportación a Excel."""

import io
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill

from flask import Blueprint, render_template, request, flash, send_file

from utilidades.seguridad import rol_requerido
from utilidades.fechas import hoy_local, ahora_utc, a_local
from utilidades.archivos import valor_seguro_excel
from servicios.terminologia import terminos


def _titulo(texto):
    """Primera fila de cada Excel: nombre de la institución + título del reporte (sin fórmulas vivas)."""
    return valor_seguro_excel(f'{terminos().institucion} — {texto}')
from utilidades.paginacion import _paginar_lista, CARGOS_POR_PAGINA, pagina_valida
from servicios.reportes import (
    _calcular_reporte_cobros_del_dia, _calcular_cartera_vencida,
    _calcular_dashboard_cobros,
)

reportes_bp = Blueprint('reportes', __name__)


@reportes_bp.route('/reportes/cobros-del-dia')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def reporte_cobros_del_dia():
    """
    Reporte a demanda de todos los pagos capturados en una fecha (por
    defecto, hoy). Cualquiera de los dos roles puede consultarlo —
    es información, no una acción de modificación.
    """
    fecha_raw = request.args.get('fecha', '')
    try:
        fecha_reporte = datetime.strptime(fecha_raw, '%Y-%m-%d').date() if fecha_raw else hoy_local()
    except ValueError:
        fecha_reporte = hoy_local()
        flash('La fecha indicada no era válida; se muestra el día de hoy.', 'warning')

    pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo = _calcular_reporte_cobros_del_dia(fecha_reporte)

    return render_template(
        'reporte_cobros_dia.html',
        fecha_reporte=fecha_reporte,
        pagos_del_dia=pagos_del_dia,
        total_del_dia=total_del_dia,
        totales_por_concepto=totales_por_concepto,
        totales_por_metodo=totales_por_metodo,
    )


@reportes_bp.route('/reportes/cobros-del-dia/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_reporte_cobros_del_dia():
    fecha_raw = request.args.get('fecha', '')
    try:
        fecha_reporte = datetime.strptime(fecha_raw, '%Y-%m-%d').date() if fecha_raw else hoy_local()
    except ValueError:
        fecha_reporte = hoy_local()

    pagos_del_dia, total_del_dia, totales_por_concepto, totales_por_metodo = _calcular_reporte_cobros_del_dia(fecha_reporte)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Cobros del Día'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')

    ws.append([_titulo(f'Reporte de Cobros del Día - {fecha_reporte.strftime("%d/%m/%Y")}')])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Folio', 'Hora', 'Alumno', 'Matrícula', 'Concepto', 'Método', 'Monto', 'Estatus'])
    for celda in ws[3]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado

    for pago in pagos_del_dia:
        # Los pagos anulados se listan (transparencia) pero marcados: el TOTAL
        # solo suma los VIGENTE, y sin esta columna las filas no cuadraban.
        ws.append([
            valor_seguro_excel(pago.folio or f'#{pago.id}'),
            a_local(pago.fecha_pago).strftime('%H:%M'),  # hora de la caja, no UTC
            valor_seguro_excel(pago.cargo.alumno.nombre_completo if pago.cargo and pago.cargo.alumno else '—'),
            valor_seguro_excel(pago.cargo.matricula_fk if pago.cargo else '—'),
            valor_seguro_excel(pago.cargo.concepto if pago.cargo else '—'),
            pago.metodo_pago.value,
            float(pago.monto_pagado),
            'ANULADO' if pago.anulado else 'VIGENTE',
        ])

    ws.append([])
    fila_total = ws.max_row + 1
    ws.cell(row=fila_total, column=6, value='TOTAL:').font = Font(bold=True)
    ws.cell(row=fila_total, column=7, value=float(total_del_dia)).font = Font(bold=True)

    for col in 'ABCDEFGH':
        ws.column_dimensions[col].width = 20

    ws_concepto = wb.create_sheet('Por Concepto')
    ws_concepto.append(['Concepto', 'Total'])
    for celda in ws_concepto[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for concepto, total in totales_por_concepto.items():
        ws_concepto.append([valor_seguro_excel(concepto), float(total)])
    ws_concepto.column_dimensions['A'].width = 30
    ws_concepto.column_dimensions['B'].width = 15

    ws_metodo = wb.create_sheet('Por Método de Pago')
    ws_metodo.append(['Método', 'Total'])
    for celda in ws_metodo[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for metodo, total in totales_por_metodo.items():
        ws_metodo.append([valor_seguro_excel(metodo), float(total)])
    ws_metodo.column_dimensions['A'].width = 25
    ws_metodo.column_dimensions['B'].width = 15

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'cobros_del_dia_{fecha_reporte.isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@reportes_bp.route('/reportes/cartera-vencida')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def cartera_vencida():
    """
    Lista de cargos vencidos (pendientes o parciales, con fecha de
    vencimiento ya pasada), ordenados por saldo pendiente de mayor a
    menor -- para priorizar a quién darle seguimiento de cobranza primero.
    Recalcula el recargo de cada uno antes de mostrar, igual que /cobros.
    """
    filas, total_vencido = _calcular_cartera_vencida()

    # La lista se pagina en Python (no en SQL) porque el orden es por saldo
    # pendiente, que es un valor calculado, no una columna. total_vencido y
    # total_filas siguen siendo los GLOBALES: las tarjetas de resumen deben
    # mostrar la cartera completa, no solo lo que se ve en esta página.
    page = pagina_valida(request.args.get('page', 1, type=int))
    filas_pagina, paginacion = _paginar_lista(filas, page, CARGOS_POR_PAGINA)

    return render_template(
        'cartera_vencida.html',
        filas=filas_pagina,
        total_vencido=total_vencido,
        total_filas=len(filas),
        paginacion=paginacion
    )


@reportes_bp.route('/reportes/cartera-vencida/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_cartera_vencida():
    filas, total_vencido = _calcular_cartera_vencida()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Cartera Vencida'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='DC3545', end_color='DC3545', fill_type='solid')

    ws.append([_titulo(f'Cartera Vencida - Generado {a_local(ahora_utc()).strftime("%d/%m/%Y %H:%M")}')])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Alumno', 'Matrícula', 'Concepto', 'Periodo', 'Días de Atraso', 'Saldo Pendiente'])
    for celda in ws[3]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado

    for fila in filas:
        cargo = fila['cargo']
        ws.append([
            valor_seguro_excel(cargo.alumno.nombre_completo),
            valor_seguro_excel(cargo.matricula_fk),
            valor_seguro_excel(cargo.concepto),
            valor_seguro_excel(cargo.periodo_escolar or '—'),
            fila['dias_atraso'],
            float(fila['saldo']),
        ])

    ws.append([])
    fila_total = ws.max_row + 1
    ws.cell(row=fila_total, column=5, value='TOTAL VENCIDO:').font = Font(bold=True)
    ws.cell(row=fila_total, column=6, value=float(total_vencido)).font = Font(bold=True)

    for col, ancho in zip('ABCDEF', [30, 15, 25, 15, 15, 18]):
        ws.column_dimensions[col].width = ancho

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'cartera_vencida_{ahora_utc().date().isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@reportes_bp.route('/cobros/dashboard')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def dashboard_cobros():
    datos = _calcular_dashboard_cobros()
    return render_template('dashboard_cobros.html', **datos)


@reportes_bp.route('/cobros/dashboard/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_dashboard_cobros():
    datos = _calcular_dashboard_cobros()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Resumen'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_encabezado = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')

    ws.append([_titulo(f'Dashboard de Cobros - Generado {a_local(ahora_utc()).strftime("%d/%m/%Y %H:%M")}')])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(['Total cobrado este mes', float(datos['total_cobrado_mes_actual'])])
    ws.append(['Total cobrado (últimos 6 meses)', float(datos['total_cobrado_6_meses'])])
    ws.append(['Total por cobrar (saldo abierto)', float(datos['total_por_cobrar'])])
    ws.append(['Total vencido', float(datos['total_vencido'])])
    ws.column_dimensions['A'].width = 32
    ws.column_dimensions['B'].width = 18

    ws_mensual = wb.create_sheet('Ingresos Mensuales')
    ws_mensual.append(['Mes', 'Total Cobrado'])
    for celda in ws_mensual[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for item in datos['ingresos_mensuales']:
        ws_mensual.append([item['etiqueta'], float(item['total'])])
    ws_mensual.column_dimensions['A'].width = 18
    ws_mensual.column_dimensions['B'].width = 18

    ws_concepto = wb.create_sheet('Ingresos por Concepto')
    ws_concepto.append(['Concepto', 'Total (últimos 6 meses)'])
    for celda in ws_concepto[1]:
        celda.font = fuente_encabezado
        celda.fill = relleno_encabezado
    for item in datos['ingresos_por_concepto']:
        ws_concepto.append([valor_seguro_excel(item['concepto']), float(item['total'])])
    ws_concepto.column_dimensions['A'].width = 30
    ws_concepto.column_dimensions['B'].width = 20

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'dashboard_cobros_{ahora_utc().date().isoformat()}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
