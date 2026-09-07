"""
Pruebas de la fecha de vencimiento de los cargos (hallazgo funcional #8).

MOTIVO: en la pantalla de cobros apareció un cargo con "Periodo: 2026-B /
Vence: 10/08/2026" estando ya en septiembre de 2026, y había que
determinar si la fecha estaba mal calculada o si era un cargo histórico
legítimo.

CONCLUSIÓN (documentada aquí para que quede fijada): la regla del negocio
es "el alumno tiene del 1 al 10 del mes para pagar"
(_vencimiento_dia_10_sugerido). Un cargo generado el 6 de agosto vence,
correctamente, el 10 de agosto. Que hoy sea septiembre no lo vuelve un
error: lo vuelve un cargo VENCIDO, que es justo lo que el sistema debe
mostrar.

Estas pruebas fijan esa regla -- que no existía cubierta por ninguna
prueba -- para que un cambio futuro no la rompa en silencio.
"""

from datetime import date
from decimal import Decimal

from freezegun import freeze_time

from tests.conftest import crear_plan, crear_alumno
from app import db, Cargo, EstatusCargo, _vencimiento_dia_10_sugerido, hoy_local


def _crear_cargo(alumno, fecha_vencimiento, estatus=EstatusCargo.PENDIENTE, monto='1000.00'):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Colegiatura de Prueba',
        monto=Decimal(monto),
        fecha_vencimiento=fecha_vencimiento,
        estatus=estatus,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


# ---------------------------------------------------------------------------
# La regla "del 1 al 10 de cada mes"
# ---------------------------------------------------------------------------

@freeze_time('2026-08-06 20:00:00')  # 14:00 en México
def test_cargo_generado_antes_del_dia_10_vence_ese_mismo_mes(app):
    """
    Reproduce EXACTAMENTE el caso que se vio en pantalla: los cargos
    reales se generaron el 2026-08-06 y quedaron con vencimiento
    2026-08-10. Era el resultado correcto para ese día.
    """
    assert _vencimiento_dia_10_sugerido() == '2026-08-10'


@freeze_time('2026-08-06 20:00:00')
def test_el_dia_10_todavia_cuenta_como_del_mismo_mes(app):
    """El día 10 es el último con derecho a pagar: no debe saltar al mes siguiente."""
    with freeze_time('2026-08-10 20:00:00'):
        assert _vencimiento_dia_10_sugerido() == '2026-08-10'


@freeze_time('2026-08-11 20:00:00')
def test_cargo_generado_despues_del_dia_10_vence_el_mes_siguiente(app):
    assert _vencimiento_dia_10_sugerido() == '2026-09-10'


@freeze_time('2026-12-15 20:00:00')
def test_el_vencimiento_de_diciembre_pasa_a_enero_del_anio_siguiente(app):
    """Caso límite de fin de año: no debe generar un mes 13."""
    assert _vencimiento_dia_10_sugerido() == '2027-01-10'


# ---------------------------------------------------------------------------
# Coherencia entre la fecha y el estado "Vencido"
# ---------------------------------------------------------------------------

@freeze_time('2026-09-06 20:00:00')
def test_un_cargo_de_un_mes_anterior_aparece_como_vencido(app):
    """
    El caso observado: cargo con vencimiento 10/08/2026 visto el
    06/09/2026. La fecha almacenada es correcta; lo que corresponde es
    que el sistema lo marque como vencido, no que la fecha cambie.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, date(2026, 8, 10))

    assert cargo.fecha_vencimiento < hoy_local()
    assert cargo.esta_vencido() is True


@freeze_time('2026-09-06 20:00:00')
def test_un_cargo_que_vence_hoy_todavia_no_esta_vencido(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, date(2026, 9, 6))

    assert cargo.esta_vencido() is False


@freeze_time('2026-09-06 20:00:00')
def test_un_cargo_futuro_no_esta_vencido(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, date(2026, 9, 10))

    assert cargo.esta_vencido() is False


@freeze_time('2026-09-06 20:00:00')
def test_un_cargo_pagado_o_cancelado_nunca_aparece_vencido(app):
    """Aunque la fecha ya pasó: pagado o cancelado deja de ser un adeudo."""
    plan = crear_plan()
    alumno = crear_alumno(plan)

    pagado = _crear_cargo(alumno, date(2026, 8, 10), estatus=EstatusCargo.PAGADO)
    cancelado = _crear_cargo(alumno, date(2026, 8, 10), estatus=EstatusCargo.CANCELADO,
                             monto='500.00')

    assert pagado.esta_vencido() is False
    assert cancelado.esta_vencido() is False


@freeze_time('2026-09-06 20:00:00')
def test_un_cargo_sin_fecha_de_vencimiento_no_esta_vencido(app):
    """Los cargos de un solo pago pueden no tener fecha; no deben marcarse."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, None)

    assert cargo.esta_vencido() is False
