"""
Simulación del 2026-09-20 (160 combinaciones tipo x valor x gracia x días x monto contra una fórmula
independiente): el CÁLCULO del recargo es correcto. El problema real era de uso: al BAJAR la política
(p. ej. 50/día -> 10/día) los cargos ya vencidos conservaban el recargo alto (el recargo solo sube), y la
pantalla decía que el cambio "aplica de inmediato". Ahora hay un simulador, una vista previa del impacto
y una opción explícita para recalcular los cargos ya vencidos (sin dejar saldo a favor).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, ConceptoCobro, ConfiguracionCobros, EstatusCargo, Pago, RolUsuario, TipoRecargo
from modelos import BitacoraAuditoria
from modelos.cobros import calcular_recargo
from servicios.cobros import recalcular_recargos_vencidos, impacto_de_politica

HOY = date(2026, 9, 20)


@pytest.fixture(autouse=True)
def hoy_fijo():
    with freeze_time('2026-09-20 12:00:00'):
        yield


# ------------------------------- la fórmula pura -------------------------------

@pytest.mark.parametrize('tipo,valor,gracia,dias,esperado', [
    (TipoRecargo.MONTO_FIJO, '150', 0, 1, '150.00'),
    (TipoRecargo.MONTO_FIJO, '150', 3, 3, '0.00'),          # dentro de la gracia
    (TipoRecargo.MONTO_FIJO, '150', 3, 4, '150.00'),
    (TipoRecargo.PORCENTAJE, '10', 0, 5, '250.00'),
    (TipoRecargo.PORCENTAJE, '7.5', 5, 10, '187.50'),
    (TipoRecargo.POR_DIA, '20', 0, 10, '200.00'),
    (TipoRecargo.POR_DIA, '12.50', 3, 10, '87.50'),          # solo cuentan los días DESPUÉS de la gracia
    (TipoRecargo.PORCENTAJE_MENSUAL, '10', 0, 30, '250.00'),
    (TipoRecargo.PORCENTAJE_MENSUAL, '10', 0, 31, '500.00'),
    (TipoRecargo.PORCENTAJE_MENSUAL, '5', 3, 95, '500.00'),
])
def test_calcular_recargo_de_un_cargo_de_2500(tipo, valor, gracia, dias, esperado):
    assert calcular_recargo(Decimal('2500'), dias, tipo, Decimal(valor), gracia) == Decimal(esperado)


def test_calcular_recargo_nunca_pasa_de_lo_que_cabe_en_la_columna():
    assert calcular_recargo(Decimal('2500'), 100_000, TipoRecargo.POR_DIA, Decimal('10000'), 0) == Decimal('99999999.99')


# ------------------------------- recalcular cargos ya vencidos -------------------------------

def _politica(tipo, valor, gracia=0):
    cfg = ConfiguracionCobros.obtener()
    cfg.tipo_recargo, cfg.valor_recargo, cfg.dias_gracia = tipo, Decimal(valor), gracia
    db.session.commit()
    return cfg


def _escenario():
    plan = crear_plan(); alumno = crear_alumno(plan)
    conc = ConceptoCobro(nombre='Mensualidad', activo=True); db.session.add(conc); db.session.commit()

    def nuevo(dias, monto='2500.00', recargo='0.00', periodo=None, **kw):
        c = Cargo(matricula_fk=alumno.matricula_id, concepto_cobro_fk=conc.id, concepto='Mensualidad', monto=Decimal(monto),
                  recargo_aplicado=Decimal(recargo), periodo_escolar=periodo or f'P{Cargo.query.count()}',
                  fecha_vencimiento=HOY - timedelta(days=dias), **kw)
        db.session.add(c); db.session.commit()
        return c
    return alumno, nuevo


def test_bajar_la_politica_no_baja_el_recargo_ya_aplicado_salvo_que_se_recalcule(app):
    alumno, nuevo = _escenario()
    c = nuevo(10, recargo='500.00')                          # se calculó con 50/día
    cfg = _politica(TipoRecargo.POR_DIA, '10')
    c.actualizar_recargo_si_vencido(cfg)
    assert c.recargo_aplicado == Decimal('500.00')            # comportamiento histórico: el recargo solo sube

    resultado = recalcular_recargos_vencidos(cfg); db.session.commit()
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('100.00')
    assert (resultado['cambiados'], resultado['bajan'], resultado['suben']) == (1, 1, 0)
    assert resultado['total_antes'] == Decimal('500.00') and resultado['total_despues'] == Decimal('100.00')


def test_recalcular_tambien_sube_lo_que_haya_quedado_corto(app):
    alumno, nuevo = _escenario()
    c = nuevo(10, recargo='100.00')
    resultado = recalcular_recargos_vencidos(_politica(TipoRecargo.POR_DIA, '30')); db.session.commit()
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('300.00') and resultado['suben'] == 1


def test_recalcular_nunca_deja_el_recargo_por_debajo_de_lo_que_ya_se_pago(app):
    """Si el alumno ya pagó 2900 de un total de 3000, bajar el recargo a 100 dejaría $300 a favor: no se permite."""
    alumno, nuevo = _escenario()
    c = nuevo(10, recargo='500.00')
    db.session.add(Pago(cargo_fk=c.id, monto_pagado=Decimal('2900.00'), folio='PAGO-1')); c.estatus = EstatusCargo.PARCIAL; db.session.commit()
    recalcular_recargos_vencidos(_politica(TipoRecargo.POR_DIA, '10')); db.session.commit()
    c = db.session.get(Cargo, c.id)
    assert c.recargo_aplicado == Decimal('400.00') and c.saldo_pendiente() == Decimal('0.00')


def test_recalcular_no_toca_cargos_congelados_pagados_cancelados_ni_sin_vencimiento(app):
    alumno, nuevo = _escenario()
    congelado = nuevo(10, recargo='500.00', recargo_congelado=True)
    pagado = nuevo(10, recargo='500.00', estatus=EstatusCargo.PAGADO)
    cancelado = nuevo(10, recargo='500.00', estatus=EstatusCargo.CANCELADO)
    sin_venc = Cargo(matricula_fk=alumno.matricula_id, concepto='X', monto=Decimal('100'), recargo_aplicado=Decimal('50'), periodo_escolar='S'); db.session.add(sin_venc); db.session.commit()
    resultado = recalcular_recargos_vencidos(_politica(TipoRecargo.POR_DIA, '10')); db.session.commit()
    assert resultado['cambiados'] == 0
    assert [db.session.get(Cargo, x.id).recargo_aplicado for x in (congelado, pagado, cancelado, sin_venc)] == [Decimal(v) for v in ('500.00', '500.00', '500.00', '50.00')]


def test_recalcular_con_mas_dias_de_gracia_puede_dejar_el_recargo_en_cero(app):
    alumno, nuevo = _escenario()
    c = nuevo(5, recargo='100.00')
    recalcular_recargos_vencidos(_politica(TipoRecargo.POR_DIA, '20', gracia=10)); db.session.commit()
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('0.00')


def test_el_impacto_de_una_politica_cuenta_cuantos_suben_y_bajan_sin_cambiar_nada(app):
    alumno, nuevo = _escenario()
    alto = nuevo(10, recargo='500.00'); corto = nuevo(10, recargo='100.00'); igual = nuevo(10, recargo='200.00')
    impacto = impacto_de_politica(TipoRecargo.POR_DIA, Decimal('20'), 0)
    assert (impacto['vencidos'], impacto['suben'], impacto['bajan'], impacto['iguales']) == (3, 1, 1, 1)
    assert impacto['total_actual'] == Decimal('800.00') and impacto['total_recalculado'] == Decimal('600.00')
    assert impacto['total_sin_recalcular'] == Decimal('900.00')   # sin recalcular: 500 (se queda) + 200 (sube) + 200
    assert [db.session.get(Cargo, x.id).recargo_aplicado for x in (alto, corto, igual)] == [Decimal('500.00'), Decimal('100.00'), Decimal('200.00')]


# ------------------------------- desde la pantalla -------------------------------

def _post_config(client, **datos):
    base = dict(tipo_recargo='POR_DIA', valor_recargo='10', dias_gracia='0'); base.update(datos)
    return client.post('/configuracion/cobros', data=base)


def test_guardar_sin_marcar_recalcular_conserva_los_recargos_altos_y_lo_avisa(client, app):
    alumno, nuevo = _escenario(); c = nuevo(10, recargo='500.00')
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    _post_config(client)
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('500.00')
    html = client.get('/configuracion/cobros').get_data(as_text=True)
    assert 'recalcular_vencidos' in html and 'solo sube' in html.lower()


def test_guardar_marcando_recalcular_aplica_la_politica_a_los_cargos_vencidos_y_deja_bitacora(client, app):
    alumno, nuevo = _escenario(); c = nuevo(10, recargo='500.00')
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    r = _post_config(client, recalcular_vencidos='on')
    assert r.status_code == 302
    db.session.expire_all()
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('100.00')
    (reg,) = BitacoraAuditoria.query.filter_by(accion='CONFIG_RECARGOS').all()
    assert 'recalcul' in reg.detalle.lower() and '500.00' in reg.detalle and '100.00' in reg.detalle


def test_si_la_configuracion_es_invalida_no_se_recalcula_nada(client, app):
    alumno, nuevo = _escenario(); c = nuevo(10, recargo='500.00')
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    _post_config(client, valor_recargo='Infinity', recalcular_vencidos='on')
    assert db.session.get(Cargo, c.id).recargo_aplicado == Decimal('500.00')


# ------------------------------- simulador -------------------------------

def test_el_simulador_devuelve_ejemplos_calculados_con_la_misma_formula_y_el_impacto(client, app):
    alumno, nuevo = _escenario(); nuevo(10, recargo='500.00')
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    r = client.get('/configuracion/cobros/simular', query_string=dict(tipo_recargo='POR_DIA', valor_recargo='20', dias_gracia='3', monto='2500'))
    assert r.status_code == 200
    datos = r.get_json()
    ej = {fila['dias']: fila for fila in datos['ejemplos']}
    assert ej[3]['recargo'] == '0.00' and ej[10]['recargo'] == '140.00' and ej[10]['total'] == '2640.00'
    assert datos['impacto']['bajan'] == 1 and datos['impacto']['vencidos'] == 1


@pytest.mark.parametrize('params', [
    dict(tipo_recargo='XX', valor_recargo='10', dias_gracia='0'),
    dict(tipo_recargo='POR_DIA', valor_recargo='Infinity', dias_gracia='0'),
    dict(tipo_recargo='PORCENTAJE', valor_recargo='101', dias_gracia='0'),
    dict(tipo_recargo='POR_DIA', valor_recargo='10', dias_gracia='999'),
    dict(tipo_recargo='POR_DIA', valor_recargo='10', dias_gracia='0', monto='abc'),
])
def test_el_simulador_rechaza_entradas_invalidas_sin_error_500(client, app, params):
    crear_usuario(); login(client, 'directivo1', 'clave12345')
    r = client.get('/configuracion/cobros/simular', query_string=params)
    assert r.status_code == 400 and r.get_json()['error']


def test_el_simulador_es_solo_para_quien_puede_configurar_recargos(client, app):
    crear_usuario(username='cap', rol=RolUsuario.CAPTURADOR); login(client, 'cap', 'clave12345')
    r = client.get('/configuracion/cobros/simular', query_string=dict(tipo_recargo='POR_DIA', valor_recargo='10', dias_gracia='0'))
    assert r.status_code == 302


def test_los_tipos_de_recargo_se_explican_con_su_significado_real():
    assert 'monto del cargo' in TipoRecargo.PORCENTAJE.value.lower()      # se calcula sobre el monto original, no sobre el saldo
