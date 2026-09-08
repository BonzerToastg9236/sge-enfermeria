"""
Lógica de generación automática de cargos: inscripción, reinscripción,
mensualidades con beca aplicada, y sugerencia de fecha de vencimiento.
Los precios son configuración de cada institución -- ver
deploy/PENDIENTES_PRODUCCION.md §3: si falta un precio, estas funciones
NO generan el cargo y devuelven un aviso, nunca inventan un monto.
"""

from datetime import datetime, date
from decimal import Decimal

from extensiones import db
from modelos import Cargo, EstatusCargo, ConceptoCobro
from utilidades.fechas import hoy_local, periodo_escolar_actual

MESES_ES = ['', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _cargo_duplicado(matricula, concepto_cobro_id, periodo_escolar):
    """
    Busca un cargo NO cancelado ya existente para el mismo alumno, mismo
    concepto del catálogo y mismo periodo escolar. Se usa tanto al crear
    un cargo individual como al generar mensualidades en lote, para no
    cobrarle dos veces el mismo concepto a la misma persona por error.
    """
    return Cargo.query.filter(
        Cargo.matricula_fk == matricula,
        Cargo.concepto_cobro_fk == concepto_cobro_id,
        Cargo.periodo_escolar == periodo_escolar,
        Cargo.estatus != EstatusCargo.CANCELADO,
    ).first()


def _meses_del_cuatrimestre_actual():
    """[(año, mes), ...] de los 4 meses del cuatrimestre EN CURSO (A=Ene-Abr, B=May-Ago, C=Sep-Dic)."""
    hoy = hoy_local()
    if hoy.month <= 4:
        meses = [1, 2, 3, 4]
    elif hoy.month <= 8:
        meses = [5, 6, 7, 8]
    else:
        meses = [9, 10, 11, 12]
    return [(hoy.year, m) for m in meses]


def _monto_mensualidad_con_beca(alumno, periodo_cargo):
    """
    Monto de mensualidad a cobrar para un cargo de un periodo específico
    (ej. "2026-B-May"), aplicando cualquier beca ACTIVA cuyo periodo de
    vigencia sea un prefijo de ese periodo (una beca con
    periodo_escolar="2026-B" aplica a todos los cargos mensuales de ese
    cuatrimestre: "2026-B-May", "2026-B-Jun", etc.). Si el alumno tiene
    más de una beca vigente al mismo tiempo, se usa la de MAYOR
    descuento (nunca se suman varias becas entre sí).
    """
    monto_base = alumno.plan.monto_mensualidad
    # `is None` a propósito, NO `not monto_base`: una mensualidad de $0.00
    # es un precio CONFIGURADO (la institución decidió que esa carrera es
    # gratuita), mientras que NULL significa "todavía no se captura". Ver
    # _generar_cargos_de_periodo().
    if monto_base is None:
        return None

    becas_vigentes = [b for b in alumno.becas if b.activa and periodo_cargo.startswith(b.periodo_escolar)]
    if not becas_vigentes:
        return monto_base

    mejor_beca = max(becas_vigentes, key=lambda b: b.calcular_descuento(monto_base))
    monto_final = monto_base - mejor_beca.calcular_descuento(monto_base)
    return max(monto_final, Decimal('0.00'))


def _generar_cargos_de_periodo(alumno, nombre_concepto_unico):
    """
    Genera: 1 cargo del concepto indicado ("Inscripción" al activarse por
    primera vez, o "Reinscripción" al avanzar de cuatrimestre) + un cargo
    de mensualidad por cada mes del cuatrimestre EN CURSO únicamente (no
    de toda la carrera): los cuatrimestres futuros se generan uno a la
    vez, cuando toque avanzarlos -- así, si el precio de la mensualidad
    cambia, no queda arrastrado un cargo viejo con el precio equivocado.
    El resto de conceptos (Uniformes, Servicio Social, etc.) se siguen
    agregando a mano. Nunca duplica: reutiliza _cargo_duplicado() antes
    de crear cada cargo.

    LOS PRECIOS SON CONFIGURACIÓN DE LA INSTITUCIÓN, y esta función NUNCA
    los inventa. `ConceptoCobro.monto_sugerido` y
    `PlanEstudio.monto_mensualidad` son nullable a propósito: NULL
    significa "esta institución todavía no capturó ese precio", que NO es
    lo mismo que $0.00 (eso sería "es gratis", una decisión que solo
    Dirección puede tomar desde su pantalla de configuración). Si falta el
    precio no se genera el cargo: se devuelve el aviso para que la persona
    sepa exactamente qué configuración falta y dónde capturarla.

    Devuelve (cargos_generados, avisos_de_configuracion).
    """
    periodo_actual = periodo_escolar_actual()
    vencimiento_default = datetime.strptime(_vencimiento_dia_10_sugerido(), '%Y-%m-%d').date()
    cargos_generados = []
    avisos_de_configuracion = []

    concepto_unico = ConceptoCobro.query.filter_by(nombre=nombre_concepto_unico, activo=True).first()
    if concepto_unico and not _cargo_duplicado(alumno.matricula_id, concepto_unico.id, periodo_actual):
        if concepto_unico.monto_sugerido is None:
            avisos_de_configuracion.append(
                f'No se generó el cargo de "{concepto_unico.nombre}": ese concepto todavía '
                'no tiene precio configurado en el catálogo de la institución. Captúralo en '
                'Conceptos de Cobro y genera el cargo desde la pantalla de Cobros.'
            )
        else:
            cargo = Cargo(
                matricula_fk=alumno.matricula_id,
                concepto_cobro_fk=concepto_unico.id,
                concepto=concepto_unico.nombre,
                monto=concepto_unico.monto_sugerido,
                periodo_escolar=periodo_actual,
                fecha_vencimiento=vencimiento_default,
                estatus=EstatusCargo.PENDIENTE,
            )
            db.session.add(cargo)
            cargos_generados.append(cargo)

    concepto_mensualidad = ConceptoCobro.query.filter_by(es_mensualidad=True, activo=True).first()
    if concepto_mensualidad and (not alumno.plan or alumno.plan.monto_mensualidad is None):
        avisos_de_configuracion.append(
            f'No se generaron los cargos de mensualidad: la carrera '
            f'"{alumno.plan.nombre if alumno.plan else "(sin plan)"}" todavía no tiene '
            'mensualidad configurada. Captúrala en Mensualidades por Carrera.'
        )
    elif concepto_mensualidad:
        for anio, mes in _meses_del_cuatrimestre_actual():
            etiqueta_mes = f'{periodo_actual}-{MESES_ES[mes]}'  # Ej. "2026-B-May" -- único por mes, cabe en 20 caracteres
            if _cargo_duplicado(alumno.matricula_id, concepto_mensualidad.id, etiqueta_mes):
                continue
            cargo = Cargo(
                matricula_fk=alumno.matricula_id,
                concepto_cobro_fk=concepto_mensualidad.id,
                concepto=concepto_mensualidad.nombre,
                monto=_monto_mensualidad_con_beca(alumno, etiqueta_mes),
                periodo_escolar=etiqueta_mes,
                fecha_vencimiento=date(anio, mes, 10),
                estatus=EstatusCargo.PENDIENTE,
            )
            db.session.add(cargo)
            cargos_generados.append(cargo)

    return cargos_generados, avisos_de_configuracion


def _generar_cargos_de_inscripcion(alumno):
    """
    Al activar a un alumno por PRIMERA vez: cargo de Inscripción +
    mensualidades del cuatrimestre en curso. Devuelve
    (cargos_generados, avisos_de_configuracion).
    """
    return _generar_cargos_de_periodo(alumno, 'Inscripción')


def _generar_cargos_de_reinscripcion(alumno):
    """
    Al avanzarlo de cuatrimestre: cargo de Reinscripción + mensualidades
    del nuevo cuatrimestre en curso. Devuelve
    (cargos_generados, avisos_de_configuracion).
    """
    return _generar_cargos_de_periodo(alumno, 'Reinscripción')


def _vencimiento_dia_10_sugerido() -> str:
    """
    Fecha sugerida para el vencimiento de mensualidad/reinscripción: el
    alumno tiene del 1 al 10 del mes para pagar. Si ya pasó el día 10 de
    este mes, se sugiere el día 10 del mes siguiente.
    """
    hoy = hoy_local()
    anio, mes = hoy.year, hoy.month
    if hoy.day > 10:
        mes += 1
        if mes > 12:
            mes = 1
            anio += 1
    return date(anio, mes, 10).isoformat()
