"""
Auditoría 2026-09-19: los montos capturados por el personal se aceptaban con
solo "es Decimal y > 0". Eso dejaba pasar `Infinity`, `1e30`, más de dos
decimales (0.001 -> $0.00 guardado; 99.995 -> pago que deja el cargo en
"Parcial" con saldo 0) y valores que en PostgreSQL desbordan Numeric(10,2).

Estas pruebas fijan un único punto de validación: utilidades/dinero.py.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import (
    db, Cargo, ConceptoCobro, ConfiguracionCobros, EstatusCargo, Pago,
    PlanEstudio, TipoRecargo, hoy_local,
)
from utilidades.dinero import parsear_monto, MontoInvalido, MONTO_MAXIMO


# ---------------------------------------------------------------------------
# La función
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('crudo,esperado', [
    ('100', Decimal('100.00')),
    ('100.5', Decimal('100.50')),
    ('  1250.75 ', Decimal('1250.75')),
    ('0.01', Decimal('0.01')),
    ('100.500', Decimal('100.50')),      # ceros a la derecha no son un tercer decimal
    ('99999999.99', MONTO_MAXIMO),
])
def test_parsear_monto_acepta_importes_normales(crudo, esperado):
    assert parsear_monto(crudo) == esperado


@pytest.mark.parametrize('crudo', [
    '', '   ', 'abc', 'Infinity', '-Infinity', 'NaN', 'inf',
    '1e30', '1E+7', '1e2',            # notación científica: nadie captura así un importe
    '0x10', '12,50', '1_000',
    '１２３',                          # dígitos de ancho completo (Decimal los acepta)
    '-1', '-0.01',
    '0', '0.00', '0.001', '0.004',    # cero, o cero tras redondear
    '0.001', '99.995', '50.005',      # tercer decimal
    '100000000', '100000000.00', '99999999.991',
])
def test_parsear_monto_rechaza_lo_que_no_es_un_importe_valido(crudo):
    with pytest.raises(MontoInvalido):
        parsear_monto(crudo)


def test_parsear_monto_permite_cero_solo_si_se_pide():
    assert parsear_monto('0', permitir_cero=True) == Decimal('0.00')
    assert parsear_monto('0.00', permitir_cero=True) == Decimal('0.00')
    with pytest.raises(MontoInvalido):
        parsear_monto('-0.01', permitir_cero=True)


def test_parsear_monto_respeta_un_maximo_propio():
    assert parsear_monto('100', maximo=Decimal('100')) == Decimal('100.00')
    with pytest.raises(MontoInvalido):
        parsear_monto('100.01', maximo=Decimal('100'))


# ---------------------------------------------------------------------------
# Las rutas
# ---------------------------------------------------------------------------

def _preparar(client, rol='DIRECTIVO', username='directivo1'):
    from app import RolUsuario
    crear_usuario(username=username, rol=RolUsuario[rol])
    login(client, username, 'clave12345')


def _concepto(nombre='Uniformes', monto=None):
    c = ConceptoCobro(nombre=nombre, monto_sugerido=monto, activo=True)
    db.session.add(c)
    db.session.commit()
    return c


@pytest.mark.parametrize('monto', ['Infinity', '1e30', '0.001', '99.995', '100000000.00', '1_000', '１２３'])
def test_nuevo_cargo_rechaza_montos_invalidos(client, app, monto):
    plan = crear_plan(); alumno = crear_alumno(plan); concepto = _concepto()
    _preparar(client)
    client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': concepto.id, 'monto': monto, 'periodo_escolar': 'P1',
    })
    assert Cargo.query.count() == 0


def _cargo(alumno, concepto, monto='100.00', **kw):
    c = Cargo(matricula_fk=alumno.matricula_id, concepto_cobro_fk=concepto.id, concepto=concepto.nombre,
              monto=Decimal(monto), periodo_escolar='P', **kw)
    db.session.add(c); db.session.commit()
    return c


@pytest.mark.parametrize('monto', ['0.001', '99.995', '50.005', '1e2', 'Infinity'])
def test_registrar_pago_rechaza_pagos_con_mas_de_dos_decimales_o_formato_raro(client, app, monto):
    plan = crear_plan(); alumno = crear_alumno(plan); cargo = _cargo(alumno, _concepto())
    _preparar(client, 'CONTADOR', 'contador1')
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': monto, 'metodo_pago': 'EFECTIVO'})
    db.session.expire_all()
    assert Pago.query.count() == 0
    assert db.session.get(Cargo, cargo.id).estatus == EstatusCargo.PENDIENTE


def test_registrar_pago_exacto_sigue_funcionando(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan); cargo = _cargo(alumno, _concepto())
    _preparar(client, 'CONTADOR', 'contador1')
    client.post(f'/cobro/{cargo.id}/pagar', data={'monto_pagado': '100.00', 'metodo_pago': 'EFECTIVO'})
    db.session.expire_all()
    assert db.session.get(Cargo, cargo.id).estatus == EstatusCargo.PAGADO


@pytest.mark.parametrize('tipo,valor', [
    ('MONTO_FIJO', 'Infinity'), ('PORCENTAJE', '100.000001'), ('PORCENTAJE', '0.001'),
    ('MONTO_FIJO', '1e30'), ('MONTO_FIJO', '0'),
])
def test_beca_rechaza_valores_invalidos(client, app, tipo, valor):
    from modelos import Beca
    plan = crear_plan(); alumno = crear_alumno(plan)
    _preparar(client)
    client.post(f'/alumno/{alumno.matricula_id}/becas', data={
        'nombre': 'Beca X', 'tipo_descuento': tipo, 'valor': valor, 'periodo_escolar': '2026-B',
    })
    assert Beca.query.count() == 0


@pytest.mark.parametrize('valor', ['Infinity', '1e30', '100000000', '-1', '0.001'])
def test_mensualidad_de_carrera_rechaza_valores_invalidos(client, app, valor):
    plan = crear_plan()
    _preparar(client)
    client.post('/planes/mensualidades', data={'plan_id': plan.id, 'monto_mensualidad': valor})
    db.session.expire_all()
    assert db.session.get(PlanEstudio, plan.id).monto_mensualidad is None


@pytest.mark.parametrize('valor', ['Infinity', '1e30', '100000000', '0.001'])
def test_precio_de_concepto_rechaza_valores_invalidos(client, app, valor):
    _preparar(client)
    client.post('/conceptos-cobro', data={'nombre': 'Concepto raro', 'monto_sugerido': valor})
    assert ConceptoCobro.query.filter_by(nombre='Concepto raro').count() == 0
    existente = _concepto('Otro', Decimal('10.00'))
    client.post(f'/conceptos-cobro/{existente.id}/editar-precio', data={'monto_sugerido': valor})
    db.session.expire_all()
    assert db.session.get(ConceptoCobro, existente.id).monto_sugerido == Decimal('10.00')


@pytest.mark.parametrize('tipo,valor,gracia', [
    ('MONTO_FIJO', 'Infinity', '0'),
    ('POR_DIA', 'Infinity', '0'),
    ('PORCENTAJE', '100.01', '0'),          # un porcentaje no pasa de 100
    ('PORCENTAJE_MENSUAL', '100.01', '0'),
    ('POR_DIA', '99999999', '0'),           # tope razonable para un recargo por día
    ('MONTO_FIJO', '10', '99999999999999999999'),
    ('MONTO_FIJO', '10', '366'),            # más de un año de gracia no es una política
    ('MONTO_FIJO', '10', '-1'),
])
def test_configuracion_de_recargos_rechaza_valores_que_romperian_las_pantallas(client, app, tipo, valor, gracia):
    _preparar(client)
    antes = ConfiguracionCobros.obtener()
    tipo_antes, valor_antes = antes.tipo_recargo, antes.valor_recargo
    r = client.post('/configuracion/cobros', data={'tipo_recargo': tipo, 'valor_recargo': valor, 'dias_gracia': gracia})
    assert r.status_code == 302, 'una entrada hostil no debe producir 500'
    db.session.expire_all()
    despues = ConfiguracionCobros.obtener()
    assert (despues.tipo_recargo, despues.valor_recargo) == (tipo_antes, valor_antes)


def test_configuracion_de_recargos_valida_sigue_funcionando(client, app):
    _preparar(client)
    client.post('/configuracion/cobros', data={'tipo_recargo': 'POR_DIA', 'valor_recargo': '50.00', 'dias_gracia': '5'})
    db.session.expire_all()
    cfg = ConfiguracionCobros.obtener()
    assert (cfg.tipo_recargo, cfg.valor_recargo, cfg.dias_gracia) == (TipoRecargo.POR_DIA, Decimal('50.00'), 5)


def test_condonar_recargo_rechaza_montos_con_formato_raro(client, app):
    plan = crear_plan(); alumno = crear_alumno(plan)
    cargo = _cargo(alumno, _concepto(), recargo_aplicado=Decimal('200.00'))
    _preparar(client)
    client.post(f'/cobro/{cargo.id}/condonar-recargo', data={'nuevo_recargo': '0.001', 'motivo_condonacion': 'x'})
    db.session.expire_all()
    assert db.session.get(Cargo, cargo.id).recargo_aplicado == Decimal('200.00')


@pytest.mark.parametrize('fecha', ['0001-01-01', '1900-01-01', '9999-12-31'])
def test_fecha_de_vencimiento_absurda_se_rechaza(client, app, fecha):
    plan = crear_plan(); alumno = crear_alumno(plan); concepto = _concepto()
    _preparar(client)
    client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': concepto.id, 'monto': '100', 'periodo_escolar': 'V', 'fecha_vencimiento': fecha,
    })
    assert Cargo.query.count() == 0


def test_el_recargo_calculado_nunca_supera_el_maximo_de_la_columna(app):
    """Defensa en profundidad: aunque la configuración ya guardada sea extrema, no se desborda Numeric(10,2)."""
    plan = crear_plan(); alumno = crear_alumno(plan)
    cfg = ConfiguracionCobros.obtener()
    cfg.tipo_recargo = TipoRecargo.POR_DIA; cfg.valor_recargo = Decimal('99999999.00'); cfg.dias_gracia = 0
    db.session.commit()
    cargo = _cargo(alumno, _concepto(), fecha_vencimiento=hoy_local() - timedelta(days=400))
    cargo.actualizar_recargo_si_vencido()
    assert cargo.recargo_aplicado <= MONTO_MAXIMO
