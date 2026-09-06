"""
Pruebas de zona horaria local (México, America/Mexico_City = UTC-6).

Contexto del bug real: el VPS corre en UTC, pero las reglas de negocio
(vencimientos, recargos, corte del día, folios) deben calcularse con la
fecha que vive la institución, no con la fecha del servidor. Sin esto,
después de las 6 PM hora de México (medianoche UTC) el sistema ya "vive"
en el día siguiente para efectos de vencimientos y cortes de caja.

Estas pruebas usan freeze_time para fijar el instante real (UTC) en el
que corre el código -- es la única forma honesta de probar un bug que
depende de la hora del reloj, sin esperar a que sea esa hora de verdad.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from tests.conftest import crear_plan, crear_alumno
from app import (
    db,
    Cargo,
    Pago,
    MetodoPago,
    ahora_utc,
    hoy_local,
    a_local,
    rango_utc_del_dia,
    periodo_escolar_actual,
    siguiente_folio,
    _calcular_reporte_cobros_del_dia,
)

TZ_MX = ZoneInfo('America/Mexico_City')


# ---------------------------------------------------------------------------
# Helpers puros (app.py) -- no requieren la app de Flask corriendo.
# ---------------------------------------------------------------------------

def test_hoy_local_sigue_en_el_dia_anterior_cuando_utc_ya_cambio_de_fecha():
    """
    2026-01-02 03:30 UTC = 2026-01-01 21:30 en México (UTC-6). El
    servidor (UTC) ya cree que es otro día; hoy_local() debe seguir
    diciendo que es el 1 de enero.
    """
    with freeze_time('2026-01-02 03:30:00'):
        assert hoy_local(TZ_MX) == date(2026, 1, 1)
        assert ahora_utc().date() == date(2026, 1, 2)  # el bug que corregimos


def test_a_local_convierte_un_datetime_utc_guardado_a_hora_de_mexico():
    # 2026-03-20 06:00 UTC = 2026-03-20 00:00 en México
    dt_utc = datetime(2026, 3, 20, 6, 0)
    assert a_local(dt_utc, TZ_MX) == datetime(2026, 3, 20, 0, 0)


def test_a_local_de_none_es_none():
    assert a_local(None, TZ_MX) is None


def test_rango_utc_del_dia_traduce_un_dia_local_completo_a_su_rango_utc():
    ini, fin = rango_utc_del_dia(date(2026, 3, 20), TZ_MX)
    assert ini == datetime(2026, 3, 20, 6, 0, 0)
    assert fin == datetime(2026, 3, 21, 5, 59, 59, 999999)


# ---------------------------------------------------------------------------
# Reglas de negocio (requieren la app -- usan app.config['ZONA_HORARIA'])
# ---------------------------------------------------------------------------

def test_cargo_que_vence_hoy_en_mexico_no_esta_vencido_aunque_en_utc_ya_sea_manana(client, app):
    """
    Antes de esta corrección, esta_vencido() comparaba contra
    ahora_utc().date() -- un cargo que vence HOY (hora de México) ya
    aparecía vencido desde las 6 PM, aunque el alumno todavía tuviera
    horas del día para pagar.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)

    with freeze_time('2026-01-02 03:30:00'):  # 2026-01-01 21:30 en México
        cargo = Cargo(
            matricula_fk=alumno.matricula_id,
            concepto='Colegiatura de Prueba',
            monto=Decimal('1000.00'),
            fecha_vencimiento=date(2026, 1, 1),  # vence HOY en México
        )
        db.session.add(cargo)
        db.session.commit()

        assert cargo.esta_vencido() is False


def test_pago_de_la_noche_aparece_en_el_corte_del_dia_local_no_del_dia_utc(client, app):
    """
    Un pago cobrado el viernes a las 8:30 PM hora de México (2026-01-03
    02:30 UTC, ya sábado en UTC) debe aparecer en el reporte del VIERNES
    -- el día que vivió la caja -- no en el del sábado.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Colegiatura de Prueba',
        monto=Decimal('1000.00'),
    )
    db.session.add(cargo)
    db.session.commit()

    with freeze_time('2026-01-03 02:30:00'):  # viernes 2/ene 20:30 en México
        pago = Pago(cargo=cargo, monto_pagado=Decimal('500.00'), metodo_pago=MetodoPago.EFECTIVO)
        db.session.add(pago)
        db.session.commit()
        pago_id = pago.id

    pagos_viernes, total_viernes, _, _ = _calcular_reporte_cobros_del_dia(date(2026, 1, 2))
    pagos_sabado, total_sabado, _, _ = _calcular_reporte_cobros_del_dia(date(2026, 1, 3))

    assert pago_id in [p.id for p in pagos_viernes]
    assert total_viernes == Decimal('500.00')
    assert pago_id not in [p.id for p in pagos_sabado]
    assert total_sabado == Decimal('0.00')


def test_siguiente_folio_usa_el_anio_de_mexico_no_el_de_utc(client, app):
    """31 de diciembre 8 PM en México ya es 1 de enero en UTC -- el folio no debe saltarse de año antes de tiempo."""
    with freeze_time('2027-01-01 02:00:00'):  # 2026-12-31 20:00 en México
        folio = siguiente_folio('TEST_ZH', 'ZH')
    assert folio.startswith('ZH-2026-')


def test_periodo_escolar_actual_usa_la_fecha_de_mexico(client, app):
    """Mismo caso límite: el periodo escolar tampoco debe adelantarse un día."""
    with freeze_time('2027-01-01 02:00:00'):  # 2026-12-31 20:00 en México -> periodo C
        assert periodo_escolar_actual() == '2026-C'


# ---------------------------------------------------------------------------
# Filtros de plantilla
# ---------------------------------------------------------------------------

def test_filtro_fechahora_muestra_la_hora_local_no_la_utc(app):
    # 2026-03-20 06:15 UTC = 2026-03-20 00:15 en México
    resultado = app.jinja_env.filters['fechahora'](datetime(2026, 3, 20, 6, 15))
    assert resultado == '20/03/2026 00:15'


def test_filtro_hora_muestra_solo_la_hora_local(app):
    resultado = app.jinja_env.filters['hora'](datetime(2026, 3, 20, 6, 15))
    assert resultado == '00:15'


def test_filtro_fecha_larga_usa_meses_en_espanol_sin_depender_del_locale_del_so(app):
    """strftime('%B') en un VPS Ubuntu sin locale es_MX devuelve el mes en inglés."""
    resultado = app.jinja_env.filters['fecha_larga'](date(2026, 3, 17))
    assert resultado == '17 de marzo de 2026'


def test_filtro_fecha_no_recorre_un_date_puro(app):
    """fecha_vencimiento, fecha_nacimiento, etc. ya son locales por diseño -- no se convierten."""
    resultado = app.jinja_env.filters['fecha'](date(2026, 3, 17))
    assert resultado == '17/03/2026'


def test_filtro_fecha_si_convierte_un_datetime(app):
    # 2026-03-20 06:00 UTC (medianoche en México) -> debe mostrar 20/03/2026, no 19/03
    resultado = app.jinja_env.filters['fecha'](datetime(2026, 3, 20, 6, 0))
    assert resultado == '20/03/2026'
