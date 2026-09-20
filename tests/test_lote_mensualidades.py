"""
Auditoría 2026-09-19 (simulación con 1,500 alumnos): el lote de
/cobros/generar-mensualidades
  - ignoraba las becas (75 alumnos con beca del 50% se cobraron completos),
  - cobraba el precio de MENSUALIDAD bajo cualquier concepto elegido,
  - permitía cobrar dos veces el mismo mes con nombres de periodo distintos
    ("2026-C-Sep", "2026-c-sep", "Septiembre 2026") porque la deduplicación
    compara texto exacto y el periodo era texto libre.
"""

from decimal import Decimal

import pytest

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, ConceptoCobro, EstatusCargo
from modelos import Beca, TipoDescuentoBeca


@pytest.fixture
def escenario(client, app):
    plan = crear_plan(); plan.monto_mensualidad = Decimal('2500.00'); db.session.commit()
    a1 = crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='TST2026-00001', nombre='Alumna Sin Beca')
    a2 = crear_alumno(plan, curp='BBBB010101HDFXYZ02', matricula_id='TST2026-00002', nombre='Alumna Con Beca')
    mens = ConceptoCobro(nombre='Mensualidad', es_mensualidad=True, activo=True)
    unif = ConceptoCobro(nombre='Uniformes', monto_sugerido=Decimal('300.00'), activo=True)
    sinprecio = ConceptoCobro(nombre='Constancias', activo=True)
    db.session.add_all([mens, unif, sinprecio]); db.session.commit()
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    return dict(plan=plan, a1=a1, a2=a2, mens=mens, unif=unif, sinprecio=sinprecio)


def _lote(client, concepto, periodo, vencimiento='2026-09-10'):
    return client.post('/cobros/generar-mensualidades', data={
        'concepto_cobro_id': concepto.id, 'periodo_escolar': periodo, 'fecha_vencimiento': vencimiento,
    })


def _monto(alumno, concepto_nombre='Mensualidad'):
    c = Cargo.query.filter_by(matricula_fk=alumno.matricula_id, concepto=concepto_nombre).first()
    return None if c is None else c.monto


def _beca(alumno, tipo, valor, periodo='2026-C'):
    db.session.add(Beca(matricula_fk=alumno.matricula_id, nombre='Beca', tipo_descuento=tipo,
                        valor=Decimal(valor), periodo_escolar=periodo, activa=True))
    db.session.commit()


def test_el_lote_respeta_las_becas(client, escenario):
    _beca(escenario['a2'], TipoDescuentoBeca.PORCENTAJE, '50')
    _lote(client, escenario['mens'], '2026-C-Sep')
    assert _monto(escenario['a1']) == Decimal('2500.00')
    assert _monto(escenario['a2']) == Decimal('1250.00')


def test_una_beca_del_cien_por_ciento_no_deja_un_cargo_pendiente_en_cero(client, escenario):
    _beca(escenario['a2'], TipoDescuentoBeca.PORCENTAJE, '100')
    _lote(client, escenario['mens'], '2026-C-Sep')
    cargo = Cargo.query.filter_by(matricula_fk=escenario['a2'].matricula_id).one()
    assert cargo.monto == Decimal('0.00')
    assert cargo.estatus == EstatusCargo.PAGADO


def test_un_concepto_que_no_es_mensualidad_cobra_su_propio_precio(client, escenario):
    _lote(client, escenario['unif'], 'UNIF-2026')
    assert _monto(escenario['a1'], 'Uniformes') == Decimal('300.00')
    assert _monto(escenario['a2'], 'Uniformes') == Decimal('300.00')


def test_un_concepto_sin_precio_no_genera_cargos(client, escenario):
    _lote(client, escenario['sinprecio'], 'CONST-2026')
    assert Cargo.query.count() == 0


def test_no_se_duplica_el_mes_por_diferencias_de_mayusculas(client, escenario):
    _lote(client, escenario['mens'], '2026-C-Sep')
    _lote(client, escenario['mens'], '2026-c-sep')
    assert Cargo.query.filter_by(concepto='Mensualidad').count() == 2  # uno por alumna, no cuatro


@pytest.mark.parametrize('periodo', ['Septiembre 2026', '2026-C', 'SEP 2026', '2026-Sep', '2026-C-Septiembre', '2026-C-Xyz'])
def test_la_mensualidad_exige_el_formato_del_flujo_automatico(client, escenario, periodo):
    _lote(client, escenario['mens'], periodo)
    assert Cargo.query.count() == 0


def test_el_periodo_de_mensualidad_se_guarda_en_forma_canonica(client, escenario):
    _lote(client, escenario['mens'], ' 2026-c-sep ')
    assert {c.periodo_escolar for c in Cargo.query.all()} == {'2026-C-Sep'}


def test_un_cargo_manual_de_mensualidad_tambien_exige_el_formato(client, escenario):
    a = escenario['a1']
    client.post(f'/alumno/{a.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': escenario['mens'].id, 'monto': '2500', 'periodo_escolar': 'Septiembre 2026'})
    assert Cargo.query.count() == 0
    client.post(f'/alumno/{a.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': escenario['mens'].id, 'monto': '2500', 'periodo_escolar': '2026-C-Sep'})
    assert Cargo.query.count() == 1


def test_un_cargo_manual_de_mensualidad_no_se_duplica_con_el_del_lote(client, escenario):
    _lote(client, escenario['mens'], '2026-C-Sep')
    a = escenario['a1']
    client.post(f'/alumno/{a.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': escenario['mens'].id, 'monto': '2500', 'periodo_escolar': '2026-c-SEP'})
    assert Cargo.query.filter_by(matricula_fk=a.matricula_id, concepto='Mensualidad').count() == 1


def test_otorgar_una_beca_total_a_un_cargo_ya_generado_lo_deja_pagado(client, escenario):
    _lote(client, escenario['mens'], '2026-C-Sep')
    a = escenario['a2']
    client.post(f'/alumno/{a.matricula_id}/becas', data={
        'nombre': 'Beca total', 'tipo_descuento': 'PORCENTAJE', 'valor': '100', 'periodo_escolar': '2026-C'})
    db.session.expire_all()
    cargo = Cargo.query.filter_by(matricula_fk=a.matricula_id).one()
    assert (cargo.monto, cargo.estatus) == (Decimal('0.00'), EstatusCargo.PAGADO)
