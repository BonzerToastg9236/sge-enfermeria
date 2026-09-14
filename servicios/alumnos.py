"""Estadísticas y agregaciones sobre Alumno para la pantalla de búsqueda."""

from sqlalchemy import func

from extensiones import db
from modelos import Alumno, EstatusAlumno, Cargo, EstatusCargo, Pago


def _matriculas_con_adeudo():
    """
    Devuelve el conjunto de matrículas con saldo pendiente > 0, calculado
    con UNA agregación en SQL (subquery de pagos por cargo) en vez de
    cargar cada Alumno y cada Cargo/Pago en Python.

    PERFORMANCE-NOTE: la versión anterior hacía Alumno.query.join(Cargo)...all()
    y luego, para cada alumno, tiene_adeudo() -> saldo_total_adeudado(),
    que a su vez recorre alumno.cargos y, por cada cargo, cargo.pagos
    (ambos lazy='select'). Con pocos alumnos no se nota, pero es un
    patrón N+1 clásico: con miles de alumnos con adeudo, cada carga del
    dashboard dispara cientos/miles de consultas adicionales. Esta versión
    hace el cálculo en 1 sola consulta agregada, sin importar cuántos
    alumnos existan.
    """
    pagos_por_cargo = (
        db.session.query(
            Pago.cargo_fk,
            func.coalesce(func.sum(Pago.monto_pagado), 0).label('total_pagado')
        )
        .filter(Pago.anulado.is_(False))  # Un pago anulado ya no cuenta para el saldo
        .group_by(Pago.cargo_fk)
        .subquery()
    )

    saldo_expr = (
        Cargo.monto + Cargo.recargo_aplicado
        - func.coalesce(pagos_por_cargo.c.total_pagado, 0)
    )

    filas = (
        db.session.query(Cargo.matricula_fk, func.sum(saldo_expr).label('saldo'))
        .outerjoin(pagos_por_cargo, pagos_por_cargo.c.cargo_fk == Cargo.id)
        .filter(Cargo.estatus != EstatusCargo.CANCELADO)
        .group_by(Cargo.matricula_fk)
        .having(func.sum(saldo_expr) > 0)
        .all()
    )
    return {matricula for matricula, _saldo in filas}


def calcular_estadisticas_alumnos():
    """Números clave para el panel del buscador (pantalla de inicio)."""
    con_adeudo = len(_matriculas_con_adeudo())

    return {
        'total': Alumno.query.count(),
        'pendientes': Alumno.query.filter_by(estatus=EstatusAlumno.PENDIENTE).count(),
        'activos': Alumno.query.filter_by(estatus=EstatusAlumno.ACTIVO).count(),
        'documentacion_incompleta': Alumno.query.filter(Alumno.documentacion_pendiente.isnot(None)).count(),
        'faltas': Alumno.query.filter(Alumno.faltas_administrativas.isnot(None)).count(),
        'con_adeudo': con_adeudo,
    }
